from datetime import date

import radicale_writer as rw
from icalendar import Calendar


def _vevent_ical(summary, description="", uid="uid-rel-1"):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//", "BEGIN:VEVENT",
              f"UID:{uid}", f"SUMMARY:{summary}"]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def test_replace_icon_strips_any_existing_icon():
    assert rw._replace_icon("💿 Artist - Title") == "💾 Artist - Title"
    assert rw._replace_icon("📀 Artist - Title") == "💾 Artist - Title"
    assert rw._replace_icon("🎵 Artist - Title") == "💾 Artist - Title"
    assert rw._replace_icon("Artist - Title") == "💾 Artist - Title"


def test_add_downloaded_line_appends_once():
    d = date(2026, 3, 5)
    desc = rw._add_downloaded_line("Release Date for 💿 Artist - Title", d)
    assert "💾 Descargado: 2026-03-05" in desc
    assert desc.startswith("Release Date for")

    # idempotente: llamarlo dos veces no duplica la línea
    desc2 = rw._add_downloaded_line(desc, d)
    assert desc2 == desc


def test_add_downloaded_line_no_prior_description():
    d = date(2026, 3, 5)
    assert rw._add_downloaded_line("", d) == "💾 Descargado: 2026-03-05"


def test_mark_downloaded_puts_modified_ical(monkeypatch):
    raw = _vevent_ical("💿 Boards of Canada - Geogaddi", description="Release Date for 💿 Boards of Canada - Geogaddi")
    monkeypatch.setattr(rw, "fetch_release_events", lambda: [{
        "uid": "uid-rel-1", "href": "/pollo/cal-uuid/uid-rel-1.ics",
        "artist": "Boards of Canada", "album": "Geogaddi",
        "artist_norm": "boards of canada", "album_norm": "geogaddi",
        "ical_text": raw,
    }])
    # RADICALE_URL incluye un path base (como en producción, ".../pollo")
    # y el href devuelto por el REPORT ya es una ruta absoluta que también
    # empieza por ese mismo path -- concatenarlos tal cual duplicaría el
    # prefijo (bug real visto en producción: 403 por URL mal formada).
    monkeypatch.setattr(rw.rc, "RADICALE_URL", "https://radicale.example/pollo")
    monkeypatch.setattr(rw.rc, "RADICALE_USER", "u")
    monkeypatch.setattr(rw.rc, "RADICALE_PW", "p")

    put_calls = []

    class _FakeResp:
        status_code = 204

    def fake_put(url, data=None, headers=None, auth=None, timeout=None):
        put_calls.append((url, data))
        return _FakeResp()

    monkeypatch.setattr(rw.requests, "put", fake_put)

    ok = rw.mark_downloaded("Boards of Canada", "Geogaddi", date(2026, 3, 5))
    assert ok is True
    assert len(put_calls) == 1
    url, body = put_calls[0]
    assert url == "https://radicale.example/pollo/cal-uuid/uid-rel-1.ics"

    cal = Calendar.from_ical(body)
    vevent = next(c for c in cal.walk() if c.name == "VEVENT")
    assert str(vevent["SUMMARY"]) == "💾 Boards of Canada - Geogaddi"
    assert "💾 Descargado: 2026-03-05" in str(vevent["DESCRIPTION"])


def test_mark_downloaded_returns_false_when_no_match(monkeypatch):
    monkeypatch.setattr(rw, "fetch_release_events", lambda: [])
    assert rw.mark_downloaded("Nadie", "Nada", date.today()) is False
