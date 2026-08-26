"""Enriquecimiento por album: musica_local.sqlite (biblioteca propia)
primero, si no esta ahi, Last.fm + Wikipedia + MusicBrainz en vivo.
Adaptado de services/yt_playlists/enrich_info.py -- ahi solo se tenia
artista+cancion y habia que resolver el album primero; aqui ya se conoce
el album directamente (viene del SUMMARY del VTODO), asi que el flujo es
mas simple."""
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from db import get_musica_local_conn
from normalize import normalize

LFM_API_KEY = os.environ.get("LASTFM_API_KEY") or None
CAA = "https://coverartarchive.org/release-group"
_LFM_NO_IMG = "2a96cbd8b46e442fc41c2b86b821562f"
_UA = "discos_pendientes/1.0 (https://github.com/volteret4/discos_pendientes)"


def _lfm_image(images: list, size: str = "extralarge") -> str:
    for img in images:
        url = img.get("#text", "")
        if img.get("size") == size and url and _LFM_NO_IMG not in url:
            return url
    return ""


def lfm_get(method: str, params: dict) -> dict:
    if not LFM_API_KEY:
        return {"error": "sin LASTFM_API_KEY"}
    base = "https://ws.audioscrobbler.com/2.0/"
    params = {**params, "method": method, "api_key": LFM_API_KEY, "format": "json"}
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def mb_search_artist(artist: str) -> dict:
    q = f'artist:"{artist}"'
    url = "https://musicbrainz.org/ws/2/artist?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": "1"})
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        results = data.get("artists", [])
        if results:
            return {"mbid": results[0].get("id", ""), "name": results[0].get("name", artist)}
    except Exception:
        pass
    return {}


def mb_search_release_group(artist: str, album: str) -> dict:
    q = f'artist:"{artist}" AND release:"{album}"'
    url = "https://musicbrainz.org/ws/2/release-group?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": "1"})
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        rgs = data.get("release-groups", [])
        if rgs:
            rg = rgs[0]
            return {"mbid": rg.get("id", ""), "title": rg.get("title", album), "date": rg.get("first-release-date", "")}
    except Exception:
        pass
    return {}


def wikipedia_summary(title: str, lang: str = "en") -> dict:
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        if data.get("type") == "disambiguation":
            return {}
        return {
            "extract": data.get("extract", ""),
            "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
            "thumbnail": (data.get("thumbnail") or {}).get("source", ""),
        }
    except Exception:
        return {}


def local_lookup(artist: str, album: str) -> dict | None:
    """Si el album ya esta en musica_local.sqlite, sus datos reales --
    mas fiables y sin red. None si no se encuentra."""
    artist_n = normalize(artist)
    album_n = normalize(album)
    try:
        conn = get_musica_local_conn()
    except Exception:
        return None
    try:
        # Camino rapido: coincidencia exacta case-insensitive vía SQL.
        row = conn.execute(
            """
            SELECT al.name, al.year, al.genre, al.wikipedia_content, al.wikipedia_url,
                   al.lastfm_url, al.discogs_url, al.folder_path, al.label,
                   ar.name AS artist_name, ar.bio AS artist_bio,
                   ar.wikipedia_content AS artist_wikipedia_content,
                   ar.wikipedia_url AS artist_wikipedia_url, ar.lastfm_url AS artist_lastfm_url
            FROM albums al JOIN artists ar ON ar.id = al.artist_id
            WHERE al.name = ? COLLATE NOCASE AND ar.name = ? COLLATE NOCASE
            LIMIT 1
            """,
            (album, artist),
        ).fetchone()
        if row is None:
            # Camino lento: normalizado (acentos/mayúsculas), solo si el
            # rápido no encontró nada -- evita escanear 4000+ filas en el
            # caso común.
            for r in conn.execute(
                """
                SELECT al.name, al.year, al.genre, al.wikipedia_content, al.wikipedia_url,
                       al.lastfm_url, al.discogs_url, al.folder_path, al.label,
                       ar.name AS artist_name, ar.bio AS artist_bio,
                       ar.wikipedia_content AS artist_wikipedia_content,
                       ar.wikipedia_url AS artist_wikipedia_url, ar.lastfm_url AS artist_lastfm_url
                FROM albums al JOIN artists ar ON ar.id = al.artist_id
                """
            ):
                if normalize(r["name"]) == album_n and normalize(r["artist_name"]) == artist_n:
                    row = r
                    break
        if row is None:
            return None

        label_info = {}
        if row["label"]:
            lrow = conn.execute(
                "SELECT name, description, wikipedia_url, discogs_url "
                "FROM labels WHERE name = ? COLLATE NOCASE LIMIT 1",
                (row["label"],),
            ).fetchone()
            if lrow:
                label_info = {
                    "name": lrow["name"],
                    "description": lrow["description"] or "",
                    "wikipedia_url": lrow["wikipedia_url"] or "",
                    "discogs_url": lrow["discogs_url"] or "",
                }

        return {
            "album": {
                "name": row["name"],
                "year": row["year"],
                "genre": row["genre"],
                "label": row["label"],
                "wiki": row["wikipedia_content"] or "",
                "url": row["lastfm_url"] or "",
                "wikipedia_url": row["wikipedia_url"] or "",
                "discogs_url": row["discogs_url"] or "",
                "owned": True,
                "folder_path": row["folder_path"],
            },
            "artist": {
                "name": row["artist_name"],
                "bio": row["artist_bio"] or "",
                "wikipedia_content": row["artist_wikipedia_content"] or "",
                "wikipedia_url": row["artist_wikipedia_url"] or "",
                "url": row["artist_lastfm_url"] or "",
            },
            "label": label_info,
        }
    finally:
        conn.close()


