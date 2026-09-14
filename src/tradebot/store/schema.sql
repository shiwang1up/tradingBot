CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  mode        TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  ended_at    INTEGER,
  config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
  symbol   TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  interval INTEGER NOT NULL,
  o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
  v INTEGER NOT NULL,
  source   TEXT NOT NULL DEFAULT 'official',
  PRIMARY KEY (symbol, ts, interval)
);

CREATE TABLE IF NOT EXISTS signals (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT NOT NULL,
  strategy  TEXT NOT NULL,
  symbol    TEXT NOT NULL,
  bar_ts    INTEGER NOT NULL,
  direction TEXT NOT NULL,
  entry     REAL NOT NULL,
  stop      REAL NOT NULL,
  target    REAL,
  product   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT NOT NULL,
  signal_id INTEGER NOT NULL,
  approved  INTEGER NOT NULL,
  reason    TEXT NOT NULL,
  quantity  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_decisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL,
  signal_id   INTEGER NOT NULL,
  filter_kind TEXT NOT NULL,
  approved    INTEGER NOT NULL,
  reason      TEXT NOT NULL,
  confidence  REAL NOT NULL,
  latency_ms  INTEGER NOT NULL,
  failure     TEXT
);

CREATE TABLE IF NOT EXISTS ai_cache (
  symbol        TEXT NOT NULL,
  bar_ts        INTEGER NOT NULL,
  prompt_hash   TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  PRIMARY KEY (symbol, bar_ts, prompt_hash)
);

CREATE TABLE IF NOT EXISTS orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL,
  client_id       TEXT NOT NULL,
  signal_id       INTEGER,
  broker_order_id TEXT,
  kind            TEXT NOT NULL,   -- ENTRY | EXIT | SQUARE_OFF
  side            TEXT NOT NULL,   -- BUY | SELL
  qty             INTEGER NOT NULL,
  limit_price     REAL,
  status          TEXT NOT NULL,   -- PENDING | FILLED | PARTIAL | UNFILLED | CANCELLED | FAILED
  placed_at       INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  UNIQUE (run_id, client_id)
);

CREATE TABLE IF NOT EXISTS fills (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  qty      INTEGER NOT NULL,
  price    REAL NOT NULL,
  ts       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  product       TEXT NOT NULL,
  direction     TEXT NOT NULL,
  strategy      TEXT NOT NULL,
  client_id     TEXT NOT NULL,
  qty           INTEGER NOT NULL,
  avg_price     REAL NOT NULL,
  stop          REAL NOT NULL,
  target        REAL,
  exit_ids_json TEXT NOT NULL DEFAULT '[]',
  opened_at     INTEGER NOT NULL,
  closed_at     INTEGER,
  exit_price    REAL,
  exit_reason   TEXT,
  pnl           REAL,
  fill_status   TEXT NOT NULL DEFAULT 'full',
  adopted       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_pnl (
  run_id         TEXT NOT NULL,
  date           TEXT NOT NULL,
  realised       REAL NOT NULL,
  unrealised     REAL NOT NULL,
  fills          INTEGER NOT NULL,
  entries_placed INTEGER NOT NULL,
  fill_rate      REAL NOT NULL,
  PRIMARY KEY (run_id, date)
);
