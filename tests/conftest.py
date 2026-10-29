import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402


@pytest.fixture
def musica_local_path(tmp_path):
    # Subcarpeta propia (no tmp_path directamente) -- se deja de solo
    # lectura para reproducir el bind-mount :ro, y no debe afectar a la
    # DB propia del servicio (own_db_path), que sí necesita escribir en
    # tmp_path.
    ro_dir = tmp_path / "musica_local_ro"
    ro_dir.mkdir()
    path = ro_dir / "musica_local.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE artists (
            id INTEGER PRIMARY KEY, name TEXT, bio TEXT,
            wikipedia_content TEXT, wikipedia_url TEXT, lastfm_url TEXT
        );
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY, artist_id INTEGER, name TEXT, year TEXT,
            label TEXT, genre TEXT, wikipedia_content TEXT, wikipedia_url TEXT,
            lastfm_url TEXT, discogs_url TEXT, folder_path TEXT
        );
        CREATE TABLE labels (
            id INTEGER PRIMARY KEY, name TEXT, description TEXT,
            wikipedia_url TEXT, discogs_url TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO artists (id, name, bio) VALUES (1, 'Boards of Canada', 'Scottish electronic duo')"
    )
    conn.execute(
        "INSERT INTO albums (id, artist_id, name, year, label, genre, folder_path) "
        "VALUES (1, 1, 'Music Has the Right to Children', '1998', 'Warp Records', 'IDM', '/music/boc')"
    )
    conn.execute(
        "INSERT INTO labels (id, name, description) VALUES (1, 'Warp Records', 'UK electronic label')"
    )
    conn.commit()
    conn.close()
    # Reproduce el bind-mount :ro de Docker: ni el archivo ni el directorio
    # que lo contiene son escribibles (ver db.py::get_musica_local_conn --
    # mode=ro solo no basta ahí).
    os.chmod(path, 0o444)
    os.chmod(ro_dir, 0o555)
    yield str(path)
    os.chmod(ro_dir, 0o755)  # para que pytest pueda limpiar tmp_path


@pytest.fixture
def own_db_path(tmp_path):
    return str(tmp_path / "discos_pendientes.db")


@pytest.fixture
def lastfm_path(tmp_path):
    # Mismo esquema (artists/albums/scrobbles) ya documentado y usado por
    # services/discos_olvidados/staleness.py contra lastfm_stats.db real.
    ro_dir = tmp_path / "lastfm_ro"
    ro_dir.mkdir()
    path = ro_dir / "lastfm_stats.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE artists (artist_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE albums (artist_id INTEGER, album_id INTEGER, name TEXT);
        CREATE TABLE scrobbles (artist_id INTEGER, album_id INTEGER, ts INTEGER);
        """
    )
    # Boards of Canada, "Music Has the Right to Children": primer scrobble
    # en 2010-02-01, luego más escuchas repartidas en el tiempo.
    conn.execute("INSERT INTO artists (artist_id, name) VALUES (1, 'Boards of Canada')")
    conn.execute("INSERT INTO albums (artist_id, album_id, name) VALUES (1, 10, 'Music Has the Right to Children')")
    for ts in (1265000000, 1270000000, 1300000000):  # 2010-02, 2010-03, 2011-03
        conn.execute("INSERT INTO scrobbles (artist_id, album_id, ts) VALUES (1, 10, ?)", (ts,))
    conn.commit()
    conn.close()
    os.chmod(path, 0o444)
    os.chmod(ro_dir, 0o555)
    yield str(path)
    os.chmod(ro_dir, 0o755)


@pytest.fixture
def trasvase_path(tmp_path):
    # music.db de services/trasvase: collection=2 = backup de qBittorrent
    # (ver staleness._load_qbittorrent_index).
    ro_dir = tmp_path / "trasvase_ro"
    ro_dir.mkdir()
    path = ro_dir / "music.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER, title TEXT, collection INTEGER);
        """
    )
    conn.execute("INSERT INTO artists (id, name) VALUES (1, 'Boards of Canada')")
    conn.execute("INSERT INTO albums (artist_id, title, collection) VALUES (1, 'Music Has the Right to Children', 2)")
    conn.commit()
    conn.close()
    os.chmod(path, 0o444)
    os.chmod(ro_dir, 0o555)
    yield str(path)
    os.chmod(ro_dir, 0o755)


@pytest.fixture
def patched_db(monkeypatch, musica_local_path, own_db_path, lastfm_path, trasvase_path):
    import staleness
    monkeypatch.setattr(db_module, "MUSICA_LOCAL_DB", musica_local_path)
    monkeypatch.setattr(db_module, "DB_PATH", own_db_path)
    monkeypatch.setattr(db_module, "LASTFM_DB", lastfm_path)
    monkeypatch.setattr(db_module, "TRASVASE_DB", trasvase_path)
    # Caché de proceso: cada test parte de cero con sus propias BDs
    monkeypatch.setattr(staleness, "_CACHE",
                        {"albums": None, "artists": None, "album_index": None, "computed_at": None})
    return db_module
