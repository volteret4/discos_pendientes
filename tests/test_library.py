"""Pestañas Sin escuchas / Escuchados y playlists de Navidrome."""
import sqlite3

import pytest

import listens
import navidrome_client
import server
import sync


def _index(albums=None, tracks=None):
    return {"albums": albums or {}, "tracks": tracks or {}}


def _song(song_id, title, artist="Sonic Youth", album="Bad Moon Rising"):
    return {"id": song_id, "title": title, "artist": artist, "album": album, "albumId": "al"}


ALBUM = {"id": "al", "name": "Bad Moon Rising", "artist": "Sonic Youth", "songCount": 2}


# ── Cruce ────────────────────────────────────────────────────────────────


def test_listen_key_unifies_typography_but_stays_exact():
    assert listens.listen_key("Takk…") == listens.listen_key("takk...")
    assert listens.listen_key("Polishin’ Up") == listens.listen_key("polishin' up")
    assert listens.listen_key("Takk") != listens.listen_key("Takk...")


def test_album_tag_match_gives_dates_and_plays():
    index = _index(albums={("sonic youth", "bad moon rising"): (100, 300, 7)})
    m = listens.match_album(ALBUM, [_song("1", "Intro")], index)
    assert (m["match_kind"], m["first_ts"], m["last_ts"], m["plays"]) == ("album", 100, 300, 7)


def test_compilation_matches_by_song_artist_not_various_artists():
    album = {"id": "al", "name": "Panorama Bar 05", "artist": "Various Artists"}
    song = _song("1", "Track", artist="Steffi", album="Panorama Bar 05")
    index = _index(albums={("steffi", "panorama bar 05"): (1, 2, 3)})
    assert listens.match_album(album, [song], index)["match_kind"] == "album"


def test_half_of_tracks_counts_as_listened_with_track_dates():
    songs = [_song("1", "A"), _song("2", "B")]
    index = _index(tracks={("sonic youth", "a"): (50, 60, 4)})
    m = listens.match_album(ALBUM, songs, index)
    assert (m["match_kind"], m["tracks_heard"], m["first_ts"], m["last_ts"], m["plays"]) == ("tracks", 1, 50, 60, 4)


def test_stray_track_below_threshold_is_not_listened():
    songs = [_song(str(i), f"T{i}") for i in range(7)]
    index = _index(tracks={("sonic youth", "t0"): (50, 60, 4)})
    m = listens.match_album(ALBUM, songs, index)
    assert (m["match_kind"], m["tracks_heard"], m["first_ts"], m["plays"]) == (None, 1, None, 0)


def test_album_without_songs_does_not_crash():
    assert listens.match_album(ALBUM, [], _index())["match_kind"] is None


