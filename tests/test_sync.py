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
    monkeypatch.setattr(sync.radicale_writer, "mark_downloaded", lambda artist, album, d: False)

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


def test_sync_downloaded_status_marks_radicale_on_new_download(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: [
        _fake_task("uid-1", "Boards of Canada", "Geogaddi"),
    ])
    sync.sync_tasks()

    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: date(2026, 2, 1))
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    calls = []
    monkeypatch.setattr(sync.radicale_writer, "mark_downloaded", lambda artist, album, d: calls.append((artist, album, d)) or True)

    sync.sync_downloaded_status(stale_only=False)
    assert calls == [("Boards of Canada", "Geogaddi", date.today())]

    conn = patched_db.get_conn()
    row = conn.execute("SELECT radicale_marked_at FROM pending_status WHERE uid = 'uid-1'").fetchone()
    conn.close()
    assert row["radicale_marked_at"] is not None

    # segunda pasada: sigue descargado y ya marcado -- no se vuelve a llamar
    sync.sync_downloaded_status(stale_only=False)
    assert len(calls) == 1


def test_sync_downloaded_status_does_not_mark_when_still_not_downloaded(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: [
        _fake_task("uid-1", "Artista", "Album"),
    ])
    sync.sync_tasks()

    monkeypatch.setattr(sync, "get_torrents", lambda: [])
    monkeypatch.setattr(sync, "search_airsonic", lambda artist, album: None)
    monkeypatch.setattr(sync, "search_qbittorrent", lambda torrents, artist, album: None)

    calls = []
    monkeypatch.setattr(sync.radicale_writer, "mark_downloaded", lambda artist, album, d: calls.append((artist, album, d)) or True)

    sync.sync_downloaded_status(stale_only=False)
    assert calls == []


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
