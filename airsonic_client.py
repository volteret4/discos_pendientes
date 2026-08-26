"""Comprobar si un album ya esta en Airsonic. Adaptado de
services/dieta_sonora/estadisticas/albums_log/airsonic_checker.py::search_airsonic."""
import os
from datetime import date, datetime

import requests

from normalize import normalize

AIRSONIC_URL = os.environ.get("AIRSONIC_URL", "").rstrip("/")
AIRSONIC_USER = os.environ.get("AIRSONIC_USER", "")
AIRSONIC_PASS = os.environ.get("AIRSONIC_PASS", "")
AIRSONIC_API_VERSION = os.environ.get("AIRSONIC_API_VERSION", "1.15.0")


def search_airsonic(artist: str, album: str) -> date | None:
    """Busca el album en Airsonic y devuelve la fecha `created` (fecha en
    que se añadio a la biblioteca) mas antigua encontrada, o None si no
    esta."""
    if not AIRSONIC_URL or not AIRSONIC_USER:
        return None

    try:
        r = requests.get(
            f"{AIRSONIC_URL}/rest/search3",
            params={
                "u": AIRSONIC_USER,
                "p": AIRSONIC_PASS,
                "v": AIRSONIC_API_VERSION,
                "c": "discos_pendientes",
                "f": "json",
                "query": album,
                "albumCount": 50,
                "albumOffset": 0,
                "artistCount": 0,
                "songCount": 0,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    if data.get("subsonic-response", {}).get("status") != "ok":
        return None

    albums = (
        data.get("subsonic-response", {})
            .get("searchResult3", {})
            .get("album", [])
    )
    if not albums:
        return None

    artist_n = normalize(artist)
    album_n = normalize(album)
    best: date | None = None

    for found in albums:
        fa = normalize(found.get("artist", ""))
        fn = normalize(found.get("name", ""))
        if fa != artist_n:
            continue
        if fn != album_n and album_n not in fn:
            continue
        raw = found.get("created", "")
        if not raw:
            d = date.today()
        else:
            try:
                d = datetime.fromisoformat(raw.rstrip("Z")).date()
            except ValueError:
                d = date.today()
        if best is None or d < best:
            best = d

    return best
