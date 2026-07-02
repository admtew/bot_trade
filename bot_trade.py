"""
crypto_price_bot.py — инлайн Telegram-бот: цена крипты/рублей/долларов
сразу в 4 валютах — USD, RUB, Telegram Stars и TON (Gram).

Как пользоваться в Telegram (после запуска и настройки у @BotFather):
    в любом чате, канале, группе: @имя_бота 1 gram   /   2 биток   /   100р   /   50 звёзд
    в личке с ботом: просто напиши то же самое текстом, без @имени

Запуск:
    pip install aiogram aiohttp python-dotenv
    export BOT_TOKEN="8011957570:AAFI4qgGruRamBTBs8QyUno-L7HvmyTc9Zc"      # или создай рядом файл .env с BOT_TOKEN=...
    python crypto_price_bot.py

Настройка бота у @BotFather (один раз, до первого запуска):
    /newbot          — создать бота, получить токен
    /setinline       — включить инлайн-режим (без этого @имя_бота в чужих чатах не сработает)

Переменные окружения (все опциональны, кроме BOT_TOKEN):
    BOT_TOKEN                — обязателен, токен от @BotFather
    COINGECKO_API_KEY        — бесплатный демо-ключ CoinGecko, поднимает лимит
                                с ~10-30 до 100 запросов/мин, оформляется на
                                coingecko.com/en/api/pricing, для теста не нужен
    STARS_USD_RATE            — курс 1 Stars в USD, по умолчанию 0.013.
                                Stars — не крипта, у них нет биржевого курса.
                                0.013$ — ориентир по выплатам авторам через Fragment
                                (конец июня 2026, самая "оптовая" цена звезды).
                                Покупка в приложении дороже, ~0.02$, из-за комиссии
                                Apple/Google 30%. Меняй значение, если Telegram
                                поменяет тарификацию.
    PRICE_REFRESH_SECONDS     — как часто обновлять курсы в фоне, по умолчанию 30 сек
    PRICE_CACHE_TTL_SECONDS   — после скольких секунд без обновления курс считается
                                "устаревшим" (бот всё равно ответит последними
                                известными цифрами, просто добавит пометку)

Про формат сообщений: в июне 2026 Telegram выкатил Bot API 10.1 с "Rich
Messages" — бот официально может слать в сообщениях настоящие таблицы и
картинки, а не только жирный текст. aiogram 3.29+ это уже поддерживает
технически, но точный markdown/html-синтаксис самих таблиц Telegram пока
нигде построчно не задокументировал. Гадать со схемой ради красоты не стал —
поймал бы рандомные 400-е ошибки на ровном месте. Тут классический
HTML-формат (жирный заголовок + моноширинная таблица через <pre>) — он
гарантированно работает везде и выглядит вполне прилично. Пришлёшь скриншот
статьи про таблицы — доработаю под то, что там показано.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import operator
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("crypto_price_bot")

# ============================== КОНФИГ ==============================

BOT_TOKEN = os.getenv("8011957570:AAFI4qgGruRamBTBs8QyUno-L7HvmyTc9Zc")
COINGECKO_API_KEY = os.getenv("CG-tF8vekDtvH5UfU9RuJSJSoNU") or None
STARS_USD_RATE = float(os.getenv("STARS_USD_RATE", "0.013").replace(",", "."))
PRICE_REFRESH_SECONDS = int(os.getenv("PRICE_REFRESH_SECONDS", "30"))
PRICE_CACHE_TTL_SECONDS = int(os.getenv("PRICE_CACHE_TTL_SECONDS", "90"))
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


# ============================== ВАЛЮТЫ И АЛИАСЫ ==============================
# Задача этой секции — по любому слову ("биток", "соль", "грам", "100р")
# однозначно понять, о какой валюте речь. Алиасов специально много: падежи,
# сленг, транслит на русском и английском — чтобы бот не тупил на вариациях.

@dataclass(frozen=True)
class Unit:
    code: str
    display: str
    kind: str  # "crypto" | "fiat" | "stars"
    emoji: str
    coingecko_id: Optional[str] = None


UNITS: dict[str, Unit] = {
    "BTC": Unit("BTC", "Bitcoin", "crypto", "🟠", "bitcoin"),
    "ETH": Unit("ETH", "Ethereum", "crypto", "🔷", "ethereum"),
    "TON": Unit("TON", "Toncoin (Gram)", "crypto", "💎", "the-open-network"),
    "SOL": Unit("SOL", "Solana", "crypto", "🟣", "solana"),
    "BNB": Unit("BNB", "BNB", "crypto", "🟡", "binancecoin"),
    "XRP": Unit("XRP", "XRP", "crypto", "⚪", "ripple"),
    "DOGE": Unit("DOGE", "Dogecoin", "crypto", "🐶", "dogecoin"),
    "ADA": Unit("ADA", "Cardano", "crypto", "🔵", "cardano"),
    "TRX": Unit("TRX", "Tron", "crypto", "🔴", "tron"),
    "LTC": Unit("LTC", "Litecoin", "crypto", "⚙️", "litecoin"),
    "DOT": Unit("DOT", "Polkadot", "crypto", "⚫", "polkadot"),
    "AVAX": Unit("AVAX", "Avalanche", "crypto", "🔺", "avalanche-2"),
    "NOT": Unit("NOT", "Notcoin", "crypto", "🐱", "notcoin"),
    "USDT": Unit("USDT", "Tether", "crypto", "💵", "tether"),
    "USDC": Unit("USDC", "USD Coin", "crypto", "💰", "usd-coin"),
    "SHIB": Unit("SHIB", "Shiba Inu", "crypto", "🐕", "shiba-inu"),
    "ATOM": Unit("ATOM", "Cosmos", "crypto", "⚛️", "cosmos"),
    "LINK": Unit("LINK", "Chainlink", "crypto", "🔗", "chainlink"),
    "USD": Unit("USD", "Доллары", "fiat", "💵"),
    "RUB": Unit("RUB", "Рубли", "fiat", "₽"),
    "STARS": Unit("STARS", "Telegram Stars", "stars", "⭐"),
}

ALIASES: dict[str, str] = {}


def _add(code: str, *words: str) -> None:
    for w in words:
        ALIASES[w] = code


_add("BTC", "btc", "bitcoin", "биткоин", "биткоина", "биткоину", "биткойн",
     "биткойна", "биток", "битка", "битку", "битков", "битки", "биткоинов")
_add("ETH", "eth", "ethereum", "эфир", "эфира", "эфиру", "эфириум",
     "эфириума", "эфиров", "эфириумов")
_add("TON", "ton", "toncoin", "тон", "тона", "тону", "тонов", "тонкоин",
     "тонкоина", "грам", "грамм", "грамма", "граммов", "грамов", "гр",
     "gram", "grams")
_add("SOL", "sol", "solana", "солана", "соланы", "солане", "соль", "соли", "солей")
_add("BNB", "bnb", "бнб", "бинанскоин", "бинанс", "binance coin")
_add("XRP", "xrp", "рипл", "риппл", "рипла", "риппла")
_add("DOGE", "doge", "dogecoin", "доге", "доги", "додж", "доджкоин", "доджкойн")
_add("ADA", "ada", "cardano", "кардано", "ада", "аду", "ады")
_add("TRX", "trx", "tron", "трон", "трона", "трону")
_add("LTC", "ltc", "litecoin", "лайткоин", "лайткойн", "лайт", "лайта")
_add("DOT", "dot", "polkadot", "полкадот", "полькадот")
_add("AVAX", "avax", "avalanche", "аваланч", "авах", "авакс")
_add("NOT", "not", "notcoin", "нот", "нотик", "нотка", "нотика")
_add("USDT", "usdt", "tether", "юсдт", "тезер", "тетер")
_add("USDC", "usdc", "юсдц", "юсдс")
_add("SHIB", "shib", "shiba", "шиба", "шибу", "шиб", "шибы")
_add("ATOM", "atom", "cosmos", "космос", "атома", "атомы")
_add("LINK", "link", "chainlink", "чейнлинк", "линк", "линка")

_add("RUB", "р", "руб", "рубль", "рубля", "рублей", "рублях", "rub", "₽", "рэ")
_add("USD", "$", "доллар", "доллара", "долларов", "usd", "бакс", "бакса", "баксов", "баксы")
_add("STARS", "звезда", "звезды", "звёзды", "звезд", "звёзд", "звездах", "звёздах",
     "star", "stars", "⭐", "старс", "старсов", "старсы")


def normalize(token: str) -> str:
    token = token.strip().lower().replace("ё", "е")
    return token.strip(" \t\n?!.,;:()\"'«»")


def resolve_unit(token: str) -> Optional[str]:
    norm = normalize(token)
    if not norm:
        return None
    if norm in ALIASES:
        return ALIASES[norm]
    # мягкие фолбэки: английское множественное число, "coin" на конце слова
    if norm.endswith("s") and len(norm) > 3 and norm[:-1] in ALIASES:
        return ALIASES[norm[:-1]]
    if norm.endswith("coin") and norm[:-4] in ALIASES:
        return ALIASES[norm[:-4]]
    return None


# ============================== ПАРСЕР ==============================
# Разбирает "1 грам", "2 биток", "100р", "0,5*2 eth", "10к рублей" в
# (сумма, код_валюты). Арифметика в сумме считается безопасно через ast,
# без eval() от греха подальше.

class ParseError(Exception):
    pass


_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}

_MULTIPLIERS = {"кк": 1_000_000, "тыс": 1_000, "млн": 1_000_000,
                 "лям": 1_000_000, "к": 1_000, "k": 1_000, "m": 1_000_000}
_MULT_PATTERN = "|".join(sorted((re.escape(k) for k in _MULTIPLIERS), key=len, reverse=True))

_INPUT_RE = re.compile(
    rf"^\s*(?P<amount>[\d.,+\-*/()\s]+?)\s*(?P<mult>{_MULT_PATTERN})?\s*(?P<unit>[^\d]*)\s*$",
    re.IGNORECASE,
)
_PREFIX_SYMBOL_RE = re.compile(r"^\s*([$₽⭐])\s*([\d.,+\-*/()\s]+)\s*$")


@dataclass
class ParsedQuery:
    amount: float
    unit_code: str


def _safe_eval_number(expr: str) -> float:
    expr = expr.strip().replace(",", ".").replace(" ", "")
    if not expr:
        raise ParseError("empty_amount")
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError as exc:
        raise ParseError("bad_amount") from exc

    def _ev(n: ast.AST) -> float:
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(n.op)](_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(n.op)](_ev(n.operand))
        raise ParseError("bad_amount")

    try:
        return _ev(node)
    except ZeroDivisionError as exc:
        raise ParseError("division_by_zero") from exc


def parse_query(text: str) -> ParsedQuery:
    text = (text or "").strip()
    if not text:
        raise ParseError("empty")

    m = _PREFIX_SYMBOL_RE.match(text)
    if m:
        symbol, amount_part = m.groups()
        unit_code = resolve_unit(symbol)
        if unit_code:
            return ParsedQuery(_safe_eval_number(amount_part), unit_code)

    if re.fullmatch(r"[^\d]+", text):
        unit_code = resolve_unit(text)
        if unit_code:
            return ParsedQuery(1.0, unit_code)
        raise ParseError("unknown_unit")

    m = _INPUT_RE.match(text)
    if not m:
        raise ParseError("cant_parse")

    amount_raw = m.group("amount")
    mult_raw = m.group("mult")
    unit_raw = (m.group("unit") or "").strip()
    if not unit_raw:
        raise ParseError("missing_unit")

    amount = _safe_eval_number(amount_raw)
    if mult_raw:
        amount *= _MULTIPLIERS[mult_raw.lower()]
    if amount <= 0:
        raise ParseError("non_positive_amount")

    unit_code = resolve_unit(unit_raw)
    if not unit_code:
        raise ParseError("unknown_unit")
    return ParsedQuery(amount, unit_code)


# ============================== ЦЕНЫ (CoinGecko) ==============================
# Фоновый кэш: инлайн-запросы в Telegram прилетают почти на каждое нажатие
# клавиши, поэтому дёргать CoinGecko на каждый запрос — верный способ
# упереться в лимит. Раз в PRICE_REFRESH_SECONDS бот сам обновляет цены в
# фоне, обработчики читают готовый кэш — это быстро и бережно к API.

class PriceUnavailable(Exception):
    pass


class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, dict[str, float]] = {}
        self._updated_at = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None

    def get(self) -> dict[str, dict[str, float]]:
        return self._prices

    def is_stale(self) -> bool:
        if not self._updated_at:
            return True
        return (time.monotonic() - self._updated_at) > PRICE_CACHE_TTL_SECONDS

    async def _fetch_once(self) -> None:
        ids = ",".join(sorted({u.coingecko_id for u in UNITS.values() if u.coingecko_id}))
        params = {"ids": ids, "vs_currencies": "usd,rub"}
        headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
        assert self._session is not None
        async with self._session.get(
            f"{COINGECKO_BASE}/simple/price",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if data:
            self._prices = data
            self._updated_at = time.monotonic()
            logger.info("Цены обновлены: %d монет", len(data))

    async def _loop(self) -> None:
        while True:
            try:
                await self._fetch_once()
            except Exception:
                logger.exception("Не удалось обновить цены с CoinGecko")
            await asyncio.sleep(PRICE_REFRESH_SECONDS)

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            await self._fetch_once()
        except Exception:
            logger.exception("Первая загрузка цен не удалась, продолжаю в фоне")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()


# ============================== КОНВЕРТАЦИЯ ==============================

@dataclass
class ConversionResult:
    input_amount: float
    input_code: str
    usd: float
    rub: float
    stars: float
    ton: float


def _usd_rub_rate(prices: dict[str, dict[str, float]]) -> float:
    """USD/RUB считаем через USDT — это ближе к реальному курсу доллара,
    который видит крипто-пользователь на биржах, чем усреднённый форекс."""
    tether = prices.get(UNITS["USDT"].coingecko_id)
    if not tether or not tether.get("usd") or "rub" not in tether:
        raise PriceUnavailable("usd_rub_rate")
    return tether["rub"] / tether["usd"]


def convert(amount: float, code: str, prices: dict[str, dict[str, float]]) -> ConversionResult:
    unit = UNITS[code]
    usd_rub = _usd_rub_rate(prices)

    ton_coin = prices.get(UNITS["TON"].coingecko_id)
    if not ton_coin or "usd" not in ton_coin:
        raise PriceUnavailable("ton")
    ton_usd_price = ton_coin["usd"]

    if unit.kind == "crypto":
        coin = prices.get(unit.coingecko_id)
        if not coin or "usd" not in coin:
            raise PriceUnavailable(code)
        usd_value = amount * coin["usd"]
    elif code == "USD":
        usd_value = amount
    elif code == "RUB":
        usd_value = amount / usd_rub
    elif code == "STARS":
        usd_value = amount * STARS_USD_RATE
    else:  # pragma: no cover — защитный случай, сюда не должны попадать
        raise ValueError(code)

    return ConversionResult(
        input_amount=amount,
        input_code=code,
        usd=usd_value,
        rub=usd_value * usd_rub,
        stars=usd_value / STARS_USD_RATE,
        ton=usd_value / ton_usd_price,
    )


# ============================== ФОРМАТИРОВАНИЕ ==============================

def _fmt(value: float, decimal_sep: str = ".") -> str:
    if value == 0:
        return f"0{decimal_sep}00"
    if abs(value) >= 1:
        s = f"{value:,.2f}"
    else:
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        if not s or s == "-":
            s = "0"
    s = s.replace(",", "\u00A0")
    if decimal_sep != ".":
        s = s.replace(".", decimal_sep)
    return s


def fmt_usd(v: float) -> str:
    return f"${_fmt(v)}"


def fmt_rub(v: float) -> str:
    return f"{_fmt(v, ',')} ₽"


def fmt_ton(v: float) -> str:
    return f"{_fmt(v)} TON"


def fmt_stars(v: float) -> str:
    if v < 1:
        return f"{v:.2f} ⭐"
    return f"{round(v):,} ⭐".replace(",", "\u00A0")


def _rows(r: ConversionResult) -> list[tuple[str, str, str]]:
    return [
        ("💵", "USD", fmt_usd(r.usd)),
        ("₽", "RUB", fmt_rub(r.rub)),
        ("⭐", "Stars", fmt_stars(r.stars)),
        ("💎", "TON", fmt_ton(r.ton)),
    ]


def build_message(r: ConversionResult, stale: bool) -> str:
    unit = UNITS[r.input_code]
    header = f"{unit.emoji} <b>{_fmt(r.input_amount)} {unit.display}</b>"
    rows = _rows(r)
    w = max(len(label) for _, label, _ in rows)
    table = "\n".join(f"{e} {l.ljust(w)}  {v}" for e, l, v in rows)
    footer = f"Источник: CoinGecko · Stars ≈ ${STARS_USD_RATE:.3f}"
    if stale:
        footer += " · курс мог немного устареть"
    return f"{header}\n\n<pre>{table}</pre>\n<i>{footer}</i>"


def build_title(r: ConversionResult) -> str:
    unit = UNITS[r.input_code]
    return f"{unit.emoji} {_fmt(r.input_amount)} {unit.code} → $ / ₽ / ⭐ / TON"


def build_description(r: ConversionResult) -> str:
    return " · ".join(v for _, _, v in _rows(r))


HELP_TEXT = (
    "Напиши сумму и валюту, например:\n"
    "<code>1 gram</code> · <code>2 биток</code> · <code>0.5 sol</code> · "
    "<code>100р</code> · <code>50 звёзд</code> · <code>10 usdt</code>\n\n"
    "Всегда покажу в 4 валютах: USD, RUB, Stars и TON."
)


# ============================== ОБРАБОТЧИКИ ==============================

router = Router()


def _rid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _text_result(rid: str, title: str, desc: str, text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=rid,
        title=title,
        description=desc,
        input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML"),
    )


@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery, cache: PriceCache) -> None:
    query = inline_query.query.strip()

    if not query:
        await inline_query.answer(
            [_text_result("help", "💱 Курс крипты, рублей, долларов и Stars",
                           "1 gram · 2 биток · 0.5 sol · 100р · 50 звёзд", HELP_TEXT)],
            cache_time=300, is_personal=False,
        )
        return

    try:
        parsed = parse_query(query)
    except ParseError:
        await inline_query.answer(
            [_text_result(_rid("err", query), "🤔 Не понял сумму или валюту",
                           f"Не смог разобрать «{query}»",
                           f"Не смог разобрать «{query}».\n\n{HELP_TEXT}")],
            cache_time=5, is_personal=False,
        )
        return

    prices = cache.get()
    if not prices:
        await inline_query.answer(
            [_text_result("loading", "⏳ Загружаю курсы…", "Ещё не получил данные от CoinGecko",
                           "Бот ещё не получил курсы от CoinGecko — попробуй через пару секунд.")],
            cache_time=1, is_personal=False,
        )
        return

    try:
        result = convert(parsed.amount, parsed.unit_code, prices)
    except PriceUnavailable:
        await inline_query.answer(
            [_text_result(_rid("unavail", query), "⚠️ Нет данных по этой монете",
                           "Курс сейчас недоступен",
                           "Курс для этой валюты сейчас недоступен, попробуй чуть позже.")],
            cache_time=5, is_personal=False,
        )
        return

    html = build_message(result, cache.is_stale())
    article = InlineQueryResultArticle(
        id=_rid("ok", query),
        title=build_title(result),
        description=build_description(result),
        input_message_content=InputTextMessageContent(message_text=html, parse_mode="HTML"),
    )
    await inline_query.answer([article], cache_time=15, is_personal=False)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    me = await message.bot.get_me()
    await message.answer(
        f"Привет! Показываю курс крипты, рублей и долларов сразу в Stars.\n"
        f"Работаю в инлайн-режиме — набери в любом чате "
        f"<code>@{me.username} 1 btc</code>.\n\n{HELP_TEXT}"
    )


@router.message()
async def handle_plain_message(message: Message, cache: PriceCache) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    try:
        parsed = parse_query(text)
    except ParseError:
        return  # в личке не спамим на каждое непонятное сообщение
    prices = cache.get()
    if not prices:
        await message.answer("Ещё загружаю курсы, дай секунду и повтори.")
        return
    try:
        result = convert(parsed.amount, parsed.unit_code, prices)
    except PriceUnavailable:
        await message.answer("Курс для этой валюты сейчас недоступен.")
        return
    await message.answer(build_message(result, cache.is_stale()))


# ============================== ТОЧКА ВХОДА ==============================

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден BOT_TOKEN. Установи переменную окружения BOT_TOKEN "
            "(токен от @BotFather) перед запуском — см. докстринг в начале файла."
        )
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    cache = PriceCache()
    await cache.start()
    logger.info("Кэш цен запущен, стартуем polling")
    try:
        await dp.start_polling(bot, cache=cache)
    finally:
        await cache.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())