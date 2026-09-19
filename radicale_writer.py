"""Escritura en el calendario de lanzamientos de Radicale
(RELEASE_CALENDAR_NAME): al detectar que un álbum pendiente se ha
descargado, se marca el VEVENT de su lanzamiento con un icono de
"descargado" y la fecha.

RELEASE_CALENDAR_NAME es un calendario hermano de CALENDAR_TASKS (ver
radicale_client.py) -- comparte credenciales/URL/base, solo cambia el
nombre del calendario. Es el mismo calendario que dieta_sonora conoce
como CALENDAR_NAME -- aquí se usa un nombre de variable distinto a
propósito: el `.env` raíz del monorepo ya reutiliza "CALENDAR_NAME" para
un calendario *distinto* en otras secciones (colisión de nombres
preexistente en el repo, no introducida aquí), así que pedir
"CALENDAR_NAME" en PROFILE_VARS[discos_pendientes] resolvía al valor
equivocado. No toca STATUS de ningún VTODO en CALENDAR_TASKS (eso lo usa
dieta_sonora/cal_to_estadisticas.py para "escuchado") ni ningún otro
campo fuera de SUMMARY/DESCRIPTION del VEVENT que matchea.
"""
import os
from datetime import date
from urllib.parse import urlsplit

import requests
from icalendar import Calendar

import radicale_client as rc
from normalize import normalize

RELEASE_CALENDAR_NAME = os.environ.get("RELEASE_CALENDAR_NAME", "")

_ICON_DOWNLOADED = "💾"
_DESCARGADO_PREFIX = "Descargado: "


def fetch_release_events() -> list[dict]:
    """VEVENTs del calendario de lanzamientos, parseados.

    Devuelve uid, href, artist, album, artist_norm, album_norm,
    ical_text (crudo, para poder reescribirlo preservando el resto de
    campos). Descarta VEVENTs sin álbum reconocible, igual que
    fetch_pending_tasks() con los VTODO.
    """
    events = []
    for item in rc.fetch_calendar_items(RELEASE_CALENDAR_NAME):
        try:
            cal = Calendar.from_ical(item["ical_text"])
        except Exception:
            continue
        for comp in cal.walk():
            if getattr(comp, "name", "") != "VEVENT":
                continue
            summary = str(comp.get("SUMMARY", ""))
            artist, album = rc.parse_summary(summary)
            if not album:
                continue
            uid = str(comp.get("UID", ""))
            if not uid:
                continue
            events.append({
                "uid": uid,
                "href": item["href"],
                "artist": artist,
                "album": album,
                "artist_norm": normalize(artist),
                "album_norm": normalize(album),
                "ical_text": item["ical_text"],
            })
    return events


def _replace_icon(summary: str) -> str:
    """Quita cualquier icono existente al principio (💿/📀/🎵/otro) y
    antepone el de "descargado"."""
    return f"{_ICON_DOWNLOADED} {rc._strip_emojis(summary)}"


def _add_downloaded_line(description: str, download_date: date) -> str:
    if _DESCARGADO_PREFIX in description:
        return description  # ya marcado, idempotente
    line = f"{_ICON_DOWNLOADED} {_DESCARGADO_PREFIX}{download_date.isoformat()}"
    return f"{description}\n{line}" if description else line


def mark_downloaded(artist: str, album: str, download_date: date) -> bool:
    """Busca el VEVENT de lanzamiento de (artist, album) y le añade el
    icono de descargado + la fecha. Devuelve False (no es un error) si
    no hay VEVENT correspondiente -- no todas las tareas pendientes
    tienen un lanzamiento rastreado en el calendario."""
    artist_norm, album_norm = normalize(artist), normalize(album)
    match = next(
        (e for e in fetch_release_events()
         if e["artist_norm"] == artist_norm and e["album_norm"] == album_norm),
        None,
    )
    if not match:
        return False

    cal = Calendar.from_ical(match["ical_text"])
    for comp in cal.walk():
        if getattr(comp, "name", "") != "VEVENT":
            continue
        comp["SUMMARY"] = _replace_icon(str(comp.get("SUMMARY", "")))
        comp["DESCRIPTION"] = _add_downloaded_line(str(comp.get("DESCRIPTION", "")), download_date)

    # rc.RADICALE_URL ya incluye el path base (ej. ".../pollo") y `href`
    # (del REPORT) es ya una ruta absoluta desde la raíz del servidor que
    # también empieza por ese mismo path -- concatenarlos tal cual duplica
    # el prefijo (visto en producción: ".../pollo/pollo/..." -> 403).
    # Solo hace falta el origin (scheme+host) de RADICALE_URL.
    origin = urlsplit(rc.RADICALE_URL)
    url = f"{origin.scheme}://{origin.netloc}{match['href']}"
    resp = requests.put(
        url,
        data=cal.to_ical(),
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        auth=(rc.RADICALE_USER, rc.RADICALE_PW),
        timeout=30,
    )
    return resp.status_code in (200, 201, 204)
