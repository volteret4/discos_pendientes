import radicale_client as rc

_VCAL_TMPL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VTODO
UID:{uid}
SUMMARY:{summary}
{extra}STATUS:{status}
END:VTODO
END:VCALENDAR
"""


def _vcal(uid, summary, status="NEEDS-ACTION", dtstart=None):
    extra = f"DTSTART;VALUE=DATE:{dtstart}\n" if dtstart else ""
    return _VCAL_TMPL.format(uid=uid, summary=summary, status=status, extra=extra)


def _report_xml(items):
    responses = ""
    for href, ical_text in items:
        escaped = ical_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        responses += f"""
        <D:response xmlns:D="DAV:">
            <D:href>{href}</D:href>
            <D:propstat>
                <D:prop>
                    <D:getetag>"1"</D:getetag>
                    <C:calendar-data xmlns:C="urn:ietf:params:xml:ns:caldav">{escaped}</C:calendar-data>
                </D:prop>
            </D:propstat>
        </D:response>"""
    return f"""<?xml version="1.0"?>
    <D:multistatus xmlns:D="DAV:">{responses}</D:multistatus>"""


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


def test_strip_emojis_and_parse_summary():
    assert rc.parse_summary("Jens Lekman - Other People") == ("Jens Lekman", "Other People")
    assert rc.parse_summary("🎧 King Gizzard & The Lizard Wizard - Alien Metal") == (
        "King Gizzard & The Lizard Wizard", "Alien Metal",
    )
    assert rc.parse_summary("SinSeparador") == ("SinSeparador", "")


def test_fetch_pending_tasks(monkeypatch):
    items = [
        ("/cal/1.ics", _vcal("uid-1", "Jens Lekman - Other People", dtstart="20260727")),
        ("/cal/2.ics", _vcal("uid-2", "🎧 King Gizzard & The Lizard Wizard - Alien Metal")),
        ("/cal/3.ics", _vcal("uid-3", "Sparks - Too Operatic", status="COMPLETED", dtstart="20241218")),
        ("/cal/4.ics", _vcal("uid-4", "SinAlbumReconocible")),  # sin " - ", se descarta
    ]
    xml = _report_xml(items)

    def fake_request(method, url, **kwargs):
        assert method == "REPORT"
        return _FakeResponse(xml.encode("utf-8"))

    monkeypatch.setattr(rc.requests, "request", fake_request)
    monkeypatch.setattr(rc, "CALENDAR_TASKS", "tasks-cal")
    monkeypatch.setattr(rc, "RADICALE_URL", "https://radicale.example/pollo/")
    monkeypatch.setattr(rc, "RADICALE_BASE", "")

    tasks = rc.fetch_pending_tasks()

    assert len(tasks) == 3  # el de "SinAlbumReconocible" se descarta
    by_uid = {t["uid"]: t for t in tasks}
    assert by_uid["uid-1"]["artist"] == "Jens Lekman"
    assert by_uid["uid-1"]["album"] == "Other People"
    assert by_uid["uid-1"]["status"] == "NEEDS-ACTION"
    assert by_uid["uid-1"]["dtstart"] is not None

    assert by_uid["uid-2"]["artist"] == "King Gizzard & The Lizard Wizard"
    assert by_uid["uid-2"]["dtstart"] is None  # sin DTSTART

    assert by_uid["uid-3"]["status"] == "COMPLETED"
