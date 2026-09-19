import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/app/discos_pendientes.db")
MUSICA_LOCAL_DB = os.environ.get("MUSICA_LOCAL_DB", "/musica_local.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    uid            TEXT PRIMARY KEY,
    href           TEXT NOT NULL,
    artist         TEXT NOT NULL,
    album          TEXT NOT NULL,
    artist_norm    TEXT NOT NULL,
    album_norm     TEXT NOT NULL,
    status         TEXT NOT NULL,
    dtstart        TEXT,
    summary_raw    TEXT NOT NULL,
    synced_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_artist_norm ON tasks(artist_norm);

CREATE TABLE IF NOT EXISTS pending_status (
    uid               TEXT PRIMARY KEY REFERENCES tasks(uid),
    airsonic_found    INTEGER NOT NULL DEFAULT 0,
    airsonic_date     TEXT,
    qbittorrent_found INTEGER NOT NULL DEFAULT 0,
    qbittorrent_date  TEXT,
    downloaded        INTEGER NOT NULL DEFAULT 0,
    checked_at        TEXT NOT NULL,
    radicale_marked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status_downloaded ON pending_status(downloaded);

CREATE TABLE IF NOT EXISTS enrichment (
    artist_norm    TEXT NOT NULL,
    album_norm     TEXT NOT NULL,
    source         TEXT NOT NULL,
    lastfm_json    TEXT,
    wikipedia_json TEXT,
    mb_json        TEXT,
    cover_url      TEXT,
    genre          TEXT,
    label          TEXT,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (artist_norm, album_norm)
);
"""


def init_db(path: str = DB_PATH) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS no migra tablas ya existentes -- las DBs
    # desplegadas antes de radicale_writer.py no tienen esta columna.
    try:
        conn.execute("ALTER TABLE pending_status ADD COLUMN radicale_marked_at TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def get_conn() -> sqlite3.Connection:
    # DB_PATH explícito (no el default de init_db, fijado en tiempo de
    # definición) -- así un cambio de DB_PATH en caliente (tests) se
    # respeta también aquí.
    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_musica_local_conn() -> sqlite3.Connection:
    # immutable=1 -- musica_local.sqlite se monta :ro en Docker; mode=ro
    # solo no basta en un bind-mount de solo lectura (mismo problema ya
    # encontrado y documentado en services/lastfm_explorer/db.py y
    # services/yt_playlists/db.py).
    conn = sqlite3.connect(f"file:{MUSICA_LOCAL_DB}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
