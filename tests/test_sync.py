from datetime import date, datetime, timedelta, timezone

import sync


def _fake_task(uid, artist, album, status="NEEDS-ACTION", dtstart=date(2026, 1, 1)):
    return {
        "uid": uid, "href": f"/cal/{uid}.ics", "artist": artist, "album": album,
        "artist_norm": artist.lower(), "album_norm": album.lower(),
        "status": status, "dtstart": dtstart, "summary_raw": f"{artist} - {album}",
    }


def test_sync_tasks_upsert_idempotent(patched_db, monkeypatch):
    tasks = [_fake_task("uid-1", "Artista", "Album 1"), _fake_task("uid-2", "Artista", "Album 2")]
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: tasks)

    n1 = sync.sync_tasks()
    n2 = sync.sync_tasks()  # segunda pasada, no debe duplicar filas

    conn = patched_db.get_conn()
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()

    assert n1 == 2 and n2 == 2
    assert total == 2


def test_sync_downloaded_status_writes_pending_status(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: [
        _fake_task("uid-1", "Boards of Canada", "Geogaddi"),
    ])
    sync.sync_tasks()

    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: date(2026, 2, 1))
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    n = sync.sync_downloaded_status(stale_only=False)
    assert n == 1

    conn = patched_db.get_conn()
    row = conn.execute("SELECT * FROM pending_status WHERE uid = 'uid-1'").fetchone()
    conn.close()
    assert row["downloaded"] == 1
    assert row["airsonic_found"] == 1
    assert row["qbittorrent_found"] == 0


def test_sync_downloaded_status_stale_only_skips_fresh(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: [
        _fake_task("uid-1", "Artista", "Album"),
    ])
    sync.sync_tasks()

    conn = patched_db.get_conn()
    conn.execute(
        "INSERT INTO pending_status (uid, downloaded, checked_at) VALUES (?,?,?)",
        ("uid-1", 0, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    calls = {"n": 0}

    def fake_search_airsonic(artist, album):
        calls["n"] += 1
        return None

    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", fake_search_airsonic)
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    n = sync.sync_downloaded_status(stale_only=True)
    assert n == 0
    assert calls["n"] == 0  # recién comprobado, no se vuelve a tocar


def test_sync_downloaded_status_stale_only_rechecks_old(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: [
        _fake_task("uid-1", "Artista", "Album"),
    ])
    sync.sync_tasks()

    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn = patched_db.get_conn()
    conn.execute("INSERT INTO pending_status (uid, downloaded, checked_at) VALUES (?,?,?)", ("uid-1", 0, old))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: None)
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    n = sync.sync_downloaded_status(stale_only=True)
    assert n == 1  # más de 24h -- se vuelve a comprobar


def _fake_event(uid, artist, album):
    return {
        "uid": uid, "href": f"/pollo/cal/{uid}.ics", "artist": artist, "album": album,
        "artist_norm": artist.lower(), "album_norm": album.lower(),
        "ical_text": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    }


def test_sync_release_calendar_marks_downloaded_album_not_in_pending_list(patched_db, monkeypatch):
    # Bonobo/FKJ nunca pasaron por la lista de "discos_pendientes" (tasks
    # vacío) pero sí tienen VEVENT en el calendario de lanzamientos y ya
    # están descargados -- deben marcarse igual.
    monkeypatch.setattr(sync.radicale_writer, "fetch_release_events", lambda: [
        _fake_event("uid-rel-1", "Bonobo", "Distance in Static"),
    ])
    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: date(2026, 9, 12))
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    calls = []
    monkeypatch.setattr(sync.radicale_writer, "mark_downloaded",
                         lambda artist, album, d, events=None: calls.append((artist, album, d)) or True)

    stats = sync.sync_release_calendar(stale_only=False)
    assert stats["marked"] == 1
    assert calls == [("Bonobo", "Distance in Static", date(2026, 9, 12))]

    conn = patched_db.get_conn()
    row = conn.execute("SELECT radicale_marked_at FROM release_marks WHERE uid = 'uid-rel-1'").fetchone()
    conn.close()
    assert row["radicale_marked_at"] is not None

    # segunda pasada: ya marcado -- no se vuelve a llamar ni a comprobar Airsonic
    sync.sync_release_calendar(stale_only=False)
    assert len(calls) == 1


def test_sync_release_calendar_not_downloaded_stays_unmarked(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_writer, "fetch_release_events", lambda: [
        _fake_event("uid-rel-2", "Artista", "Album"),
    ])
    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: None)
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    calls = []
    monkeypatch.setattr(sync.radicale_writer, "mark_downloaded",
                         lambda artist, album, d, events=None: calls.append((artist, album, d)) or True)

    stats = sync.sync_release_calendar(stale_only=False)
    assert stats["marked"] == 0
    assert stats["still_pending"] == 1
    assert calls == []


def test_sync_release_calendar_stale_only_skips_recently_checked(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_writer, "fetch_release_events", lambda: [
        _fake_event("uid-rel-3", "Artista", "Album"),
    ])
    conn = patched_db.get_conn()
    conn.execute(
        "INSERT INTO release_marks (uid, artist, album, downloaded, checked_at) VALUES (?,?,?,?,?)",
        ("uid-rel-3", "Artista", "Album", 0, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    calls = {"n": 0}

    def fake_search_airsonic(artist, album):
        calls["n"] += 1
        return None

    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", fake_search_airsonic)
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    stats = sync.sync_release_calendar(stale_only=True)
    assert stats["total"] == 0
    assert calls["n"] == 0


def test_sync_release_calendar_dry_run_does_not_write(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_writer, "fetch_release_events", lambda: [
        _fake_event("uid-rel-4", "Bonobo", "Distance in Static"),
    ])
    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: date(2026, 9, 12))
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    calls = []
    monkeypatch.setattr(sync.radicale_writer, "mark_downloaded",
                         lambda artist, album, d, events=None: calls.append((artist, album, d)) or True)

    stats = sync.sync_release_calendar(stale_only=False, dry_run=True)
    assert stats["marked"] == 1
    assert calls == []  # dry-run: cuenta pero no llama a mark_downloaded

    conn = patched_db.get_conn()
    row = conn.execute("SELECT * FROM release_marks WHERE uid = 'uid-rel-4'").fetchone()
    conn.close()
    assert row is None  # dry-run: tampoco escribe en la DB


def test_sync_album_first_listens_writes_and_is_idempotent(patched_db):
    n1 = sync.sync_album_first_listens()
    n2 = sync.sync_album_first_listens()  # segunda pasada, no debe duplicar filas

    conn = patched_db.get_conn()
    rows = conn.execute("SELECT * FROM album_first_listen").fetchall()
    conn.close()

    assert n1 == 1 and n2 == 1
    assert len(rows) == 1
    assert rows[0]["artist_norm"] == "boards of canada"
    assert rows[0]["first_ts"] == 1265000000
