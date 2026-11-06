"""Cliente Subsonic de Navidrome con el usuario de escucha (NAVIDROME_USER,
"music" en producción) -- distinto de AIRSONIC_USER a propósito: "admin"
ve también la biblioteca "mixxx" y las playlists son del usuario que las
crea (ver CLAUDE.md).

Todo va por POST con token+salt: las credenciales no aparecen en URLs ni,
por tanto, en logs o mensajes de excepción, y las listas largas de songId
no chocan con el límite de longitud de una URL."""
import hashlib
import os
import secrets

import requests

PAGE_SIZE = 500


class NavidromeError(Exception):
    """El mensaje es seguro para enseñarlo en la UI (sin credenciales)."""


def _config() -> tuple[str, str, str]:
    # NAVIDROME_URL es opcional: normalmente es el mismo servidor que
    # AIRSONIC_URL (Navidrome ya atiende ese puerto).
    url = (os.environ.get("NAVIDROME_URL") or os.environ.get("AIRSONIC_URL") or "").rstrip("/")
    return url, os.environ.get("NAVIDROME_USER", ""), os.environ.get("NAVIDROME_PASS", "")


def is_configured() -> bool:
    return all(_config())


def username() -> str:
    return _config()[1]


def _call(endpoint: str, timeout: int = 60, **params) -> dict:
    url, user, password = _config()
    if not all((url, user, password)):
        raise NavidromeError("Navidrome sin configurar: faltan NAVIDROME_USER / NAVIDROME_PASS en el .env")
    salt = secrets.token_hex(8)
    data = {
        "u": user, "s": salt, "t": hashlib.md5((password + salt).encode()).hexdigest(),
        "v": "1.16.1", "c": "discos_pendientes", "f": "json", **params,
    }
    try:
        r = requests.post(f"{url}/rest/{endpoint}", data=data, timeout=timeout)
        r.raise_for_status()
        body = r.json()["subsonic-response"]
    except (requests.RequestException, ValueError, KeyError) as e:
        # "from None" y solo el tipo: el texto original podría incluir la petición.
        raise NavidromeError(f"{endpoint}: sin respuesta válida de Navidrome ({type(e).__name__})") from None
    if body.get("status") != "ok":
        raise NavidromeError(f"{endpoint}: {(body.get('error') or {}).get('message', 'error de Navidrome')}")
    return body


def _fetch_pages(fetch_page) -> list[dict]:
    items, offset = [], 0
    while True:
        page = fetch_page(offset)
        items += page
        offset += len(page)
        if len(page) < PAGE_SIZE:
            return items


def fetch_albums() -> list[dict]:
    """Todos los álbumes que ve el usuario, en todas sus bibliotecas."""
    return _fetch_pages(lambda offset: _call(
        "getAlbumList2", type="alphabeticalByName", size=PAGE_SIZE, offset=offset,
    ).get("albumList2", {}).get("album", []))


def fetch_songs() -> list[dict]:
    """Todas las canciones del usuario: search3 con la consulta vacía '""'
    es la forma de Navidrome de listarlas sin una petición por álbum."""
    return _fetch_pages(lambda offset: _call(
        "search3", query='""', songCount=PAGE_SIZE, songOffset=offset, albumCount=0, artistCount=0,
    ).get("searchResult3", {}).get("song", []))


def album_song_ids(album_id: str) -> list[str]:
    """IDs en el orden del disco (getAlbum ya los devuelve por disco/pista)."""
    songs = _call("getAlbum", id=album_id).get("album", {}).get("song", [])
    return [s["id"] for s in songs]


def editable_playlists() -> list[dict]:
    """Solo las propias: getPlaylists también lista públicas de otros
    usuarios, que Subsonic no deja modificar."""
    user = username()
    playlists = _call("getPlaylists").get("playlists", {}).get("playlist", [])
    return sorted(
        ({"id": p["id"], "name": p["name"], "song_count": p.get("songCount", 0)}
         for p in playlists if p.get("owner") == user),
        key=lambda p: p["name"].lower(),
    )


def playlist_info(playlist_id: str) -> dict:
    """{"name": ..., "song_ids": [...]}; falla si no es del usuario, para
    dar un error claro antes de intentar escribir."""
    pl = _call("getPlaylist", id=playlist_id).get("playlist", {})
    if pl.get("owner") != username():
        raise NavidromeError(f"La playlist «{pl.get('name', playlist_id)}» no es de {username()}")
    return {"name": pl.get("name", ""), "song_ids": [s["id"] for s in pl.get("entry", [])]}


def create_playlist(name: str, song_ids: list[str]) -> str:
    body = _call("createPlaylist", timeout=120, name=name, songId=song_ids)
    playlist_id = body.get("playlist", {}).get("id")
    if not playlist_id:
        raise NavidromeError("createPlaylist: Navidrome no devolvió el id de la playlist")
    return playlist_id


def add_to_playlist(playlist_id: str, song_ids: list[str]) -> None:
    _call("updatePlaylist", timeout=120, playlistId=playlist_id, songIdToAdd=song_ids)
