"""
Tusa — единая serverless-функция для Vercel.
Обслуживает: API Mini App, Telegram webhook, cron-напоминания.
База: Neon Postgres. Telegram: прямые вызовы Bot API через requests.
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
CRON_SECRET = os.environ.get("CRON_SECRET", "changeme")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
NEW_ID_THRESHOLD = int(os.environ.get("NEW_ID_THRESHOLD", "8500000000"))
ADDRESS_REVEAL_HOURS = int(os.environ.get("ADDRESS_REVEAL_HOURS", "3"))
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ───────────────────── DB ─────────────────────

def db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def q(sql, args=(), one=False, write=False):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        if write:
            row = cur.fetchone() if cur.description else None
            return row
        if one:
            return cur.fetchone()
        return cur.fetchall()
    finally:
        conn.close()


def now():
    return int(time.time())


def upsert_user(tg_id, username, first_name):
    q(
        """INSERT INTO users(tg_id, username, first_name, created_at)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(tg_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
        (tg_id, username or "", first_name or "", now()), write=True,
    )


def get_user(tg_id):
    return q("SELECT * FROM users WHERE tg_id=%s", (tg_id,), one=True)


def tickets_count(event_id):
    r = q("SELECT COUNT(*) AS c FROM tickets WHERE event_id=%s AND status!='revoked' AND kind!='paid_pending'",
          (event_id,), one=True)
    return r["c"]


# ───────────────────── Telegram helpers ─────────────────────

def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=10)
        return r.json()
    except Exception:
        return {"ok": False}


def bot_username():
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    r = tg("getMe")
    BOT_USERNAME = (r.get("result") or {}).get("username", "")
    return BOT_USERNAME


def is_subscribed(channel, user_id):
    if not channel:
        return True
    r = tg("getChatMember", chat_id=f"@{channel}", user_id=user_id)
    if not r.get("ok"):
        return True  # бот не админ канала — не блокируем
    status = (r.get("result") or {}).get("status")
    return status in ("member", "administrator", "creator")


# ───────────────────── initData (подпись Telegram) ─────────────────────

def validate_init_data(init_data):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


def current_user():
    user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if user:
        upsert_user(user["id"], user.get("username"), user.get("first_name"))
    return user


# ───────────────────── сериализация ─────────────────────

def event_json(e, me_id=None):
    taken = tickets_count(e["id"])
    org = get_user(e["org_id"])
    return {
        "id": e["id"], "title": e["title"], "description": e["description"],
        "starts_at": e["starts_at"], "area": e["area"],
        "price_text": e["price_text"], "pay_url": e["pay_url"],
        "capacity": e["capacity"], "refs_needed": e["refs_needed"],
        "channel": e["channel"], "age_limit": e["age_limit"],
        "cover": e["cover"], "city": e["city"], "genre": e["genre"],
        "taken": taken,
        "sold_out": bool(e["capacity"] and taken >= e["capacity"]),
        "is_mine": me_id == e["org_id"],
        "host": (org["username"] or org["first_name"] or "host") if org else "host",
    }


# ───────────────────── API: лента / города / мета ─────────────────────

@app.route("/api/events")
def h_events():
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    city = request.args.get("city") or None
    sql = "SELECT * FROM events WHERE status='active' AND starts_at > %s"
    args = [now() - 6 * 3600]
    if city:
        sql += " AND city=%s"
        args.append(city)
    sql += " ORDER BY starts_at ASC"
    rows = q(sql, tuple(args))
    return jsonify([event_json(e, me["id"]) for e in rows])


@app.route("/api/cities")
def h_cities():
    if not current_user():
        return jsonify({"error": "unauthorized"}), 401
    rows = q("SELECT city, COUNT(*) AS c FROM events WHERE status='active' AND starts_at > %s GROUP BY city",
             (now() - 6 * 3600,))
    return jsonify({r["city"]: r["c"] for r in rows})


@app.route("/api/meta")
def h_meta():
    if not current_user():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"bot": bot_username()})


@app.route("/api/events/<int:eid>")
def h_event(eid):
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    e = q("SELECT * FROM events WHERE id=%s", (eid,), one=True)
    if not e:
        return jsonify({"error": "not_found"}), 404
    data = event_json(e, me["id"])
    t = q("SELECT * FROM tickets WHERE event_id=%s AND user_id=%s", (eid, me["id"]), one=True)
    data["my_ticket"] = {"code": t["code"], "kind": t["kind"], "status": t["status"]} if t else None
    refs = q("SELECT * FROM referrals WHERE event_id=%s AND referrer_id=%s", (eid, me["id"]))
    data["my_refs"] = len(refs)
    data["subscribed"] = is_subscribed(e["channel"], me["id"])
    bu = bot_username()
    data["ref_link"] = f"https://t.me/{bu}?start=ref_{eid}_{me['id']}"
    data["share_link"] = f"https://t.me/{bu}?start=evt_{eid}"
    return jsonify(data)


