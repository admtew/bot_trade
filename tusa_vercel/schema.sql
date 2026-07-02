-- Схема для Neon Postgres. Выполни один раз в Neon SQL Editor.

CREATE TABLE IF NOT EXISTS users (
    tg_id      BIGINT PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          SERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at   BIGINT NOT NULL,
    area        TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    price_text  TEXT NOT NULL DEFAULT '',
    pay_url     TEXT NOT NULL DEFAULT '',
    capacity    INTEGER NOT NULL DEFAULT 0,
    refs_needed INTEGER NOT NULL DEFAULT 0,
    channel     TEXT NOT NULL DEFAULT '',
    age_limit   TEXT NOT NULL DEFAULT '',
    cover       TEXT NOT NULL DEFAULT 'ember',
    city        TEXT NOT NULL DEFAULT 'Москва',
    genre       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    code       TEXT PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES events(id),
    user_id    BIGINT NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    rem24_sent INTEGER NOT NULL DEFAULT 0,
    rem3_sent  INTEGER NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    used_at    BIGINT,
    UNIQUE(event_id, user_id)
);

CREATE TABLE IF NOT EXISTS referrals (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    referrer_id BIGINT NOT NULL,
    referred_id BIGINT NOT NULL,
    created_at  BIGINT NOT NULL,
    UNIQUE(event_id, referred_id)
);

CREATE INDEX IF NOT EXISTS idx_events_active ON events(status, starts_at);
CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id);
