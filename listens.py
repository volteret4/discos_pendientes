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


# ── Biblioteca de Navidrome: ¿escuchado o no? ───────────────────────────

# A partir de esta fracción de pistas con scrobble, un álbum sin ningún
# scrobble bajo su tag cuenta como escuchado. En producción separa bien
# "Bad Moon Rising" (11/12, reedición con otro tag) de "MCMXC a.D." (1/7,
# single o recopilatorio suelto).
LISTENED_TRACK_RATIO = 0.5

_TYPOGRAPHY = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "“": '"', "”": '"', "…": "...", "–": "-", "—": "-"})


def listen_key(s: str) -> str:
    """normalize() + variantes tipográficas ("Takk..." / "Takk…"). Sigue
    siendo una igualdad exacta, no fuzzy. No se cambia normalize() porque
    sus valores ya están cacheados en otras tablas."""
    return normalize((s or "").translate(_TYPOGRAPHY))


def _merge(index: dict, key: tuple, first_ts: int, last_ts: int, plays: int) -> None:
    prev = index.get(key)
    index[key] = (first_ts, last_ts, plays) if prev is None else (
        min(prev[0], first_ts), max(prev[1], last_ts), prev[2] + plays,
    )


def load_listen_index(lastfm_conn: sqlite3.Connection) -> dict:
    """{"albums": {(artista, álbum): (first, last, plays)}, "tracks": {(artista, pista): ...}}
    sobre TODOS los scrobbles: lastfm_stats.db ya une Last.fm y ListenBrainz
    (columna source, ya deduplicada por extraer_scrobbles.py)."""
    albums, tracks = {}, {}
    for r in lastfm_conn.execute(
        """
        SELECT ar.name AS artist, al.name AS album, MIN(s.ts) AS f, MAX(s.ts) AS l, COUNT(*) AS n
        FROM scrobbles s
        JOIN artists ar ON ar.artist_id = s.artist_id
        JOIN albums al ON al.album_id = s.album_id
        GROUP BY s.artist_id, s.album_id
        """
    ):
        _merge(albums, (listen_key(r["artist"]), listen_key(r["album"])), r["f"], r["l"], r["n"])
    for r in lastfm_conn.execute(
        """
        SELECT ar.name AS artist, s.track AS track, MIN(s.ts) AS f, MAX(s.ts) AS l, COUNT(*) AS n
        FROM scrobbles s JOIN artists ar ON ar.artist_id = s.artist_id
        GROUP BY s.artist_id, s.track
        """
    ):
        _merge(tracks, (listen_key(r["artist"]), listen_key(r["track"])), r["f"], r["l"], r["n"])
    return {"albums": albums, "tracks": tracks}


def _artist_keys(item: dict) -> set:
    """Artista principal + los que Navidrome separa en "artists" (ej.
    colaboraciones, que Last.fm suele tener a nombre de uno solo)."""
    names = [item.get("artist")] + [a.get("name") for a in item.get("artists") or []]
    return {listen_key(n) for n in names if n}


def match_album(album: dict, songs: list[dict], index: dict) -> dict:
    """Cruza un álbum de Navidrome con los scrobbles.

    match_kind:
      "album"  -- hay scrobbles con el tag de este álbum. Se mira también
                  el artista de cada canción: en un "Various Artists" el
                  scrobble está a nombre del artista de la pista.
      "tracks" -- sin tag, pero >= LISTENED_TRACK_RATIO de sus pistas tienen
                  scrobbles (reediciones, etc.). Aproximado: una pista pudo
                  sonar desde otro disco, por eso la UI lo marca con "~".
      None     -- no escuchado (puede tener tracks_heard > 0: pistas sueltas).
    """
    album_keys = {(a, listen_key(album.get("name"))) for a in _artist_keys(album)}
    track_keys = {}  # {song_id: {(artista, pista), ...}}
    for s in songs:
        artists = _artist_keys(s)
        album_keys |= {(a, listen_key(s.get("album"))) for a in artists}
        track_keys[s["id"]] = {(a, listen_key(s.get("title"))) for a in artists}

    def aggregate(entries):
        return min(e[0] for e in entries), max(e[1] for e in entries), sum(e[2] for e in entries)

    heard_keys = [{k for k in keys if k in index["tracks"]} for keys in track_keys.values()]
    tracks_heard = sum(1 for keys in heard_keys if keys)
    result = {"tracks_heard": tracks_heard, "match_kind": None, "first_ts": None, "last_ts": None, "plays": 0}

    tagged = [index["albums"][k] for k in album_keys if k in index["albums"]]
    if tagged:
        result["match_kind"] = "album"
        result["first_ts"], result["last_ts"], result["plays"] = aggregate(tagged)
    elif songs and tracks_heard / len(songs) >= LISTENED_TRACK_RATIO:
        result["match_kind"] = "tracks"
        result["first_ts"], result["last_ts"], result["plays"] = aggregate(
            [index["tracks"][k] for k in set().union(*heard_keys)]
        )
    return result


def compute_library_rows(albums: list[dict], songs: list[dict], index: dict) -> list[dict]:
    songs_by_album = {}
    for s in songs:
        songs_by_album.setdefault(s.get("albumId"), []).append(s)

    rows = []
    for a in albums:
        album_songs = songs_by_album.get(a["id"], [])
        rows.append({
            "id": a["id"], "artist": a.get("artist") or "", "album": a.get("name") or "",
            "year": a.get("year") or None, "genre": a.get("genre") or None, "created": a.get("created"),
            "song_count": a.get("songCount") or len(album_songs),
            **match_album(a, album_songs, index),
        })
    return rows
