"""Cruza el catálogo local (musica_local.sqlite) con los scrobbles de
Last.fm (lastfm_stats.db, tablas artists/albums/scrobbles -- mismo
esquema ya documentado en services/discos_olvidados/staleness.py) para
calcular, por álbum, cuándo se escuchó por primera vez, y para un
artista dado, su serie temporal de escuchas mes a mes desde la primera.
"""
import sqlite3
from datetime import datetime, timezone

from normalize import normalize


def _load_artist_index(lastfm_conn: sqlite3.Connection) -> dict:
    """{normalize(name): artist_id}"""
    return {
        normalize(row["name"]): row["artist_id"]
        for row in lastfm_conn.execute("SELECT artist_id, name FROM artists")
    }


def _load_album_first_ts(lastfm_conn: sqlite3.Connection) -> dict:
    """{(artist_id, normalize(album_name)): MIN(ts)} -- solo álbumes de
    lastfm_stats.db con al menos un scrobble."""
    index = {}
    for row in lastfm_conn.execute(
        """
        SELECT al.artist_id, al.name, MIN(s.ts) AS first_ts
        FROM albums al JOIN scrobbles s
            ON s.artist_id = al.artist_id AND s.album_id = al.album_id
        GROUP BY al.artist_id, al.album_id
        """
    ):
        index[(row["artist_id"], normalize(row["name"]))] = row["first_ts"]
    return index


def _load_artist_first_ts(lastfm_conn: sqlite3.Connection) -> dict:
    """{artist_id: MIN(ts)} -- primera escucha de CUALQUIER scrobble de
    ese artista, sin exigir que su tag de álbum coincida con ninguno de
    los álbumes conocidos (ver compute_artist_first_listens)."""
    return {
        row["artist_id"]: row[1]
        for row in lastfm_conn.execute("SELECT artist_id, MIN(ts) FROM scrobbles GROUP BY artist_id")
    }


def compute_album_first_listens(musica_local_conn: sqlite3.Connection, lastfm_conn: sqlite3.Connection) -> list[dict]:
    """Para cada álbum del catálogo local, su (artist_norm, album_norm,
    first_ts) si hay algún scrobble en Last.fm, para cachear en
    discos_pendientes.db (ver sync.sync_album_first_listens)."""
    artist_index = _load_artist_index(lastfm_conn)
    album_first_ts = _load_album_first_ts(lastfm_conn)

    albums = musica_local_conn.execute(
        "SELECT al.name AS album, ar.name AS artist FROM albums al JOIN artists ar ON ar.id = al.artist_id"
    ).fetchall()

    rows = []
    for a in albums:
        artist_norm, album_norm = normalize(a["artist"]), normalize(a["album"])
        artist_id = artist_index.get(artist_norm)
        first_ts = album_first_ts.get((artist_id, album_norm)) if artist_id is not None else None
        rows.append({"artist_norm": artist_norm, "album_norm": album_norm, "first_ts": first_ts})
    return rows


def compute_artist_first_listens(musica_local_conn: sqlite3.Connection, lastfm_conn: sqlite3.Connection) -> list[dict]:
    """Primera escucha por ARTISTA (no por álbum concreto) -- fallback
    para cuando compute_album_first_listens() no encuentra ningún
    scrobble con el tag exacto de ese álbum (reediciones, "..." vs "…",
    recopilatorios, etc. -- el mismo tipo de falso negativo ya conocido
    y documentado en services/discos_olvidados/CLAUDE.md, aquí resuelto
    mostrando el dato de la banda en vez de intentar fuzzy matching)."""
    artist_index = _load_artist_index(lastfm_conn)
    artist_first_ts = _load_artist_first_ts(lastfm_conn)

    artists = musica_local_conn.execute("SELECT DISTINCT name FROM artists").fetchall()
    rows = []
    for a in artists:
        artist_norm = normalize(a["name"])
        artist_id = artist_index.get(artist_norm)
        first_ts = artist_first_ts.get(artist_id) if artist_id is not None else None
        rows.append({"artist_norm": artist_norm, "first_ts": first_ts})
    return rows


def artist_timeline(lastfm_conn: sqlite3.Connection, artist_name: str) -> dict | None:
    """Serie mensual de escuchas de un artista desde su primer scrobble.
    None si el artista no tiene ningún scrobble registrado en Last.fm."""
    artist_norm = normalize(artist_name)
    row = lastfm_conn.execute(
        "SELECT artist_id FROM artists WHERE name = ? COLLATE NOCASE LIMIT 1", (artist_name,)
    ).fetchone()
    artist_id = row["artist_id"] if row else _load_artist_index(lastfm_conn).get(artist_norm)
    if artist_id is None:
        return None

    monthly = lastfm_conn.execute(
        """
        SELECT strftime('%Y-%m', datetime(ts, 'unixepoch')) AS month, COUNT(*) AS plays
        FROM scrobbles WHERE artist_id = ?
        GROUP BY month ORDER BY month
        """,
        (artist_id,),
    ).fetchall()
    if not monthly:
        return None

    first_ts = lastfm_conn.execute(
        "SELECT MIN(ts) AS first_ts FROM scrobbles WHERE artist_id = ?", (artist_id,)
    ).fetchone()["first_ts"]

    return {
        "first_listened": datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat() if first_ts else None,
        "total_plays": sum(r["plays"] for r in monthly),
        "timeline": [{"month": r["month"], "plays": r["plays"]} for r in monthly],
    }
