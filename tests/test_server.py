from datetime import date

import pytest

import server
import sync


def _fake_task(uid, artist, album, status="NEEDS-ACTION", dtstart=date(2026, 1, 1)):
    return {
        "uid": uid, "href": f"/cal/{uid}.ics", "artist": artist, "album": album,
        "artist_norm": artist.lower(), "album_norm": album.lower(),
        "status": status, "dtstart": dtstart, "summary_raw": f"{artist} - {album}",
    }


@pytest.fixture
def client(patched_db, monkeypatch):
    monkeypatch.setattr(sync.radicale_client, "fetch_pending_tasks", lambda: [
        _fake_task("uid-1", "Jens Lekman", "Other People", dtstart=date(2026, 1, 10)),
        _fake_task("uid-2", "Sparks", "Too Operatic", status="COMPLETED", dtstart=date(2024, 1, 1)),
    ])
    sync.sync_tasks()

    conn = patched_db.get_conn()
    conn.execute(
        "INSERT INTO pending_status (uid, downloaded, checked_at) VALUES (?,?,?)",
        ("uid-1", 1, "2026-01-11T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_api_tasks_default_pending(client):
    r = client.get("/api/tasks")
    data = r.get_json()
    assert data["total"] == 1
    assert data["items"][0]["uid"] == "uid-1"
    assert data["items"][0]["downloaded"] == 1


def test_api_tasks_status_all(client):
    r = client.get("/api/tasks?status=all")
    data = r.get_json()
    assert data["total"] == 2


def test_api_tasks_downloaded_filter(client):
    r = client.get("/api/tasks?status=all&downloaded=no")
    data = r.get_json()
    assert data["total"] == 1
    assert data["items"][0]["uid"] == "uid-2"


def test_api_tasks_artist_filter(client):
    r = client.get("/api/tasks?status=all&artist=Lekman")
    data = r.get_json()
    assert data["total"] == 1
    assert data["items"][0]["artist"] == "Jens Lekman"


def test_api_catalog_search(client):
    r = client.get("/api/catalog/search")
    data = r.get_json()
    assert data["total"] == 1
    assert data["items"][0]["artist"] == "Boards of Canada"


def test_api_catalog_search_by_genre(client):
    r = client.get("/api/catalog/search?genre=IDM")
    assert r.get_json()["total"] == 1
    r = client.get("/api/catalog/search?genre=NoExiste")
    assert r.get_json()["total"] == 0


def test_api_catalog_filters_meta(client):
    r = client.get("/api/catalog/filters/meta")
    data = r.get_json()
    assert "IDM" in data["genres"]
    assert "Warp Records" in data["labels"]


def test_api_catalog_album_detail(client):
    r = client.get("/api/catalog/album/1")
    data = r.get_json()
    assert data["album"]["name"] == "Music Has the Right to Children"
    assert data["label"]["name"] == "Warp Records"


def test_api_catalog_album_not_found(client):
    r = client.get("/api/catalog/album/999")
    assert r.status_code == 404


def test_api_task_enrichment_local(client, monkeypatch):
    conn = server.get_conn()
    conn.execute(
        "INSERT INTO tasks (uid, href, artist, album, artist_norm, album_norm, status, dtstart, summary_raw, synced_at) "
        "VALUES ('uid-3', '/cal/3.ics', 'Boards of Canada', 'Music Has the Right to Children', "
        "'boards of canada', 'music has the right to children', 'NEEDS-ACTION', '2026-01-01', 'x', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    r = client.get("/api/tasks/uid-3/enrichment")
    data = r.get_json()
    assert data["source"] == "local_db"
    assert data["album"]["name"] == "Music Has the Right to Children"


def test_api_task_enrichment_not_found(client):
    r = client.get("/api/tasks/no-existe/enrichment")
    assert r.status_code == 404