@app.route("/api/events", methods=["POST"])
def h_create_event():
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    b = request.get_json(force=True, silent=True) or {}
    if not b.get("title") or not b.get("starts_at"):
        return jsonify({"error": "title и дата обязательны"}), 400
    if len(b.get("title", "")) > 80:
        return jsonify({"error": "слишком длинное название"}), 400
    row = q(
        """INSERT INTO events(org_id,title,description,starts_at,area,address,price_text,pay_url,
           capacity,refs_needed,channel,age_limit,cover,city,genre,created_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (me["id"], b["title"], b.get("description", ""), int(b["starts_at"]),
         b.get("area", ""), b.get("address", ""), b.get("price_text", ""), b.get("pay_url", ""),
         int(b.get("capacity") or 0), int(b.get("refs_needed") or 0),
         (b.get("channel", "") or "").lstrip("@"), b.get("age_limit", ""),
         str(b.get("cover") or "ember"), str(b.get("city") or "Москва"), b.get("genre", ""), now()),
        write=True,
    )
    return jsonify({"id": row["id"]})


# ───────────────────── API: билеты ─────────────────────

@app.route("/api/events/<int:eid>/claim_free", methods=["POST"])
def h_claim_free(eid):
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    e = q("SELECT * FROM events WHERE id=%s", (eid,), one=True)
    if not e or e["status"] != "active":
        return jsonify({"error": "Туса не найдена"}), 404
    if q("SELECT 1 FROM tickets WHERE event_id=%s AND user_id=%s", (eid, me["id"]), one=True):
        return jsonify({"error": "У тебя уже есть билет 😉"}), 400
    if e["capacity"] and tickets_count(eid) >= e["capacity"]:
        return jsonify({"error": "Мест больше нет 😢"}), 400
    if not is_subscribed(e["channel"], me["id"]):
        return jsonify({"error": f"Сначала подпишись на @{e['channel']}", "need": "subscribe"}), 400
    refs = q("SELECT * FROM referrals WHERE event_id=%s AND referrer_id=%s", (eid, me["id"]))
    valid = sum(1 for r in refs if is_subscribed(e["channel"], r["referred_id"]))
    if valid < e["refs_needed"]:
        return jsonify({"error": f"Приведи ещё {e['refs_needed'] - valid} друз.", "need": "refs",
                        "have": valid, "needed": e["refs_needed"]}), 400
    code = uuid.uuid4().hex
    q("INSERT INTO tickets(code,event_id,user_id,kind,created_at) VALUES(%s,%s,%s,'free',%s)",
      (code, eid, me["id"], now()), write=True)
    return jsonify({"code": code})


@app.route("/api/events/<int:eid>/claim_paid", methods=["POST"])
def h_claim_paid(eid):
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    e = q("SELECT * FROM events WHERE id=%s", (eid,), one=True)
    if not e or e["status"] != "active":
        return jsonify({"error": "Туса не найдена"}), 404
    if q("SELECT 1 FROM tickets WHERE event_id=%s AND user_id=%s", (eid, me["id"]), one=True):
        return jsonify({"error": "Заявка уже есть"}), 400
    code = uuid.uuid4().hex
    q("INSERT INTO tickets(code,event_id,user_id,kind,created_at) VALUES(%s,%s,%s,'paid_pending',%s)",
      (code, eid, me["id"], now()), write=True)
    return jsonify({"code": code, "pending": True})


@app.route("/api/me/tickets")
def h_my_tickets():
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    rows = q(
        """SELECT t.*, e.title, e.starts_at, e.area, e.address, e.age_limit, e.cover
           FROM tickets t JOIN events e ON e.id=t.event_id
           WHERE t.user_id=%s AND t.status!='revoked' AND e.status='active'
           ORDER BY e.starts_at ASC""", (me["id"],))
    out = []
    for t in rows:
        reveal = t["starts_at"] - time.time() <= ADDRESS_REVEAL_HOURS * 3600
        out.append({
            "code": t["code"], "kind": t["kind"], "status": t["status"],
            "title": t["title"], "starts_at": t["starts_at"], "area": t["area"],
            "age_limit": t["age_limit"], "cover": t["cover"],
            "address": t["address"] if reveal else None,
        })
    return jsonify(out)


@app.route("/api/me/events")
def h_my_events():
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    rows = q("SELECT * FROM events WHERE org_id=%s ORDER BY starts_at DESC", (me["id"],))
    return jsonify([event_json(e, me["id"]) for e in rows])


@app.route("/api/events/<int:eid>/guests")
def h_guests(eid):
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    e = q("SELECT * FROM events WHERE id=%s", (eid,), one=True)
    if not e or e["org_id"] != me["id"]:
        return jsonify({"error": "forbidden"}), 403
    rows = q(
        """SELECT t.*, u.username, u.first_name FROM tickets t JOIN users u ON u.tg_id=t.user_id
           WHERE t.event_id=%s ORDER BY t.created_at ASC""", (eid,))
    return jsonify([{"code": g["code"], "kind": g["kind"], "status": g["status"],
                     "name": g["first_name"], "username": g["username"]} for g in rows])


@app.route("/api/approve", methods=["POST"])
def h_approve():
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    b = request.get_json(force=True, silent=True) or {}
    t = q("""SELECT t.*, e.org_id, e.title FROM tickets t JOIN events e ON e.id=t.event_id
             WHERE t.code=%s""", (b.get("code", ""),), one=True)
    if not t or t["org_id"] != me["id"]:
        return jsonify({"error": "forbidden"}), 403
    q("UPDATE tickets SET kind='paid' WHERE code=%s AND kind='paid_pending'", (t["code"],), write=True)
    tg("sendMessage", chat_id=t["user_id"],
       text=f"Оплата подтверждена — билет на «{t['title']}» у тебя! 🎟 Смотри вкладку «Билеты».")
    return jsonify({"ok": True})


@app.route("/api/scan", methods=["POST"])
def h_scan():
    me = current_user()
    if not me:
        return jsonify({"error": "unauthorized"}), 401
    b = request.get_json(force=True, silent=True) or {}
    t = q("""SELECT t.*, e.org_id, e.title, u.username, u.first_name
             FROM tickets t JOIN events e ON e.id=t.event_id JOIN users u ON u.tg_id=t.user_id
             WHERE t.code=%s""", (b.get("code", ""),), one=True)
    if not t:
        return jsonify({"ok": False, "msg": "Билет не найден ❌"})
    if t["org_id"] != me["id"]:
        return jsonify({"ok": False, "msg": "Это билет не на твою тусу"})
    name = t["first_name"] + (f" (@{t['username']})" if t["username"] else "")
    if t["kind"] == "paid_pending":
        return jsonify({"ok": False, "msg": f"{name}: оплата НЕ подтверждена ⚠️"})
    if t["status"] == "used":
        return jsonify({"ok": False, "msg": f"{name}: билет УЖЕ использован ⚠️"})
    if t["status"] != "active":
        return jsonify({"ok": False, "msg": "Билет недействителен ❌"})
    q("UPDATE tickets SET status='used', used_at=%s WHERE code=%s", (now(), t["code"]), write=True)
    return jsonify({"ok": True, "msg": f"✅ {name} — проходит!"})


# ───────────────────── Telegram webhook ─────────────────────

def webapp_kb(path="", text="Открыть тусы 🎉"):
    url = WEBAPP_URL + (f"#{path}" if path else "")
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": url}}]]}


@app.route("/api/webhook", methods=["POST"])
def h_webhook():
    update = request.get_json(force=True, silent=True) or {}
    msg = update.get("message")
    if not msg or not msg.get("text"):
        return Response("ok")
    chat_id = msg["chat"]["id"]
    frm = msg.get("from", {})
    upsert_user(frm.get("id"), frm.get("username"), frm.get("first_name"))
    text = msg["text"].strip()
    name = frm.get("first_name", "")

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if payload.startswith("ref_"):
            try:
                _, eid, rid = payload.split("_", 2)
                eid, rid = int(eid), int(rid)
            except ValueError:
                eid = rid = 0
            e = q("SELECT * FROM events WHERE id=%s", (eid,), one=True) if eid else None
            if not e:
                tg("sendMessage", chat_id=chat_id, text="Этой тусы уже нет 😢 Но есть другие:",
                   reply_markup=webapp_kb())
                return Response("ok")
            is_new = not get_user(frm.get("id")) or get_user(frm.get("id"))["created_at"] >= now() - 5
            counted = False
            if frm.get("id") <= NEW_ID_THRESHOLD and rid != frm.get("id"):
                try:
                    q("INSERT INTO referrals(event_id,referrer_id,referred_id,created_at) VALUES(%s,%s,%s,%s)",
                      (eid, rid, frm.get("id"), now()), write=True)
                    counted = True
                except Exception:
                    counted = False
            txt = f"Привет, {name}! Тебя позвали на «{e['title']}» 🎉\nЖми кнопку — забирай билет."
            if counted:
                txt += "\n\nДруг стал на шаг ближе к free-проходке 🔥"
            tg("sendMessage", chat_id=chat_id, text=txt, reply_markup=webapp_kb(f"event/{eid}"))
            return Response("ok")
        if payload.startswith("evt_"):
            try:
                eid = int(payload[4:])
            except ValueError:
                eid = 0
            if eid and q("SELECT 1 FROM events WHERE id=%s", (eid,), one=True):
                tg("sendMessage", chat_id=chat_id, text="Вот эта туса 👇",
                   reply_markup=webapp_kb(f"event/{eid}"))
                return Response("ok")
        tg("sendMessage", chat_id=chat_id,
           text=f"Привет, {name}! 🎉\nВсе тусы города — в одном месте. Организатор? Создай свою прямо в приложении.",
           reply_markup=webapp_kb())
        return Response("ok")

    if text.startswith("/help"):
        tg("sendMessage", chat_id=chat_id,
           text="🎟 Гостям — открой приложение, выбери тусу, забери билет (QR на входе).\n"
                "🪩 Организаторам — кнопка «Создать»: афиша, free за друзей, гостевой список, сканер.",
           reply_markup=webapp_kb())
        return Response("ok")

    tg("sendMessage", chat_id=chat_id, text="Все тусы — в приложении 👇", reply_markup=webapp_kb())
    return Response("ok")


# ───────────────────── Cron: напоминания ─────────────────────

def _cron_authed():
    """Доступ по ?key=, либо по заголовку Authorization: Bearer (так шлёт Vercel Cron)."""
    if request.args.get("key") == CRON_SECRET:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {CRON_SECRET}"


@app.route("/api/cron")
def h_cron():
    if not _cron_authed():
        return Response("forbidden", status=403)
    sent = {"r24": 0, "r3": 0}
    # за сутки
    rows = q("""SELECT t.*, e.title, e.starts_at, e.area FROM tickets t JOIN events e ON e.id=t.event_id
                WHERE e.status='active' AND t.status='active' AND t.kind!='paid_pending'
                  AND t.rem24_sent=0 AND e.starts_at <= %s AND e.starts_at > %s""",
             (now() + 24 * 3600, now()))
    for t in rows:
        import datetime
        when = datetime.datetime.fromtimestamp(t["starts_at"]).strftime("%d.%m в %H:%M")
        tg("sendMessage", chat_id=t["user_id"],
           text=f"Напоминаю: завтра туса! 🎉\n{t['title']}\n{when}, {t['area']}\n\nБилет — в приложении.",
           reply_markup=webapp_kb("tickets", "Мой билет 🎟"))
        q("UPDATE tickets SET rem24_sent=1 WHERE code=%s", (t["code"],), write=True)
        sent["r24"] += 1
    # незадолго до начала — адрес
    rows = q("""SELECT t.*, e.title, e.starts_at, e.area, e.address FROM tickets t JOIN events e ON e.id=t.event_id
                WHERE e.status='active' AND t.status='active' AND t.kind!='paid_pending'
                  AND t.rem3_sent=0 AND e.starts_at <= %s AND e.starts_at > %s""",
             (now() + ADDRESS_REVEAL_HOURS * 3600, now()))
    for t in rows:
        import datetime
        when = datetime.datetime.fromtimestamp(t["starts_at"]).strftime("%H:%M")
        addr = t["address"] or t["area"]
        tg("sendMessage", chat_id=t["user_id"],
           text=f"Сегодня! {t['title']} в {when} 🔥\n📍 Адрес: {addr}\n\nПокажи QR на входе. До встречи!",
           reply_markup=webapp_kb("tickets", "Мой билет 🎟"))
        q("UPDATE tickets SET rem3_sent=1 WHERE code=%s", (t["code"],), write=True)
        sent["r3"] += 1
    return jsonify(sent)


# ───────────────────── Setup: webhook + кнопка меню ─────────────────────

@app.route("/api/setup")
def h_setup():
    if not _cron_authed():
        return Response("forbidden", status=403)
    wh = tg("setWebhook", url=f"{WEBAPP_URL}/api/webhook", allowed_updates=["message"])
    mb = tg("setChatMenuButton",
            menu_button={"type": "web_app", "text": "Тусы 🎉", "web_app": {"url": WEBAPP_URL}})
    return jsonify({"webhook": wh, "menu_button": mb, "bot": bot_username()})


@app.route("/api/health")
def h_health():
    return jsonify({"ok": True})
