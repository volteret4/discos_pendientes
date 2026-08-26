"""Comprobar si un album ya esta descargado en qBittorrent. Adaptado de
services/dieta_sonora/estadisticas/albums_log/qbittorrent_checker.py::search_qbittorrent.

QB_HOST=localhost en el .env apunta al host real desde un script no
contenedorizado -- dentro de un contenedor Docker hay que sobreescribirlo a
host.docker.internal (mismo patron que los bloques dieta-sonora/
dieta-sonora-stats en docker-compose.yml, con extra_hosts)."""
import os
from datetime import date, datetime, timezone

from qbittorrentapi import Client

from normalize import normalize

QB_HOST = os.environ.get("QB_HOST", "localhost")
QB_PORT = os.environ.get("QB_PORT", "8080")
QB_USER = os.environ.get("QB_USER", "admin")
QB_PASS = os.environ.get("QB_PASS", "adminadmin")


def get_torrents() -> list:
    qbt = Client(host=QB_HOST, port=QB_PORT, username=QB_USER, password=QB_PASS)
    qbt.auth_log_in()
    return qbt.torrents_info()


def search_qbittorrent(torrents: list, artist: str, album: str) -> date | None:
    """Busca el album en la lista de torrents y devuelve la fecha en que
    fue añadido a qBittorrent (added_on) mas antigua, o None si no esta."""
    artist_n = normalize(artist)
    album_n = normalize(album)
    best: date | None = None

    for t in torrents:
        t_name = normalize(t.name)
        if artist_n not in t_name or album_n not in t_name:
            continue
        try:
            added = datetime.fromtimestamp(t.added_on, tz=timezone.utc).date()
        except Exception:
            added = date.today()
        if best is None or added < best:
            best = added

    return best
