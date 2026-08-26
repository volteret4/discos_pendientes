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
def patched_db(monkeypatch, musica_local_path, own_db_path):
    monkeypatch.setattr(db_module, "MUSICA_LOCAL_DB", musica_local_path)
    monkeypatch.setattr(db_module, "DB_PATH", own_db_path)
    return db_module
