-- LoL T2 Match / Draft Data — SQLite Schema (match_data.db)
--
-- Source: Oracle's Elixir annual match CSVs, filtered to Tier-2 leagues.
-- Granularity:
--   games        → one row per game   (metadata + patch + outcome)
--   draft_picks  → one row per player per game (champion + role)
--   team_pickbans→ one row per team   per game (full pick/ban draft order)
--   patches      → one row per patch   (reference summary, derived)
--
-- Patch lives once per game on `games`. Per-player draft rows reference the
-- game by `gameid`, so patch is never duplicated onto every pick row — join
-- draft_picks.gameid -> games.gameid to get the patch for a pick.

-- ---------------------------------------------------------------------------
-- One row per game: metadata, patch, and collapsed outcome.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    id               INTEGER PRIMARY KEY,
    gameid           TEXT UNIQUE NOT NULL,
    date             TEXT,
    league           TEXT,
    year             INTEGER,
    split            TEXT,
    playoffs         INTEGER,
    patch            TEXT,
    blue_team        TEXT,
    red_team         TEXT,
    winner           TEXT,            -- 'blue' | 'red'
    gamelength       INTEGER,         -- seconds
    datacompleteness TEXT             -- OE flag: 'complete' | 'partial' | 'ignore'
);

-- ---------------------------------------------------------------------------
-- One row per player per game: which champion they drafted, in which role.
-- This is the core "every champ each player drafted + their role" table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS draft_picks (
    id         INTEGER PRIMARY KEY,
    gameid     TEXT NOT NULL,
    side       TEXT,                  -- 'Blue' | 'Red'
    position   TEXT,                  -- top | jng | mid | bot | sup
    playername TEXT,
    playerid   TEXT,                  -- OE stable player id (nullable)
    teamname   TEXT,
    champion   TEXT,
    result     INTEGER,               -- 1 = won the game, 0 = lost
    UNIQUE (gameid, side, position),
    FOREIGN KEY (gameid) REFERENCES games(gameid)
);

-- ---------------------------------------------------------------------------
-- One row per team per game: full draft order.
--   pick1..pick5 are in the order that team picked.
--   ban1..ban5  are in the order that team banned.
-- Preserves draft sequence that draft_picks (role-indexed) does not.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS team_pickbans (
    id         INTEGER PRIMARY KEY,
    gameid     TEXT NOT NULL,
    side       TEXT,                  -- 'Blue' | 'Red'
    teamname   TEXT,
    first_pick INTEGER,               -- 1 if this team had first pick
    ban1 TEXT, ban2 TEXT, ban3 TEXT, ban4 TEXT, ban5 TEXT,
    pick1 TEXT, pick2 TEXT, pick3 TEXT, pick4 TEXT, pick5 TEXT,
    UNIQUE (gameid, side),
    FOREIGN KEY (gameid) REFERENCES games(gameid)
);

-- ---------------------------------------------------------------------------
-- Derived patch reference: rebuilt from games on each scraper run.
-- Handy for patch-level analysis without scanning the full games table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patches (
    patch      TEXT PRIMARY KEY,
    num_games  INTEGER,
    first_date TEXT,
    last_date  TEXT
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_games_date          ON games(date);
CREATE INDEX IF NOT EXISTS idx_games_league        ON games(league);
CREATE INDEX IF NOT EXISTS idx_games_patch         ON games(patch);
CREATE INDEX IF NOT EXISTS idx_draft_gameid        ON draft_picks(gameid);
CREATE INDEX IF NOT EXISTS idx_draft_champion      ON draft_picks(champion);
CREATE INDEX IF NOT EXISTS idx_draft_player        ON draft_picks(playername);
CREATE INDEX IF NOT EXISTS idx_draft_champ_pos     ON draft_picks(champion, position);
CREATE INDEX IF NOT EXISTS idx_pickbans_gameid     ON team_pickbans(gameid);
CREATE INDEX IF NOT EXISTS idx_pickbans_team       ON team_pickbans(teamname);
