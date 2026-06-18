-- LoL T2 Prediction Model — SQLite Schema

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    player_name TEXT NOT NULL UNIQUE,
    role TEXT,
    team TEXT,
    region TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    summoner_name TEXT,
    server TEXT,
    rank_tier TEXT,
    lp INTEGER,
    soloq_rating REAL,
    snapshot_date TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    team_name TEXT NOT NULL UNIQUE,
    region TEXT,
    league TEXT,
    pro_elo REAL DEFAULT 1500.0,
    games_played INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    gameid TEXT UNIQUE,
    date TEXT,
    league TEXT,
    patch TEXT,
    playoffs INTEGER,
    blue_team TEXT,
    red_team TEXT,
    winner TEXT,
    gamelength INTEGER
);

CREATE TABLE IF NOT EXISTS rosters (
    id INTEGER PRIMARY KEY,
    team TEXT,
    player_name TEXT,
    role TEXT,
    snapshot_date TEXT,
    tournament TEXT
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    team_a TEXT,
    team_b TEXT,
    bet_team TEXT,
    side TEXT,
    amount REAL,
    entry_price REAL,
    model_prob REAL,
    edge REAL,
    kelly_fraction REAL,
    entry_time TEXT,
    market_url TEXT,
    exit_price REAL,
    match_winner TEXT,
    profit_loss REAL,
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS paper_portfolio (
    id INTEGER PRIMARY KEY,
    date TEXT UNIQUE,
    bankroll REAL,
    daily_pnl REAL,
    open_positions INTEGER,
    total_bets INTEGER,
    wins INTEGER,
    losses INTEGER
);

CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(date);
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league);
CREATE INDEX IF NOT EXISTS idx_accounts_player ON accounts(player_id);
CREATE INDEX IF NOT EXISTS idx_accounts_snapshot ON accounts(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_rosters_team ON rosters(team, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_market ON paper_trades(market_id);
