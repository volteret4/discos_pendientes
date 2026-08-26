import enrich


# ── Regresión: quirks reales del JSON de Last.fm, encontrados en
# producción al lanzar el backfill completo contra los 371 pendientes
# reales (ver services/discos_pendientes/CLAUDE.md) ────────────────────

def test_as_dict_handles_lastfm_empty_string_quirk():
    # Last.fm devuelve "bio"/"tags"/"wiki": "" (string) en vez de {} para
    # artistas/álbumes sin esos datos -- .get(x, {}) no protege porque la
    # clave SÍ existe.
    assert enrich._as_dict("") == {}
    assert enrich._as_dict({"summary": "x"}) == {"summary": "x"}
    assert enrich._as_dict(None) == {}


def test_as_list_handles_lastfm_single_item_collapse():
    # El XML->JSON de Last.fm colapsa listas de un elemento en un dict
    # suelto: "tag": {...} con un tag, "tag": [{...}, {...}] con varios.
    # Sin normalizar, [:6] sobre un dict revienta (KeyError: slice(...)).
    assert enrich._as_list({"name": "idm"}) == [{"name": "idm"}]
    assert enrich._as_list([{"name": "a"}, {"name": "b"}]) == [{"name": "a"}, {"name": "b"}]
    assert enrich._as_list(None) == []
    assert enrich._as_list("") == []


def test_lfm_image_handles_empty_string_instead_of_list():
    assert enrich._lfm_image("") == ""
    assert enrich._lfm_image([]) == ""


def test_enrich_album_survives_single_tag_and_empty_bio(patched_db, monkeypatch):
    # Reproduce exactamente la forma real que rompía enrich_album() antes
    # del fix: "tags" como dict suelto (un tag) y "bio"/"wiki" como "".
    monkeypatch.setattr(enrich, "lfm_get", lambda method, params: (
        {"artist": {"name": "Macroblank", "bio": "", "tags": {"tag": {"name": "downtempo"}}, "image": "", "url": "u"}}
        if method == "artist.getInfo" else
        {"album": {"name": "CLOWNS", "wiki": "", "tags": {"tag": {"name": "chillwave"}}, "image": "", "url": "u2"}}
    ))
    monkeypatch.setattr(enrich, "mb_search_release_group", lambda a, b: {})
    monkeypatch.setattr(enrich, "mb_search_artist", lambda a: {})
    monkeypatch.setattr(enrich, "wikipedia_summary", lambda a, lang="en": {})
    monkeypatch.setattr(enrich.time, "sleep", lambda *_: None)

    result = enrich.enrich_album("Macroblank", "CLOWNS")
    assert result["source"] == "live"
    assert result["artist"]["tags"] == ["downtempo"]
    assert result["album"]["tags"] == ["chillwave"]


def test_local_lookup_exact_match(patched_db):
    result = enrich.local_lookup("Boards of Canada", "Music Has the Right to Children")
    assert result is not None
    assert result["album"]["name"] == "Music Has the Right to Children"
    assert result["album"]["label"] == "Warp Records"
    assert result["artist"]["name"] == "Boards of Canada"
    assert result["label"]["name"] == "Warp Records"


def test_local_lookup_accent_insensitive(patched_db):
    # "Bjork"/"boards of canada" sin acentos/mayúsculas -- el camino lento
    # (normalizado) debe encontrarlo igual.
    result = enrich.local_lookup("boards OF canada", "music has the right to children")
    assert result is not None
    assert result["album"]["name"] == "Music Has the Right to Children"


def test_local_lookup_no_match(patched_db):
    assert enrich.local_lookup("Artista Desconocido", "Album Desconocido") is None


def test_enrich_album_prefers_local(patched_db, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("no debería llamar a APIs externas si está en local_db")

    monkeypatch.setattr(enrich, "lfm_get", _boom)
    monkeypatch.setattr(enrich, "mb_search_release_group", _boom)
    monkeypatch.setattr(enrich, "mb_search_artist", _boom)
    monkeypatch.setattr(enrich, "wikipedia_summary", _boom)

    result = enrich.enrich_album("Boards of Canada", "Music Has the Right to Children")
    assert result["source"] == "local_db"
    assert result["album"]["name"] == "Music Has the Right to Children"


def test_enrich_album_falls_back_to_live(patched_db, monkeypatch):
    monkeypatch.setattr(enrich, "lfm_get", lambda method, params: (
        {"artist": {"name": "Nueva Banda", "bio": {"summary": "bio"}, "stats": {"listeners": "100"}, "image": [], "url": "u", "tags": {}}}
        if method == "artist.getInfo" else
        {"album": {"name": "Nuevo Album", "wiki": {"summary": "wiki"}, "image": [], "url": "u2", "tags": {}}}
    ))
    monkeypatch.setattr(enrich, "mb_search_release_group", lambda a, b: {})
    monkeypatch.setattr(enrich, "mb_search_artist", lambda a: {})
    monkeypatch.setattr(enrich, "wikipedia_summary", lambda a, lang="en": {})
    monkeypatch.setattr(enrich.time, "sleep", lambda *_: None)

    result = enrich.enrich_album("Artista Nuevo", "Album Nuevo")
    assert result["source"] == "live"
    assert result["artist"]["name"] == "Nueva Banda"
    assert result["album"]["name"] == "Nuevo Album"


def test_get_or_fetch_enrichment_caches_live_results(patched_db, monkeypatch):
    calls = {"n": 0}

    def fake_enrich_album(artist, album):
        calls["n"] += 1
        return {
            "source": "live",
            "artist": {"name": artist},
            "album": {"name": album, "genre": "Electronic", "label": "Test Label", "image": "http://img"},
            "wikipedia": {"extract": "algo"},
            "musicbrainz": {},
            "label": {},
        }

    monkeypatch.setattr(enrich, "enrich_album", fake_enrich_album)

    conn = patched_db.get_conn()
    r1 = enrich.get_or_fetch_enrichment(conn, "Artista Nuevo", "Album Nuevo")
    r2 = enrich.get_or_fetch_enrichment(conn, "Artista Nuevo", "Album Nuevo")
    conn.close()

    assert calls["n"] == 1  # la segunda llamada se sirve de la cache, no vuelve a llamar
    assert r1["album"]["name"] == "Album Nuevo"
    assert r2["source"] == "live"
    assert r2["album"]["name"] == "Album Nuevo"


def test_get_or_fetch_enrichment_does_not_cache_local(patched_db, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("no debería tocar la cache/APIs externas para un álbum local")

    monkeypatch.setattr(enrich, "enrich_album", _boom)

    conn = patched_db.get_conn()
    result = enrich.get_or_fetch_enrichment(conn, "Boards of Canada", "Music Has the Right to Children")
    n_cached = conn.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0]
    conn.close()

    assert result["source"] == "local_db"
    assert n_cached == 0
