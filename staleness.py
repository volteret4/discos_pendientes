"""Copiado de services/discos_olvidados/staleness.py (servicio retirado,
su vista se ha fusionado en la pestaña Catálogo y la nueva pestaña
Artistas de esta app).

Cruza la colección propia (musica_local.sqlite) con los scrobbles de
Last.fm (lastfm_stats.db) para calcular cuánto hace que no se escucha
cada álbum/artista, y con el backup de qBittorrent/Lidarr (music.db de
services/trasvase, collection=2) para saber si además está respaldado
ahí. Sin base de datos propia -- todo se calcula al vuelo y se cachea en
memoria de proceso (ver get_report()/refresh_all())."""
import time
from datetime import datetime, timezone

from db import get_lastfm_conn, get_musica_local_conn, get_trasvase_conn
from normalize import normalize

_CACHE: dict = {"albums": None, "artists": None, "album_index": None, "computed_at": None}


def _now_ts() -> int:
    return int(time.time())


def _days_since(ts) -> float | None:
    if ts is None:
        return None
    return round((_now_ts() - ts) / 86400.0, 1)


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _load_lastfm_artist_index() -> dict:
    """{normalize(name): (artist_id, last_ts_or_None)}"""
    conn = get_lastfm_conn()
    try:
        last_ts_by_artist = {
            row["artist_id"]: row[1]
            for row in conn.execute("SELECT artist_id, MAX(ts) FROM scrobbles GROUP BY artist_id")
        }
        index = {}
        for row in conn.execute("SELECT artist_id, name FROM artists"):
            index[normalize(row["name"])] = (row["artist_id"], last_ts_by_artist.get(row["artist_id"]))
        return index
    finally:
        conn.close()


def _load_lastfm_album_last_ts() -> dict:
    """{(artist_id, normalize(album_name)): last_ts} -- solo álbumes de
    lastfm_stats.db que tienen al menos un scrobble."""
    conn = get_lastfm_conn()
    try:
        index = {}
        for row in conn.execute(
            """
            SELECT al.artist_id, al.name, MAX(s.ts) AS last_ts
            FROM albums al JOIN scrobbles s
                ON s.artist_id = al.artist_id AND s.album_id = al.album_id
            GROUP BY al.artist_id, al.album_id
            """
        ):
            index[(row["artist_id"], normalize(row["name"]))] = row["last_ts"]
        return index
    finally:
        conn.close()


def _load_qbittorrent_index() -> set:
    """{(normalize(artist), normalize(album))} -- álbumes presentes en el
    backup de qBittorrent/Lidarr (services/trasvase music.db,
    collection=2 = la carpeta de descargas de qBittorrent`)."""
    conn = get_trasvase_conn()
    try:
        return {
            (normalize(row["name"]), normalize(row["title"]))
            for row in conn.execute(
                "SELECT ar.name, al.title FROM albums al "
                "JOIN artists ar ON ar.id = al.artist_id WHERE al.collection = 2"
            )
        }
    finally:
        conn.close()


def _compute_albums() -> list[dict]:
    lastfm_artist = _load_lastfm_artist_index()
    lastfm_album_ts = _load_lastfm_album_last_ts()
    qbittorrent_index = _load_qbittorrent_index()

    conn = get_musica_local_conn()
    try:
        albums = conn.execute(
            "SELECT al.name AS album, al.year, al.label, al.genre, ar.name AS artist "
            "FROM albums al JOIN artists ar ON ar.id = al.artist_id"
        ).fetchall()
    finally:
        conn.close()

    rows = []
    for a in albums:
        artist_norm, album_norm = normalize(a["artist"]), normalize(a["album"])
        artist_match = lastfm_artist.get(artist_norm)
        last_ts = None
        if artist_match:
            artist_id, _ = artist_match
            last_ts = lastfm_album_ts.get((artist_id, album_norm))
        rows.append({
            "artist": a["artist"], "album": a["album"], "year": a["year"],
            "label": a["label"], "genre": a["genre"],
            "last_listened": _iso(last_ts), "days_since": _days_since(last_ts),
            "in_qbittorrent": (artist_norm, album_norm) in qbittorrent_index,
        })
    return rows


def _compute_artists() -> list[dict]:
    lastfm_artist = _load_lastfm_artist_index()
    qbittorrent_index = _load_qbittorrent_index()

    conn = get_musica_local_conn()
    try:
        all_artists = conn.execute("SELECT name FROM artists").fetchall()
        album_rows = conn.execute(
            "SELECT ar.name AS artist, al.name AS album FROM artists ar JOIN albums al ON al.artist_id = ar.id"
        ).fetchall()
    finally:
        conn.close()

    album_count, backed_up_count = {}, {}
    for r in album_rows:
        album_count[r["artist"]] = album_count.get(r["artist"], 0) + 1
        if (normalize(r["artist"]), normalize(r["album"])) in qbittorrent_index:
            backed_up_count[r["artist"]] = backed_up_count.get(r["artist"], 0) + 1

    rows = []
    for a in all_artists:
        name = a["name"]
        match = lastfm_artist.get(normalize(name))
        last_ts = match[1] if match else None
        rows.append({
            "artist": name, "album_count": album_count.get(name, 0),
            "backed_up_count": backed_up_count.get(name, 0),
            "last_listened": _iso(last_ts), "days_since": _days_since(last_ts),
        })
    return rows


def album_index() -> dict:
    """{(normalize(artist), normalize(album)): fila de get_report("albums")}
    -- para anotar filas del catálogo (que salen de su propia consulta SQL
    a musica_local.sqlite) con days_since/in_qbittorrent."""
    if _CACHE["album_index"] is None:
        _CACHE["album_index"] = {
            (normalize(r["artist"]), normalize(r["album"])): r for r in get_report("albums")
        }
    return _CACHE["album_index"]


def refresh_all() -> None:
    _CACHE["album_index"] = None
    _CACHE["albums"] = _compute_albums()
    _CACHE["artists"] = _compute_artists()
    _CACHE["computed_at"] = datetime.now(timezone.utc).isoformat()


def get_report(kind: str) -> list[dict]:
    if _CACHE[kind] is None:
        refresh_all()
    return _CACHE[kind]


def computed_at() -> str | None:
    return _CACHE["computed_at"]