def enrich_album(artist: str, album: str) -> dict:
    result = {"source": "none", "artist": {}, "album": {}, "wikipedia": {}, "musicbrainz": {}, "label": {}}
    if not artist or not album:
        return result

    local = local_lookup(artist, album)
    if local:
        result["source"] = "local_db"
        result["artist"] = local["artist"]
        result["album"] = local["album"]
        result["label"] = local["label"]
        return result

    result["source"] = "live"

    ar_data = lfm_get("artist.getInfo", {"artist": artist, "autocorrect": 1})
    if "artist" in ar_data:
        ar = ar_data["artist"]
        result["artist"] = {
            "name": ar.get("name", artist),
            "bio": (ar.get("bio", {}).get("summary", "") or "").split("<a ")[0].strip(),
            "listeners": ar.get("stats", {}).get("listeners", ""),
            "image": _lfm_image(ar.get("image", [])),
            "url": ar.get("url", ""),
            "tags": [t["name"] for t in (ar.get("tags", {}).get("tag", []) or [])[:6]],
        }
    time.sleep(0.15 + random.random() * 0.15)

    al_data = lfm_get("album.getInfo", {"artist": artist, "album": album, "autocorrect": 1})
    if "album" in al_data:
        al = al_data["album"]
        result["album"] = {
            "name": al.get("name", album),
            "wiki": (al.get("wiki", {}).get("summary", "") or "").split("<a ")[0].strip(),
            "image": _lfm_image(al.get("image", [])),
            "url": al.get("url", ""),
            "tags": [t["name"] for t in (al.get("tags", {}).get("tag", []) or [])[:6]],
            "owned": False,
        }
    else:
        result["album"] = {"name": album, "owned": False}

    time.sleep(0.15 + random.random() * 0.15)
    mb_rg = mb_search_release_group(artist, album)
    if mb_rg.get("mbid"):
        result["musicbrainz"]["release_group_mbid"] = mb_rg["mbid"]
        result["musicbrainz"]["release_group_url"] = f"https://musicbrainz.org/release-group/{mb_rg['mbid']}"
        if not result["album"].get("image"):
            result["album"]["image"] = f"{CAA}/{mb_rg['mbid']}/front-500"
        if not result["album"].get("year") and mb_rg.get("date"):
            result["album"]["year"] = mb_rg["date"][:4]

    time.sleep(0.15 + random.random() * 0.15)
    mb_artist = mb_search_artist(artist)
    if mb_artist.get("mbid"):
        result["musicbrainz"]["artist_mbid"] = mb_artist["mbid"]
        result["musicbrainz"]["artist_url"] = f"https://musicbrainz.org/artist/{mb_artist['mbid']}"

    wiki = wikipedia_summary(artist)
    if wiki:
        result["wikipedia"] = wiki

    return result


def _cache_enrichment(conn, artist_norm: str, album_norm: str, result: dict) -> None:
    # Solo se cachea el camino "live" (llamadas externas de verdad) -- el
    # camino "local_db" ya es instantáneo (una query a musica_local.sqlite)
    # y siempre puede recalcularse, así que cachearlo sería duplicar datos
    # sin ganar nada.
    if result.get("source") != "live":
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO enrichment
            (artist_norm, album_norm, source, lastfm_json, wikipedia_json, mb_json,
             cover_url, genre, label, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artist_norm, album_norm, result["source"],
            json.dumps({"artist": result["artist"], "album": result["album"]}),
            json.dumps(result.get("wikipedia") or {}),
            json.dumps(result.get("musicbrainz") or {}),
            (result.get("album") or {}).get("image", ""),
            (result.get("album") or {}).get("genre", ""),
            (result.get("album") or {}).get("label", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_or_fetch_enrichment(conn, artist: str, album: str) -> dict:
    """Punto de entrada usado tanto por sync.py (backfill) como por
    server.py (bajo demanda): local_db siempre se recalcula (barato), el
    camino live se sirve desde la cache `enrichment` si ya existe."""
    local = local_lookup(artist, album)
    if local:
        return {
            "source": "local_db", "artist": local["artist"], "album": local["album"],
            "label": local["label"], "wikipedia": {}, "musicbrainz": {},
        }

    artist_norm, album_norm = normalize(artist), normalize(album)
    row = conn.execute(
        "SELECT * FROM enrichment WHERE artist_norm = ? AND album_norm = ?",
        (artist_norm, album_norm),
    ).fetchone()
    if row is not None:
        lastfm = json.loads(row["lastfm_json"] or "{}")
        return {
            "source": row["source"],
            "artist": lastfm.get("artist", {}),
            "album": lastfm.get("album", {}),
            "wikipedia": json.loads(row["wikipedia_json"] or "{}"),
            "musicbrainz": json.loads(row["mb_json"] or "{}"),
            "label": {},
        }

    result = enrich_album(artist, album)
    _cache_enrichment(conn, artist_norm, album_norm, result)
    return result