def test_load_listen_index_covers_both_sources_and_skips_null_album(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE artists (artist_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE albums (album_id INTEGER PRIMARY KEY, artist_id INTEGER, name TEXT);
        CREATE TABLE scrobbles (artist_id INTEGER, album_id INTEGER, track TEXT, ts INTEGER, source TEXT);
        INSERT INTO artists VALUES (1, 'Björk');
        INSERT INTO albums VALUES (10, 1, 'Post');
        INSERT INTO scrobbles VALUES (1, 10, 'Army of Me', 100, 'listenbrainz');
        INSERT INTO scrobbles VALUES (1, 10, 'Army of Me', 200, 'lastfm');
        INSERT INTO scrobbles VALUES (1, NULL, 'Hyperballad', 300, 'lastfm');
        """
    )
    index = listens.load_listen_index(conn)
    assert index["albums"] == {("bjork", "post"): (100, 200, 2)}
    assert index["tracks"][("bjork", "army of me")] == (100, 200, 2)
    assert index["tracks"][("bjork", "hyperballad")] == (300, 300, 1)


# ── Cliente ──────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return {"subsonic-response": self.body}


@pytest.fixture
def nd_env(monkeypatch):
    monkeypatch.setenv("NAVIDROME_URL", "http://nd")
    monkeypatch.setenv("NAVIDROME_USER", "music")
    monkeypatch.setenv("NAVIDROME_PASS", "s3cret")


def test_credentials_never_sent_in_clear_or_in_url(nd_env, monkeypatch):
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, data))
        return _Resp({"status": "ok", "playlists": {"playlist": []}})

    monkeypatch.setattr(navidrome_client.requests, "post", fake_post)
    navidrome_client.editable_playlists()
    url, data = calls[0]
    assert "s3cret" not in url and "s3cret" not in data.values() and "p" not in data
    assert data["u"] == "music" and data["t"] and data["s"]


def test_network_error_message_hides_request_details(nd_env, monkeypatch):
    def boom(*a, **k):
        raise navidrome_client.requests.ConnectionError("http://nd/rest/x?t=SECRET")

    monkeypatch.setattr(navidrome_client.requests, "post", boom)
    with pytest.raises(navidrome_client.NavidromeError) as e:
        navidrome_client.fetch_albums()
    assert "SECRET" not in str(e.value)


def test_unconfigured_raises_clear_error(monkeypatch):
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.delenv("NAVIDROME_PASS", raising=False)
    assert not navidrome_client.is_configured()
    with pytest.raises(navidrome_client.NavidromeError, match="NAVIDROME_USER"):
        navidrome_client.fetch_albums()


def test_fetch_albums_pages_until_short_page(nd_env, monkeypatch):
    monkeypatch.setattr(navidrome_client, "PAGE_SIZE", 2)
    offsets = []

    def fake_post(url, data, timeout):
        offsets.append(data["offset"])
        count = 2 if data["offset"] == 0 else 1
        return _Resp({"status": "ok", "albumList2": {"album": [{"id": f"{data['offset']}-{i}"} for i in range(count)]}})

    monkeypatch.setattr(navidrome_client.requests, "post", fake_post)
    assert len(navidrome_client.fetch_albums()) == 3
    assert offsets == [0, 2]


def test_editable_playlists_only_returns_own(nd_env, monkeypatch):
    monkeypatch.setattr(navidrome_client.requests, "post", lambda *a, **k: _Resp({"status": "ok", "playlists": {"playlist": [
        {"id": "1", "name": "mía", "owner": "music", "songCount": 3},
        {"id": "2", "name": "ajena", "owner": "mix"},
    ]}}))
    assert [p["id"] for p in navidrome_client.editable_playlists()] == ["1"]


# ── Sync ─────────────────────────────────────────────────────────────────


@pytest.fixture
def library_db(patched_db, monkeypatch, tmp_path):
    """lastfm con columna track, que el fixture común no tiene."""
    path = tmp_path / "lastfm_library.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE artists (artist_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE albums (album_id INTEGER PRIMARY KEY, artist_id INTEGER, name TEXT);
        CREATE TABLE scrobbles (artist_id INTEGER, album_id INTEGER, track TEXT, ts INTEGER);
        INSERT INTO artists VALUES (1, 'Oído');
        INSERT INTO albums VALUES (10, 1, 'Viejo');
        INSERT INTO albums VALUES (11, 1, 'Reciente');
        INSERT INTO scrobbles VALUES (1, 10, 'x', 1000);
        INSERT INTO scrobbles VALUES (1, 11, 'y', 2000);
        INSERT INTO scrobbles VALUES (1, 11, 'y', 3000);
        """
    )
    conn.commit()
    conn.close()

    def lastfm():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(sync, "get_lastfm_conn", lastfm)
    monkeypatch.setattr(navidrome_client, "fetch_albums", lambda: [
        {"id": "a1", "name": "Viejo", "artist": "Oído", "year": 1990, "genre": "Rock", "songCount": 1, "created": "2020-01-01T00:00:00Z"},
        {"id": "a2", "name": "Reciente", "artist": "Oído", "year": 2020, "songCount": 1, "created": "2021-01-01T00:00:00Z"},
        {"id": "a3", "name": "Virgen", "artist": "Nadie", "year": 2001, "songCount": 1, "created": "2022-01-01T00:00:00Z"},
    ])
    monkeypatch.setattr(navidrome_client, "fetch_songs", lambda: [])
    return patched_db


def test_sync_library_failure_keeps_previous_data(library_db, monkeypatch):
    sync.sync_library()

    def down():
        raise navidrome_client.NavidromeError("caído")

    monkeypatch.setattr(navidrome_client, "fetch_albums", down)
    with pytest.raises(navidrome_client.NavidromeError):
        sync.sync_library()
    monkeypatch.setattr(navidrome_client, "fetch_albums", lambda: [])
    with pytest.raises(navidrome_client.NavidromeError):
        sync.sync_library()

    conn = library_db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM library_albums").fetchone()[0] == 3
    conn.close()


def test_sync_library_removes_albums_gone_from_navidrome(library_db, monkeypatch):
    sync.sync_library()
    monkeypatch.setattr(navidrome_client, "fetch_albums", lambda: [{"id": "a3", "name": "Virgen", "artist": "Nadie"}])
    sync.sync_library()
    conn = library_db.get_conn()
    assert [r["id"] for r in conn.execute("SELECT id FROM library_albums")] == ["a3"]
    conn.close()


def test_cron_pass_survives_missing_config(monkeypatch, capsys):
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    sync._sync_library_safe()
    assert "omitido" in capsys.readouterr().out


# ── Endpoints ────────────────────────────────────────────────────────────


@pytest.fixture
def client(library_db):
    sync.sync_library()
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def _ids(client, **params):
    return [i["id"] for i in client.get("/api/library", query_string=params).get_json()["items"]]


def test_tabs_are_disjoint_and_cover_library(client):
    assert _ids(client, listened="no") == ["a3"]
    assert sorted(_ids(client, listened="yes")) == ["a1", "a2"]


def test_listened_sorts_by_first_and_last_listen(client):
    assert _ids(client, listened="yes", sort="first_ts", dir="asc") == ["a1", "a2"]
    assert _ids(client, listened="yes", sort="last_ts", dir="desc") == ["a2", "a1"]
    assert _ids(client, listened="yes", sort="plays", dir="desc") == ["a2", "a1"]


def test_unknown_sort_is_not_injected(client):
    assert sorted(_ids(client, listened="yes", sort="plays; DROP TABLE library_albums")) == ["a1", "a2"]


def test_filters(client):
    assert _ids(client, listened="yes", year_from="2000") == ["a2"]
    assert _ids(client, listened="yes", year_to="2000") == ["a1"]
    assert _ids(client, listened="yes", genre="rock") == ["a1"]
    assert _ids(client, listened="no", artist="zzz") == []


def test_status(client):
    data = client.get("/api/library/status").get_json()
    assert (data["total"], data["heard"]) == (3, 2)


def test_sync_endpoint_without_config_is_503(client, monkeypatch):
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    assert client.post("/api/library/sync").status_code == 503


@pytest.fixture
def nd_fake(monkeypatch):
    """Navidrome falso: la playlist p1 ya tiene s1."""
    state = {"created": [], "added": []}
    albums = {"a1": ["s1", "s2"], "a2": ["s2", "s3"]}

    def song_ids(album_id):
        if album_id not in albums:
            raise navidrome_client.NavidromeError("not found")
        return albums[album_id]

    monkeypatch.setattr(navidrome_client, "album_song_ids", song_ids)
    monkeypatch.setattr(navidrome_client, "playlist_info", lambda pid: {"name": "Mía", "song_ids": ["s1"]})
    monkeypatch.setattr(navidrome_client, "create_playlist", lambda name, ids: state["created"].append((name, ids)) or "new-id")
    monkeypatch.setattr(navidrome_client, "add_to_playlist", lambda pid, ids: state["added"].append((pid, ids)))
    return state


def _post(client, **body):
    return client.post("/api/navidrome/playlist", json=body)


def test_create_playlist_keeps_order_without_duplicates(client, nd_fake):
    r = _post(client, album_ids=["a2", "a1"], name=" Nueva ")
    assert r.status_code == 200
    assert nd_fake["created"] == [("Nueva", ["s2", "s3", "s1"])]
    assert r.get_json()["playlist_id"] == "new-id"


def test_add_skips_songs_already_in_playlist(client, nd_fake):
    r = _post(client, album_ids=["a1"], playlist_id="p1")
    assert nd_fake["added"] == [("p1", ["s2"])]
    assert r.get_json()["added"] == 1


def test_add_nothing_new_does_not_write(client, nd_fake, monkeypatch):
    monkeypatch.setattr(navidrome_client, "playlist_info", lambda pid: {"name": "Mía", "song_ids": ["s1", "s2"]})
    r = _post(client, album_ids=["a1"], playlist_id="p1")
    assert r.status_code == 200 and r.get_json()["added"] == 0 and nd_fake["added"] == []


def test_partial_failure_reports_album_and_adds_the_rest(client, nd_fake):
    r = _post(client, album_ids=["a1", "gone"], playlist_id="p1")
    assert r.get_json()["failed_albums"] == ["gone"]
    assert nd_fake["added"] == [("p1", ["s2"])]


def test_all_albums_failing_creates_no_playlist(client, nd_fake):
    r = _post(client, album_ids=["gone"], name="X")
    assert r.status_code == 502 and nd_fake["created"] == []


@pytest.mark.parametrize("body", [
    {"album_ids": [], "name": "X"},
    {"album_ids": "a1", "name": "X"},
    {"album_ids": [1], "name": "X"},
    {"album_ids": ["a1"]},
    {"album_ids": ["a1"], "name": "X", "playlist_id": "p1"},
    {"album_ids": ["a"] * 201, "name": "X"},
])
def test_invalid_requests_are_rejected(client, nd_fake, body):
    assert _post(client, **body).status_code == 400
    assert nd_fake["created"] == [] and nd_fake["added"] == []


def test_foreign_playlist_error_is_reported(client, nd_fake, monkeypatch):
    def foreign(pid):
        raise navidrome_client.NavidromeError("La playlist «x» no es de music")

    monkeypatch.setattr(navidrome_client, "playlist_info", foreign)
    r = _post(client, album_ids=["a1"], playlist_id="p9")
    assert r.status_code == 502 and "no es de music" in r.get_json()["error"]
