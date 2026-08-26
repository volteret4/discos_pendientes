"""Lectura de VTODOs de Radicale por HTTP crudo (REPORT), sin el paquete
`caldav` -- mismo patron que
services/dieta_sonora/estadisticas/albums_log/{airsonic,qbittorrent}_checker.py.

v1 no escribe en Radicale: cal_to_estadisticas.py ya usa
VTODO STATUS=COMPLETED en este mismo calendario para marcar "escuchado"
(via scrobbles de Last.fm) -- escribir aqui tambien pisaria esa semantica.
El estado "descargado" vive solo en la DB propia de este servicio.
"""
import os
import re
from datetime import date, datetime
from xml.etree import ElementTree as ET

import requests
from icalendar import Calendar

from normalize import normalize

RADICALE_URL = os.environ.get("RADICALE_URL", "").rstrip("/")
RADICALE_USER = os.environ.get("RADICALE_USERNAME", "")
RADICALE_PW = os.environ.get("RADICALE_PW", "")
RADICALE_BASE = os.environ.get("RADICALE_CALENDAR", "").rstrip("/")
CALENDAR_TASKS = os.environ.get("CALENDAR_TASKS", "")


_EMOJI_RANGES = "".join(chr(a) + "-" + chr(b) for a, b in (
    (0x10000, 0x10ffff),  # supplementary planes (most emoji)
    (0x2000, 0x2bff),     # general punctuation .. misc symbols/arrows
    (0x2600, 0x26ff),     # misc symbols
    (0x2700, 0x27bf),     # dingbats
))
_EMOJI_RE = re.compile(f"^[{_EMOJI_RANGES}\\s]+|[{_EMOJI_RANGES}\\s]+$")


def _strip_emojis(s: str) -> str:
    return _EMOJI_RE.sub("", s).strip()


def parse_summary(summary: str) -> tuple[str, str]:
    summary = _strip_emojis(summary or "")
    parts = re.split(r"\s+[-–—]\s+", summary, maxsplit=1)
    if len(parts) == 2:
        return _strip_emojis(parts[0]), _strip_emojis(parts[1])
    return summary, ""


def parse_date(dt_val) -> date | None:
    if dt_val is None:
        return None
    if hasattr(dt_val, "dt"):
        dt_val = dt_val.dt
    if isinstance(dt_val, datetime):
        return dt_val.date()
    if isinstance(dt_val, date):
        return dt_val
    return None


def fetch_calendar_items(cal_name: str) -> list[dict]:
    url = f"{RADICALE_URL}{RADICALE_BASE}/{cal_name}/"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "  <D:prop><D:getetag/><C:calendar-data/></D:prop>"
        '  <C:filter><C:comp-filter name="VCALENDAR"/></C:filter>'
        "</C:calendar-query>"
    )
    r = requests.request(
        "REPORT", url,
        data=body.encode("utf-8"),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        auth=(RADICALE_USER, RADICALE_PW),
        timeout=30,
    )
    r.raise_for_status()
    ns = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}
    root = ET.fromstring(r.content)
    items = []
    for resp in root.findall(".//D:response", ns):
        href_el = resp.find("D:href", ns)
        cal_data = resp.find(".//C:calendar-data", ns)
        if href_el is not None and cal_data is not None and cal_data.text:
            items.append({"href": href_el.text, "ical_text": cal_data.text})
    return items


def fetch_pending_tasks() -> list[dict]:
    """VTODOs del calendario "discos pendientes", parseados.

    Devuelve una lista de dicts: uid, href, artist, album, artist_norm,
    album_norm, status, dtstart (date|None), summary_raw. Descarta VTODOs
    sin album reconocible (SUMMARY sin separador " - ").
    """
    tasks = []
    for item in fetch_calendar_items(CALENDAR_TASKS):
        try:
            cal = Calendar.from_ical(item["ical_text"])
        except Exception:
            continue
        for comp in cal.walk():
            if getattr(comp, "name", "") != "VTODO":
                continue
            summary = str(comp.get("SUMMARY", ""))
            artist, album = parse_summary(summary)
            if not album:
                continue
            uid = str(comp.get("UID", ""))
            if not uid:
                continue
            tasks.append({
                "uid": uid,
                "href": item["href"],
                "artist": artist,
                "album": album,
                "artist_norm": normalize(artist),
                "album_norm": normalize(album),
                "status": str(comp.get("STATUS", "NEEDS-ACTION")),
                "dtstart": parse_date(comp.get("DTSTART")),
                "summary_raw": summary,
            })
    return tasks
