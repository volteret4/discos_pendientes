import os
import sqlite3

from db import get_lastfm_conn, get_musica_local_conn
from listens import artist_timeline, compute_album_first_listens, compute_artist_first_listens


def test_compute_album_first_listens(patched_db):
    musica_conn = get_musica_local_conn()
    lastfm_conn = get_lastfm_conn()
    rows = compute_album_first_listens(musica_conn, lastfm_conn)
    musica_conn.close()
    lastfm_conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["artist_norm"] == "boards of canada"
    assert row["album_norm"] == "music has the right to children"
    assert row["first_ts"] == 1265000000  # el más antiguo de los 3 scrobbles insertados


def test_artist_timeline(patched_db):
    conn = get_lastfm_conn()
    data = artist_timeline(conn, "Boards of Canada")
    conn.close()

    assert data is not None
    assert data["total_plays"] == 3
    assert data["first_listened"].startswith("2010-02")
    # Los 3 scrobbles de prueba caen en meses distintos: 2010-02, 2010-03, 2011-03.
    assert [p["month"] for p in data["timeline"]] == ["2010-02", "2010-03", "2011-03"]
    assert all(p["plays"] == 1 for p in data["timeline"])


def test_artist_timeline_unknown_artist(patched_db):
    conn = get_lastfm_conn()
    data = artist_timeline(conn, "Nadie Conocido")
    conn.close()
    assert data is None


def test_compute_artist_first_listens_covers_albums_with_no_exact_tag_match(patched_db):
    # Reproduce el bug real reportado con Sigur Rós: un segundo álbum del
    # mismo artista que no tiene NINGÚN scrobble con su tag exacto en
    # lastfm_stats.db (p.ej. una reedición con nombre distinto), pero el
    # artista sí tiene escuchas de sobra (las 3 ya insertadas en el
    # fixture lastfm_path, todas bajo "Music Has the Right to Children").
    # musica_local_path se deja de solo lectura a nivel de fichero Y de
    # directorio (ver tests/conftest.py) para reproducir el bind-mount
    # :ro real -- hay que reabrir en modo escritura un momento para
    # insertar este segundo álbum de prueba.
    path = patched_db.MUSICA_LOCAL_DB
    ro_dir = os.path.dirname(path)
    os.chmod(ro_dir, 0o755)
    os.chmod(path, 0o644)
    setup_conn = sqlite3.connect(path)
    setup_conn.execute("INSERT INTO albums (id, artist_id, name, year) VALUES (2, 1, 'Geogaddi (Reissue)', '2002')")
    setup_conn.commit()
    setup_conn.close()
    os.chmod(path, 0o444)
    os.chmod(ro_dir, 0o555)

    musica_conn = get_musica_local_conn()
    lastfm_conn = get_lastfm_conn()
    album_rows = {r["album_norm"]: r for r in compute_album_first_listens(musica_conn, lastfm_conn)}
    artist_rows = {r["artist_norm"]: r for r in compute_artist_first_listens(musica_conn, lastfm_conn)}
    musica_conn.close()
    lastfm_conn.close()

    # El álbum sin tag exacto: ningún first_ts a nivel de álbum...
    assert album_rows["geogaddi (reissue)"]["first_ts"] is None
    # ...pero el fallback a nivel de artista sí tiene dato.
    assert artist_rows["boards of canada"]["first_ts"] == 1265000000
