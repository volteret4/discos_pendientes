import os
import sqlite3
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


def test_api_catalog_search_includes_staleness(client):
    it = client.get("/api/catalog/search").get_json()["items"][0]
    assert it["in_qbittorrent"] is True
    assert it["days_since"] is not None and it["days_since"] > 365


def test_api_catalog_search_staleness_filters(client):
    assert client.get("/api/catalog/search?qbittorrent=yes").get_json()["total"] == 1
    assert client.get("/api/catalog/search?qbittorrent=no").get_json()["total"] == 0
    assert client.get("/api/catalog/search?never_listened=yes").get_json()["total"] == 0
    assert client.get("/api/catalog/search?never_listened=no&genre=IDM").get_json()["total"] == 1
    assert client.get("/api/catalog/search?never_listened=no&genre=NoExiste").get_json()["total"] == 0


def test_api_catalog_search_sort_by_days_since(client):
    data = client.get("/api/catalog/search?sort=days_since&dir=desc").get_json()
    assert data["total"] == 1
    assert data["items"][0]["artist"] == "Boards of Canada"


def test_api_catalog_artists(client):
    data = client.get("/api/catalog/artists").get_json()
    assert data["total"] == 1
    a = data["items"][0]
    assert a["artist"] == "Boards of Canada"
    assert a["album_count"] == 1 and a["backed_up_count"] == 1
    assert client.get("/api/catalog/artists?never_listened=yes").get_json()["total"] == 0


def test_api_catalog_staleness_refresh(client):
    data = client.post("/api/catalog/staleness/refresh").get_json()
    assert data["ok"] and data["computed_at"]
    assert client.get("/api/catalog/staleness/status").get_json()["computed_at"] == data["computed_at"]


def test_api_tasks_sort(client):
    r = client.get("/api/tasks?status=all&sort=artist&dir=asc").get_json()
    names = [it["artist"] for it in r["items"]]
    assert names == sorted(names, key=str.lower)
    r = client.get("/api/tasks?status=all&sort=artist&dir=desc").get_json()
    assert [it["artist"] for it in r["items"]] == sorted(names, key=str.lower, reverse=True)
    assert client.get("/api/tasks?status=all&sort=bogus").status_code == 200


def test_api_catalog_search_sort_by_first_listened(client):
    sync.sync_album_first_listens()
    data = client.get("/api/catalog/search?sort=first_listened&dir=asc").get_json()
    assert data["total"] == 1
    assert data["items"][0]["first_listened"].startswith("2010-02")


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


def test_api_catalog_search_includes_first_listened(client):
    sync.sync_album_first_listens()
    r = client.get("/api/catalog/search")
    data = r.get_json()
    assert data["items"][0]["artist"] == "Boards of Canada"
    assert data["items"][0]["first_listened"].startswith("2010-02")


def test_api_catalog_artist_listens(client):
    r = client.get("/api/catalog/artist_listens?artist=Boards of Canada")
    data = r.get_json()
    assert data["total_plays"] == 3
    assert data["first_listened"].startswith("2010-02")
    assert len(data["timeline"]) == 3


def test_api_catalog_artist_listens_unknown(client):
    r = client.get("/api/catalog/artist_listens?artist=Nadie Conocido")
    data = r.get_json()
    assert data["timeline"] == []
    assert data["first_listened"] is None


def test_api_catalog_artist_listens_missing_param(client):
    r = client.get("/api/catalog/artist_listens")
    assert r.status_code == 400


def test_api_catalog_search_falls_back_to_artist_first_listened(client, patched_db):
    # Mismo caso real reportado con Sigur Rós: un álbum sin ningún
    # scrobble con tag exacto, pero cuyo artista sí tiene escuchas.
    path = patched_db.MUSICA_LOCAL_DB
    ro_dir = os.path.dirname(path)
    os.chmod(ro_dir, 0o755)
    os.chmod(path, 0o644)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO albums (id, artist_id, name, year) VALUES (2, 1, 'Geogaddi (Reissue)', '2002')")
    conn.commit()
    conn.close()
    os.chmod(path, 0o444)
    os.chmod(ro_dir, 0o555)

    sync.sync_album_first_listens()
    sync.sync_artist_first_listens()

    r = client.get("/api/catalog/search?album=Geogaddi")
    item = r.get_json()["items"][0]
    assert item["first_listened"] is None
    assert item["artist_first_listened"].startswith("2010-02")


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
