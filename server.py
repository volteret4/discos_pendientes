#!/usr/bin/env python3
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string

import navidrome_client
import staleness
import sync
from db import get_conn, get_lastfm_conn, get_musica_local_conn
from enrich import get_or_fetch_enrichment
from listens import artist_timeline
from normalize import normalize

app = Flask(__name__)

PER_PAGE = 50


def _int_or(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ── Pendientes ───────────────────────────────────────────────────────────

TASKS_SORT_COLUMNS = {
    "artist": "t.artist COLLATE NOCASE",
    "album": "t.album COLLATE NOCASE",
    "dtstart": "t.dtstart",
    "downloaded": "COALESCE(ps.downloaded, 0)",
}


@app.route("/api/tasks")
def api_tasks():
    page = max(1, _int_or(request.args.get("page"), 1))
    per_page = min(200, max(1, _int_or(request.args.get("per_page"), PER_PAGE)))
    status = request.args.get("status", "pending")
    downloaded = request.args.get("downloaded", "all")
    artist = request.args.get("artist", "").strip()

    where = []
    params = []
    if status == "pending":
        where.append("t.status = 'NEEDS-ACTION'")
    elif status == "completed":
        where.append("t.status = 'COMPLETED'")
    if downloaded == "yes":
        where.append("ps.downloaded = 1")
    elif downloaded == "no":
        where.append("(ps.downloaded IS NULL OR ps.downloaded = 0)")
    if artist:
        where.append("t.artist LIKE ?")
        params.append(f"%{artist}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sort_col = TASKS_SORT_COLUMNS.get(request.args.get("sort"), TASKS_SORT_COLUMNS["dtstart"])
    sort_dir = "ASC" if request.args.get("dir") == "asc" else "DESC"

    base = """
        FROM tasks t
        LEFT JOIN pending_status ps ON ps.uid = t.uid
    """
    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) {base} {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT t.uid, t.artist, t.album, t.status, t.dtstart,
               ps.downloaded, ps.airsonic_date, ps.qbittorrent_date, ps.checked_at
        {base}
        {where_sql}
        ORDER BY {sort_col} {sort_dir}, t.dtstart DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    return jsonify({"total": total, "page": page, "per_page": per_page, "items": items})


@app.route("/api/tasks/<uid>/enrichment")
def api_task_enrichment(uid):
    conn = get_conn()
    row = conn.execute("SELECT artist, album FROM tasks WHERE uid = ?", (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "no encontrado"}), 404
    try:
        info = get_or_fetch_enrichment(conn, row["artist"], row["album"])
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    conn.close()
    resp = jsonify(info)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


_sync_lock = threading.Lock()


def _run_sync_bg(fn):
    if not _sync_lock.acquire(blocking=False):
        return False
    def _run():
        try:
            fn()
        finally:
            _sync_lock.release()
    threading.Thread(target=_run, daemon=True).start()
    return True


@app.route("/api/sync/tasks", methods=["POST"])
def api_sync_tasks():
    started = _run_sync_bg(sync.sync_tasks)
    return jsonify({"started": started})


@app.route("/api/sync/downloaded_status", methods=["POST"])
def api_sync_downloaded_status():
    started = _run_sync_bg(lambda: sync.sync_downloaded_status(stale_only=True))
    return jsonify({"started": started})


# ── Catálogo (búsqueda por género/sello/fecha/artista) ─────────────────────

CATALOG_SORT_COLUMNS = {
    "artist": "ar.name COLLATE NOCASE",
    "album": "al.name COLLATE NOCASE",
    "year": "al.year",
    "label": "al.label COLLATE NOCASE",
    "genre": "al.genre COLLATE NOCASE",
}


def _build_catalog_where(args):
    where = []
    params = []
    if args.get("genre"):
        where.append("al.genre LIKE ?")
        params.append(f"%{args['genre']}%")
    if args.get("label"):
        where.append("al.label LIKE ?")
        params.append(f"%{args['label']}%")
    if args.get("artist"):
        where.append("ar.name LIKE ?")
        params.append(f"%{args['artist']}%")
    if args.get("album"):
        where.append("al.name LIKE ?")
        params.append(f"%{args['album']}%")
    if args.get("year_from"):
        where.append("substr(al.year,1,4) >= ?")
        params.append(args["year_from"][:4])
    if args.get("year_to"):
        where.append("substr(al.year,1,4) <= ?")
        params.append(args["year_to"][:4])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


CATALOG_BASE = """
    FROM albums al
    JOIN artists ar ON ar.id = al.artist_id
"""


def _fetch_first_listens(pairs):
    """{(artist_norm, album_norm): iso_first_listened} para el conjunto de
    pares de la página actual -- la caché vive en discos_pendientes.db
    (album_first_listen), una conexión sqlite distinta de
    musica_local.sqlite, así que no se puede hacer en un único JOIN SQL."""
    pairs = list({p for p in pairs})
    if not pairs:
        return {}
    conn = get_conn()
    where = " OR ".join(["(artist_norm = ? AND album_norm = ?)"] * len(pairs))
    params = [v for pair in pairs for v in pair]
    rows = conn.execute(
        f"SELECT artist_norm, album_norm, first_ts FROM album_first_listen WHERE {where}", params
    ).fetchall()
    conn.close()
    return {
        (r["artist_norm"], r["album_norm"]): datetime.fromtimestamp(r["first_ts"], tz=timezone.utc).isoformat()
        for r in rows if r["first_ts"] is not None
    }


def _fetch_artist_first_listens(artist_norms):
    """{artist_norm: iso_first_listened} -- fallback cuando
    album_first_listen no tiene ningún scrobble con el tag de álbum
    exacto (~42% de los álbumes en producción: reediciones, "..." vs
    "…", recopilatorios... ver listens.compute_artist_first_listens).
    Sin esto, esos álbumes aparecían como "nunca escuchados" aunque la
    banda sí se hubiera escuchado -- lo que reportó el usuario con
    Sigur Rós."""
    artist_norms = list(set(artist_norms))
    if not artist_norms:
        return {}
    conn = get_conn()
    placeholders = ",".join(["?"] * len(artist_norms))
    rows = conn.execute(
        f"SELECT artist_norm, first_ts FROM artist_first_listen WHERE artist_norm IN ({placeholders})",
        artist_norms,
    ).fetchall()
    conn.close()
    return {
        r["artist_norm"]: datetime.fromtimestamp(r["first_ts"], tz=timezone.utc).isoformat()
        for r in rows if r["first_ts"] is not None
    }


# Columnas que viven fuera de musica_local.sqlite (Last.fm / trasvase,
# ver staleness.py) -- filtrar u ordenar por ellas obliga a hacerlo en
# Python sobre todas las filas que cumplen el WHERE, no en SQL.
STALENESS_SORT_KEYS = {"days_since", "in_qbittorrent"}
IN_MEMORY_SORT_KEYS = STALENESS_SORT_KEYS | {"first_listened"}


def _annotate_all_first_listens(items):
    """Igual que _fetch_first_listens/_fetch_artist_first_listens pero
    cargando las dos tablas enteras de una vez (para ordenar todo el
    catálogo por primera escucha; por página bastan las otras)."""
    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None
    conn = get_conn()
    albums = {(r["artist_norm"], r["album_norm"]): iso(r["first_ts"])
              for r in conn.execute("SELECT artist_norm, album_norm, first_ts FROM album_first_listen")}
    artists = {r["artist_norm"]: iso(r["first_ts"])
               for r in conn.execute("SELECT artist_norm, first_ts FROM artist_first_listen")}
    conn.close()
    for it in items:
        an = normalize(it["artist"])
        it["first_listened"] = albums.get((an, normalize(it["album"])))
        it["artist_first_listened"] = artists.get(an)
    return items


def _sort_first_listened(rows, desc):
    """Por la primera escucha del álbum o, si no hay, la de la banda (lo
    mismo que muestra la columna con "~"); sin ninguna, siempre al final."""
    def eff(r):
        return r.get("first_listened") or r.get("artist_first_listened")
    known = sorted((r for r in rows if eff(r)), key=eff, reverse=desc)
    return known + [r for r in rows if not eff(r)]


def _annotate_staleness(items):
    index = staleness.album_index()
    for it in items:
        st = index.get((normalize(it["artist"]), normalize(it["album"])))
        it["days_since"] = st["days_since"] if st else None
        it["last_listened"] = st["last_listened"] if st else None
        it["in_qbittorrent"] = bool(st and st["in_qbittorrent"])
    return items


def _staleness_sort_key(sort_key):
    """Mismo criterio que discos_olvidados: "nunca escuchado" (None) cuenta
    como infinito, así sale el primero ordenando de más a menos días."""
    def key(r):
        v = r.get(sort_key)
        if v is None:
            return float("inf") if sort_key == "days_since" else ""
        return v.lower() if isinstance(v, str) else v
    return key


def _filter_staleness(rows, args):
    never = args.get("never_listened")
    qbittorrent = args.get("qbittorrent")
    if never == "yes":
        rows = [r for r in rows if r["days_since"] is None]
    elif never == "no":
        rows = [r for r in rows if r["days_since"] is not None]
    if qbittorrent == "yes":
        rows = [r for r in rows if r.get("in_qbittorrent")]
    elif qbittorrent == "no":
        rows = [r for r in rows if not r.get("in_qbittorrent")]
    return rows


@app.route("/api/catalog/search")
def api_catalog_search():
    page = max(1, _int_or(request.args.get("page"), 1))
    per_page = min(200, max(1, _int_or(request.args.get("per_page"), PER_PAGE)))
    where_sql, params = _build_catalog_where(request.args)
    sort_key = request.args.get("sort")
    sort_col = CATALOG_SORT_COLUMNS.get(sort_key, CATALOG_SORT_COLUMNS["artist"])
    sort_dir = "DESC" if request.args.get("dir") == "desc" else "ASC"
    in_memory = (sort_key in IN_MEMORY_SORT_KEYS
                 or request.args.get("never_listened") in ("yes", "no")
                 or request.args.get("qbittorrent") in ("yes", "no"))

    conn = get_musica_local_conn()
    select = f"SELECT al.id, al.name AS album, al.year, al.label, al.genre, ar.name AS artist {CATALOG_BASE} {where_sql}"
    if in_memory:
        all_rows = _filter_staleness(_annotate_staleness([dict(r) for r in conn.execute(select, params)]), request.args)
        if sort_key == "first_listened":
            all_rows = _sort_first_listened(_annotate_all_first_listens(all_rows), sort_dir == "DESC")
        elif sort_key in STALENESS_SORT_KEYS:
            all_rows.sort(key=_staleness_sort_key(sort_key), reverse=(sort_dir == "DESC"))
        else:
            all_rows.sort(key=_staleness_sort_key(sort_key if sort_key in CATALOG_SORT_COLUMNS else "artist"),
                          reverse=(sort_dir == "DESC"))
        total = len(all_rows)
        items = all_rows[(page - 1) * per_page: page * per_page]
    else:
        total = conn.execute(f"SELECT COUNT(*) {CATALOG_BASE} {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"{select} ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
        items = _annotate_staleness([dict(r) for r in rows])
    conn.close()

    first_listens = _fetch_first_listens((normalize(it["artist"]), normalize(it["album"])) for it in items)
    artist_first_listens = _fetch_artist_first_listens(normalize(it["artist"]) for it in items)
    for it in items:
        it["first_listened"] = first_listens.get((normalize(it["artist"]), normalize(it["album"])))
        it["artist_first_listened"] = artist_first_listens.get(normalize(it["artist"]))

    return jsonify({"total": total, "page": page, "per_page": per_page, "items": items})


@app.route("/api/catalog/artists")
def api_catalog_artists():
    """Pestaña Artistas (antes en discos_olvidados): última escucha por
    artista y cuántos de sus álbumes están en el backup de qBittorrent."""
    rows = staleness.get_report("artists")
    artist = (request.args.get("artist") or "").strip().lower()
    if artist:
        rows = [r for r in rows if artist in (r.get("artist") or "").lower()]
    rows = _filter_staleness(rows, {"never_listened": request.args.get("never_listened")})

    sort_key = request.args.get("sort") or "days_since"
    if sort_key not in {"artist", "album_count", "backed_up_count", "days_since"}:
        sort_key = "days_since"
    sort_dir = request.args.get("dir") or ("desc" if sort_key == "days_since" else "asc")
    rows = sorted(rows, key=_staleness_sort_key(sort_key), reverse=(sort_dir == "desc"))

    page = max(1, _int_or(request.args.get("page"), 1))
    per_page = min(200, max(1, _int_or(request.args.get("per_page"), PER_PAGE)))
    return jsonify({"total": len(rows), "page": page, "per_page": per_page,
                    "items": rows[(page - 1) * per_page: page * per_page]})


@app.route("/api/catalog/staleness/refresh", methods=["POST"])
def api_catalog_staleness_refresh():
    staleness.refresh_all()
    return jsonify({"ok": True, "computed_at": staleness.computed_at()})


@app.route("/api/catalog/staleness/status")
def api_catalog_staleness_status():
    return jsonify({"computed_at": staleness.computed_at()})


@app.route("/api/catalog/artist_listens")
def api_catalog_artist_listens():
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "falta artist"}), 400
    conn = get_lastfm_conn()
    try:
        data = artist_timeline(conn, artist)
    finally:
        conn.close()
    if data is None:
        return jsonify({"artist": artist, "first_listened": None, "total_plays": 0, "timeline": []})
    return jsonify({"artist": artist, **data})


@app.route("/api/catalog/filters/meta")
def api_catalog_filters_meta():
    conn = get_musica_local_conn()
    genres = {
        r["genre"] for r in conn.execute(
            "SELECT DISTINCT genre FROM albums WHERE genre IS NOT NULL AND genre != ''"
        )
    }
    labels = {
        r["label"] for r in conn.execute(
            "SELECT DISTINCT label FROM albums WHERE label IS NOT NULL AND label != ''"
        )
    }
    conn.close()
    return jsonify({"genres": sorted(genres), "labels": sorted(labels)})


@app.route("/api/catalog/album/<int:album_id>")
def api_catalog_album(album_id):
    conn = get_musica_local_conn()
    row = conn.execute(
        """
        SELECT al.name, al.year, al.genre, al.label, al.wikipedia_content, al.wikipedia_url,
               al.lastfm_url, al.discogs_url, al.folder_path,
               ar.name AS artist_name, ar.bio AS artist_bio,
               ar.wikipedia_content AS artist_wikipedia_content,
               ar.wikipedia_url AS artist_wikipedia_url, ar.lastfm_url AS artist_lastfm_url
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        WHERE al.id = ?
        """,
        (album_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "no encontrado"}), 404

    label_info = {}
    if row["label"]:
        lrow = conn.execute(
            "SELECT name, description, wikipedia_url, discogs_url FROM labels WHERE name = ? COLLATE NOCASE LIMIT 1",
            (row["label"],),
        ).fetchone()
        if lrow:
            label_info = {
                "name": lrow["name"], "description": lrow["description"] or "",
                "wikipedia_url": lrow["wikipedia_url"] or "", "discogs_url": lrow["discogs_url"] or "",
            }
    conn.close()

    return jsonify({
        "source": "local_db",
        "artist": {
            "name": row["artist_name"], "bio": row["artist_bio"] or "",
            "wikipedia_content": row["artist_wikipedia_content"] or "",
            "wikipedia_url": row["artist_wikipedia_url"] or "", "url": row["artist_lastfm_url"] or "",
        },
        "album": {
            "name": row["name"], "year": row["year"], "genre": row["genre"], "label": row["label"],
            "wiki": row["wikipedia_content"] or "", "url": row["lastfm_url"] or "",
            "wikipedia_url": row["wikipedia_url"] or "", "discogs_url": row["discogs_url"] or "",
            "owned": True, "folder_path": row["folder_path"],
        },
        "label": label_info,
        "wikipedia": {}, "musicbrainz": {},
    })


# ── Biblioteca de Navidrome: Sin escuchas / Escuchados + playlists ───────

LIBRARY_SORT_COLUMNS = {
    "artist": "artist COLLATE NOCASE",
    "album": "album COLLATE NOCASE",
    "year": "year",
    "genre": "genre COLLATE NOCASE",
    "created": "created",
    "tracks_heard": "tracks_heard",
    "first_ts": "first_ts",
    "last_ts": "last_ts",
    "plays": "plays",
}
MAX_ALBUMS_PER_ADD = 200

_library_sync_error = None


def _sync_library_tracked():
    global _library_sync_error
    _library_sync_error = None
    try:
        sync.sync_library()
    except Exception as e:
        # Mensaje genérico salvo NavidromeError, que ya es seguro de mostrar.
        _library_sync_error = str(e) if isinstance(e, navidrome_client.NavidromeError) else "Error inesperado al sincronizar (ver logs)"
        app.logger.exception("sync_library")


@app.route("/api/library")
def api_library():
    page = max(1, _int_or(request.args.get("page"), 1))
    per_page = min(200, max(1, _int_or(request.args.get("per_page"), PER_PAGE)))
    # Cualquier valor distinto de "yes" es la vista de sin escuchar.
    where = ["match_kind IS NOT NULL" if request.args.get("listened") == "yes" else "match_kind IS NULL"]
    params = []
    for arg, column in (("artist", "artist"), ("album", "album"), ("genre", "genre")):
        value = request.args.get(arg, "").strip()
        if value:
            where.append(f"{column} LIKE ?")
            params.append(f"%{value}%")
    for arg, op in (("year_from", ">="), ("year_to", "<=")):
        year = _int_or(request.args.get(arg), None)
        if year is not None:
            where.append(f"year {op} ?")
            params.append(year)
    where_sql = "WHERE " + " AND ".join(where)
    sort_col = LIBRARY_SORT_COLUMNS.get(request.args.get("sort"), LIBRARY_SORT_COLUMNS["artist"])
    sort_dir = "DESC" if request.args.get("dir") == "desc" else "ASC"

    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) FROM library_albums {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT id, artist, album, year, genre, created, song_count, tracks_heard, match_kind, first_ts, last_ts, plays
        FROM library_albums {where_sql}
        ORDER BY {sort_col} {sort_dir}, artist COLLATE NOCASE, album COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()
    return jsonify({"total": total, "page": page, "per_page": per_page, "items": [dict(r) for r in rows]})


@app.route("/api/library/status")
def api_library_status():
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(synced_at) AS synced_at, COUNT(*) AS total, COUNT(match_kind) AS heard FROM library_albums"
    ).fetchone()
    conn.close()
    return jsonify({
        "configured": navidrome_client.is_configured(), "user": navidrome_client.username(),
        "synced_at": row["synced_at"], "total": row["total"], "heard": row["heard"],
        "running": _sync_lock.locked(), "error": _library_sync_error,
    })


@app.route("/api/library/sync", methods=["POST"])
def api_library_sync():
    if not navidrome_client.is_configured():
        return jsonify({"started": False, "error": "Faltan NAVIDROME_USER / NAVIDROME_PASS en el .env"}), 503
    return jsonify({"started": _run_sync_bg(_sync_library_tracked)})


@app.route("/api/library/<album_id>/enrichment")
def api_library_enrichment(album_id):
    conn = get_conn()
    row = conn.execute("SELECT artist, album FROM library_albums WHERE id = ?", (album_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "no encontrado"}), 404
    try:
        info = get_or_fetch_enrichment(conn, row["artist"], row["album"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
    return jsonify(info)


@app.route("/api/navidrome/playlists")
def api_navidrome_playlists():
    try:
        return jsonify({"user": navidrome_client.username(), "items": navidrome_client.editable_playlists()})
    except navidrome_client.NavidromeError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/navidrome/playlist", methods=["POST"])
def api_navidrome_add_to_playlist():
    """Añade álbumes enteros, en orden, a una playlist existente
    (playlist_id) o nueva (name). No repite canciones que ya estén, así que
    reintentar es seguro."""
    data = request.get_json(silent=True) or {}
    album_ids = data.get("album_ids")
    playlist_id = (data.get("playlist_id") or "").strip()
    name = (data.get("name") or "").strip()
    if not isinstance(album_ids, list) or not album_ids or not all(isinstance(a, str) for a in album_ids):
        return jsonify({"error": "No hay álbumes seleccionados"}), 400
    if len(album_ids) > MAX_ALBUMS_PER_ADD:
        return jsonify({"error": f"Máximo {MAX_ALBUMS_PER_ADD} álbumes de una vez"}), 400
    if bool(playlist_id) == bool(name):
        return jsonify({"error": "Elige una playlist o escribe el nombre de una nueva"}), 400

    try:
        existing = navidrome_client.playlist_info(playlist_id) if playlist_id else {"name": name, "song_ids": []}
        seen = set(existing["song_ids"])
        to_add, failed = [], []
        for album_id in dict.fromkeys(album_ids):
            try:
                song_ids = navidrome_client.album_song_ids(album_id)
            except navidrome_client.NavidromeError:
                failed.append(album_id)  # ej. borrado de Navidrome desde el último sync
                continue
            for song_id in song_ids:
                if song_id not in seen:
                    seen.add(song_id)
                    to_add.append(song_id)

        if len(failed) == len(set(album_ids)):
            return jsonify({"error": "No se pudo leer ninguno de los álbumes en Navidrome"}), 502
        if playlist_id:
            if to_add:
                navidrome_client.add_to_playlist(playlist_id, to_add)
        else:
            playlist_id = navidrome_client.create_playlist(name, to_add)
    except navidrome_client.NavidromeError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({
        "playlist_id": playlist_id, "name": existing["name"], "added": len(to_add),
        "failed_albums": failed,
    })


HTML_PAGE = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Discos Pendientes</title>
<!-- Iconos Airsonic / qBittorrent / escuchado / Mix (servicio ya-lo-tengo) -->
<script>(function(){var s=document.createElement('script');s.src=location.protocol+'//'+location.hostname+':8780/owned-badges.js';document.head.appendChild(s);})();</script>
<style>
:root {
    --bg: #0a0e27; --surface: #16213e; --surface-2: #1f2d4a; --border: #2d3e5f;
    --text: #b0b8c9; --text-muted: #7a8694; --accent: #35bf88; --accent-2: #8e44ad;
    --warning: #f39c12; --danger: #e74c3c;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--bg); color: var(--text); padding: 20px; }
h1 { color: #fff; margin-bottom: 16px; font-size: 20px; }
.tabs { display: flex; gap: 6px; margin-bottom: 16px; }
.tab-btn { background: var(--surface); color: var(--text-muted); border: 1px solid var(--border); padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; }
.tab-btn.active { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.filters { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; background: var(--surface); padding: 16px; border-radius: 10px; border: 1px solid var(--border); margin-bottom: 16px; }
.filters label { font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px; }
.filters input, .filters select { width: 100%; padding: 6px 8px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 13px; }
.filters input:focus, .filters select:focus { outline: none; border-color: var(--accent); }
.actions { grid-column: 1 / -1; display: flex; gap: 8px; }
button { background: var(--accent); color: var(--bg); border: none; padding: 8px 16px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 13px; }
button.secondary { background: var(--surface-2); color: var(--text); }
button:disabled { opacity: 0.5; cursor: default; }
#summary, #summary-catalog { color: var(--text-muted); font-size: 13px; margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
th { color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 11px; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--text); }
th .sort-arrow { color: var(--accent); margin-left: 3px; }
tr:hover td { background: var(--surface-2); }
.clickable { cursor: pointer; }
.clickable:hover { color: #fff; text-decoration: underline; }
.row-link { cursor: pointer; }
.mark { display: inline-block; width: 18px; text-align: center; }
.mark.yes { color: var(--accent); }
.mark.no { color: var(--text-muted); }
.pagination { display: flex; gap: 6px; justify-content: center; margin-top: 14px; }
.pagination button { background: var(--surface-2); color: var(--text); padding: 6px 10px; }
.pagination button.active { background: var(--accent); color: var(--bg); }
.pagination button:disabled { opacity: 0.4; cursor: default; }

/* ── Panel lateral ── */
#panel {
    position: fixed; top: 0; right: 0; width: 380px; max-width: 92vw; height: 100vh;
    background: var(--surface); border-left: 1px solid var(--border); box-shadow: -8px 0 24px rgba(0,0,0,0.4);
    overflow-y: auto; transform: translateX(100%); transition: transform 0.25s ease; z-index: 200; padding: 16px;
}
#panel.open { transform: translateX(0); }
#panel-close { position: absolute; top: 10px; right: 12px; background: var(--surface-2); color: var(--text); border: none; border-radius: 50%; width: 26px; height: 26px; font-size: 14px; padding: 0; cursor: pointer; }
.panel-section { margin-top: 16px; }
.panel-section h4 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; color: var(--text-muted); margin-bottom: 6px; }
.panel-title { font-size: 15px; font-weight: bold; color: #fff; margin-top: 8px; padding-right: 26px; }
.panel-sub { font-size: 12px; color: var(--text-muted); }
.panel-img { width: 100%; max-height: 200px; object-fit: cover; border-radius: 8px; margin-bottom: 8px; }
.panel-text { font-size: 12px; color: var(--text); line-height: 1.5; max-height: 200px; overflow-y: auto; }
.panel-link { color: var(--accent); font-size: 12px; text-decoration: none; display: inline-block; margin-top: 6px; }
.panel-link:hover { text-decoration: underline; }
.panel-empty { color: var(--text-muted); font-size: 12px; }
.owned-badge { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 4px; background: var(--surface-2); color: var(--accent); margin-top: 6px; }
.chart-stats { display: flex; gap: 16px; margin-bottom: 10px; }
.chart-stat { font-size: 11px; color: var(--text-muted); }
.chart-stat strong { display: block; font-size: 15px; color: #fff; font-weight: 600; }
.chart-tooltip-hint { font-size: 11px; color: var(--text-muted); margin-top: 4px; }
.icon-yes { color: var(--accent); font-weight: 700; }
.icon-no { color: var(--danger); font-weight: 700; }
.never { color: var(--danger); font-weight: 600; }
.stale-high { color: var(--warning); }
.staleness-status { color: var(--text-muted); font-size: 12px; align-self: center; }
.hint-approx { color: var(--text-muted); border-bottom: 1px dashed var(--text-muted); cursor: help; }
.library-summary { color: var(--text-muted); font-size: 13px; margin-bottom: 10px; }
.library-notice { color: var(--warning); font-size: 13px; margin-bottom: 10px; }
.check-cell { width: 34px; text-align: center; }
.check-cell input { cursor: pointer; width: 15px; height: 15px; accent-color: var(--accent); }
body.has-playlist-bar { padding-bottom: 90px; }
#playlist-bar { display: none; position: fixed; left: 0; right: 0; bottom: 0; z-index: 100; background: var(--surface); border-top: 1px solid var(--accent); padding: 12px 20px; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 13px; }
#playlist-bar.visible { display: flex; }
#playlist-bar select, #playlist-bar input { padding: 6px 8px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 13px; }
#pl_name { min-width: 260px; }
#pl_count { color: #fff; font-weight: 600; }
#pl_status { color: var(--text-muted); }
#pl_status.error { color: var(--danger); }
</style>
</head>
<body>
<h1>Discos Pendientes — REPEAT-ONE</h1>
<div class="tabs">
    <button class="tab-btn active" data-tab="pendientes" onclick="switchTab('pendientes')">Pendientes</button>
    <button class="tab-btn" data-tab="catalogo" onclick="switchTab('catalogo')">Catálogo</button>
    <button class="tab-btn" data-tab="artistas" onclick="switchTab('artistas')">Artistas</button>
    <button class="tab-btn" data-tab="sin-escuchas" onclick="switchTab('sin-escuchas')">Sin escuchas</button>
    <button class="tab-btn" data-tab="escuchados" onclick="switchTab('escuchados')">Escuchados</button>
</div>

<div id="tab-pendientes" class="tab-panel active">
    <div class="filters">
        <div><label>Estado</label>
            <select id="f_status"><option value="pending">Pendientes</option><option value="completed">Completados</option><option value="all">Todos</option></select>
        </div>
        <div><label>Descargado</label>
            <select id="f_downloaded"><option value="all">Todos</option><option value="yes">Sí</option><option value="no">No</option></select>
        </div>
        <div><label>Artista</label><input type="text" id="f_p_artist"></div>
        <div class="actions">
            <button onclick="searchTasks(1)">Buscar</button>
            <button class="secondary" onclick="clearTaskFilters()">Limpiar</button>
            <button class="secondary" onclick="triggerSync('tasks')">Actualizar calendario</button>
            <button class="secondary" onclick="triggerSync('downloaded_status')">Comprobar descargas</button>
        </div>
    </div>
    <div id="summary"></div>
    <table>
        <thead><tr>
            <th class="sortable" data-sort="artist">Artista<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="album">Álbum<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="dtstart">Fecha<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="downloaded">Descargado<span class="sort-arrow"></span></th>
        </tr></thead>
        <tbody id="tasks-results"></tbody>
    </table>
    <div class="pagination" id="tasks-pagination"></div>
</div>

<div id="tab-catalogo" class="tab-panel">
    <div class="filters">
        <div><label>Artista</label><input type="text" id="f_c_artist"></div>
        <div><label>Álbum</label><input type="text" id="f_c_album"></div>
        <div><label>Género</label><input type="text" id="f_genre" list="genres_list"></div>
        <div><label>Sello</label><input type="text" id="f_label" list="labels_list"></div>
        <div><label>Año desde</label><input type="number" id="f_year_from" placeholder="1990"></div>
        <div><label>Año hasta</label><input type="number" id="f_year_to" placeholder="2026"></div>
        <div><label>Escuchado</label>
            <select id="f_never"><option value="">Todos</option><option value="yes">Nunca</option><option value="no">Alguna vez</option></select>
        </div>
        <div><label>En qBittorrent</label>
            <select id="f_qbittorrent"><option value="">Todos</option><option value="yes">Sí</option><option value="no">No</option></select>
        </div>
        <datalist id="genres_list"></datalist>
        <datalist id="labels_list"></datalist>
        <div class="actions">
            <button onclick="searchCatalog(1)">Buscar</button>
            <button class="secondary" onclick="clearCatalogFilters()">Limpiar</button>
            <button class="secondary" onclick="refreshStaleness()" title="Vuelve a cruzar la colección con Last.fm y con el backup de qBittorrent (trasvase)">Recalcular escuchas y descargas</button>
            <span class="staleness-status"></span>
        </div>
    </div>
    <div id="summary-catalog"></div>
    <table>
        <thead><tr>
            <th class="sortable" data-sort="artist">Artista<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="album">Álbum<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="year">Año<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="label">Sello<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="genre">Género<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="first_listened" title="Primera escucha en Last.fm. Si no hay ningún scrobble con el tag de álbum exacto se muestra, en gris con '~', la primera escucha de la banda en general">Primera escucha<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="days_since" title="Tiempo desde la última escucha del álbum en Last.fm / ListenBrainz">Sin escuchar<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="in_qbittorrent" title="Está en el backup de qBittorrent (trasvase, colección 2)">qBittorrent<span class="sort-arrow"></span></th>
        </tr></thead>
        <tbody id="catalog-results"></tbody>
    </table>
    <div class="pagination" id="catalog-pagination"></div>
</div>

<div id="tab-artistas" class="tab-panel">
    <div class="filters">
        <div><label>Artista</label><input type="text" id="fr_artist"></div>
        <div><label>Escuchado</label>
            <select id="fr_never"><option value="">Todos</option><option value="yes">Nunca</option><option value="no">Alguna vez</option></select>
        </div>
        <div class="actions">
            <button onclick="searchArtists(1)">Buscar</button>
            <button class="secondary" onclick="clearArtistFilters()">Limpiar</button>
            <button class="secondary" onclick="refreshStaleness()">Recalcular escuchas y descargas</button>
            <span class="staleness-status"></span>
        </div>
    </div>
    <div id="summary-artists"></div>
    <table>
        <thead><tr>
            <th class="sortable" data-sort="artist">Artista<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="album_count">Álbumes<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="backed_up_count">En qBittorrent<span class="sort-arrow"></span></th>
            <th class="sortable" data-sort="days_since">Sin escuchar<span class="sort-arrow"></span></th>
        </tr></thead>
        <tbody id="artists-results"></tbody>
    </table>
    <div class="pagination" id="artists-pagination"></div>
</div>

<div id="tab-sin-escuchas" class="tab-panel"></div>
<div id="tab-escuchados" class="tab-panel"></div>

<div id="playlist-bar">
    <span id="pl_count"></span>
    <select id="pl_target" onchange="updatePlaylistBar()"></select>
    <input type="text" id="pl_name" placeholder="Nombre de la nueva playlist" maxlength="200">
    <button id="pl_add" onclick="addSelectionToPlaylist()">Añadir a playlist</button>
    <button class="secondary" onclick="clearSelection()">Vaciar selección</button>
    <span id="pl_status"></span>
</div>

<div id="panel">
    <button id="panel-close" onclick="closePanel()">×</button>
    <div id="panel-body"></div>
</div>

<script>
function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, '&quot;');
}

function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab));
    if (tab === 'catalogo' && !window._catalogLoaded) {
        window._catalogLoaded = true;
        loadCatalogMeta();
        loadStalenessStatus();
        searchCatalog(1);
    }
    if (tab === 'artistas' && !window._artistsLoaded) {
        window._artistsLoaded = true;
        loadStalenessStatus();
        searchArtists(1);
    }
    if (LIBRARY_VIEWS[tab] && !LIBRARY_VIEWS[tab].loaded) {
        LIBRARY_VIEWS[tab].loaded = true;
        LIBRARY_VIEWS[tab].search(1);
    }
    updatePlaylistBar();
}

// ── Pendientes ──────────────────────────────────────────────────────────

let tasksPage = 1, tasksTotalPages = 1, lastTasks = [];

function clearTaskFilters() {
    document.getElementById('f_status').value = 'pending';
    document.getElementById('f_downloaded').value = 'all';
    document.getElementById('f_p_artist').value = '';
    searchTasks(1);
}

let tasksSortKey = 'dtstart', tasksSortDir = 'desc';

document.querySelectorAll('#tab-pendientes th.sortable').forEach(th => {
    th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (tasksSortKey === key) tasksSortDir = tasksSortDir === 'asc' ? 'desc' : 'asc';
        else { tasksSortKey = key; tasksSortDir = (key === 'dtstart' || key === 'downloaded') ? 'desc' : 'asc'; }
        searchTasks(1);
    });
});

async function searchTasks(page) {
    tasksPage = page;
    const params = new URLSearchParams({
        status: document.getElementById('f_status').value,
        downloaded: document.getElementById('f_downloaded').value,
        page: page,
        sort: tasksSortKey,
        dir: tasksSortDir,
    });
    const artist = document.getElementById('f_p_artist').value.trim();
    if (artist) params.set('artist', artist);

    const r = await fetch('/api/tasks?' + params.toString());
    const data = await r.json();
    tasksTotalPages = Math.max(1, Math.ceil(data.total / data.per_page));
    lastTasks = data.items;

    document.getElementById('summary').textContent = `${data.total} tareas encontradas`;
    document.getElementById('tasks-results').innerHTML = data.items.map((it, i) => `
        <tr class="row-link" data-idx="${i}">
            <td>${escapeHtml(it.artist)}</td>
            <td data-owned-artist="${escapeAttr(it.artist)}" data-owned-album="${escapeAttr(it.album)}">${escapeHtml(it.album)}</td>
            <td>${it.dtstart ? new Date(it.dtstart).toLocaleDateString('es-ES') : ''}</td>
            <td><span class="mark ${it.downloaded ? 'yes' : 'no'}">${it.downloaded ? '✓' : '✗'}</span></td>
        </tr>
    `).join('');
    document.querySelectorAll('#tasks-results tr').forEach(tr => {
        tr.addEventListener('click', () => openTaskPanel(lastTasks[parseInt(tr.dataset.idx)]));
    });

    document.querySelectorAll('#tab-pendientes th.sortable').forEach(th => {
        th.querySelector('.sort-arrow').textContent = th.dataset.sort === tasksSortKey ? (tasksSortDir === 'asc' ? '▲' : '▼') : '';
    });
    renderPagination('tasks-pagination', tasksPage, tasksTotalPages, searchTasks);
}

async function triggerSync(kind) {
    const r = await fetch('/api/sync/' + kind, { method: 'POST' });
    const data = await r.json();
    if (data.started) {
        alert('Sincronización lanzada en segundo plano. Vuelve a buscar en un rato.');
    } else {
        alert('Ya hay una sincronización en curso.');
    }
}

// ── Catálogo ────────────────────────────────────────────────────────────

let catalogPage = 1, catalogTotalPages = 1, lastCatalog = [];
let catalogSortKey = 'artist', catalogSortDir = 'asc';

async function loadCatalogMeta() {
    const r = await fetch('/api/catalog/filters/meta');
    const data = await r.json();
    document.getElementById('genres_list').innerHTML = data.genres.map(g => `<option value="${escapeHtml(g)}">`).join('');
    document.getElementById('labels_list').innerHTML = data.labels.map(l => `<option value="${escapeHtml(l)}">`).join('');
}

function clearCatalogFilters() {
    document.querySelectorAll('#tab-catalogo .filters input, #tab-catalogo .filters select').forEach(i => i.value = '');
    searchCatalog(1);
}

document.querySelectorAll('#tab-catalogo th.sortable').forEach(th => {
    th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (catalogSortKey === key) {
            catalogSortDir = catalogSortDir === 'asc' ? 'desc' : 'asc';
        } else {
            catalogSortKey = key;
            catalogSortDir = key === 'days_since' ? 'desc' : 'asc';
        }
        searchCatalog(1);
    });
});

function renderCatalogSortIndicators() {
    document.querySelectorAll('#tab-catalogo th.sortable').forEach(th => {
        const arrow = th.querySelector('.sort-arrow');
        arrow.textContent = th.dataset.sort === catalogSortKey ? (catalogSortDir === 'asc' ? '▲' : '▼') : '';
    });
}

async function searchCatalog(page) {
    catalogPage = page;
    const params = new URLSearchParams({ page: page, sort: catalogSortKey, dir: catalogSortDir });
    const artist = document.getElementById('f_c_artist').value.trim();
    const album = document.getElementById('f_c_album').value.trim();
    const genre = document.getElementById('f_genre').value.trim();
    const label = document.getElementById('f_label').value.trim();
    const yf = document.getElementById('f_year_from').value.trim();
    const yt = document.getElementById('f_year_to').value.trim();
    if (artist) params.set('artist', artist);
    if (album) params.set('album', album);
    if (genre) params.set('genre', genre);
    if (label) params.set('label', label);
    if (yf) params.set('year_from', yf);
    if (yt) params.set('year_to', yt);
    const never = document.getElementById('f_never').value;
    const qbittorrent = document.getElementById('f_qbittorrent').value;
    if (never) params.set('never_listened', never);
    if (qbittorrent) params.set('qbittorrent', qbittorrent);

    const r = await fetch('/api/catalog/search?' + params.toString());
    const data = await r.json();
    catalogTotalPages = Math.max(1, Math.ceil(data.total / data.per_page));
    lastCatalog = data.items;

    document.getElementById('summary-catalog').textContent = `${data.total} álbumes encontrados`;
    document.getElementById('catalog-results').innerHTML = data.items.map((it, i) => `
        <tr class="row-link" data-idx="${i}">
            <td class="clickable" data-field="artist" title="Ver escuchas a lo largo del tiempo">${escapeHtml(it.artist)}</td>
            <td data-owned-artist="${escapeAttr(it.artist)}" data-owned-album="${escapeAttr(it.album)}">${escapeHtml(it.album)}</td>
            <td>${escapeHtml(it.year || '')}</td>
            <td class="clickable" data-field="label">${escapeHtml(it.label || '')}</td>
            <td class="clickable" data-field="genre">${escapeHtml(it.genre || '')}</td>
            <td>${renderFirstListened(it)}</td>
            <td>${formatDaysSince(it.days_since)}</td>
            <td>${it.in_qbittorrent ? '<span class="icon-yes">✓</span>' : '<span class="icon-no">✗</span>'}</td>
        </tr>
    `).join('');
    document.querySelectorAll('#catalog-results tr').forEach((tr, i) => {
        const item = lastCatalog[i];
        tr.addEventListener('click', (evt) => {
            const field = evt.target.dataset.field;
            if (field === 'artist') {
                openArtistChart(item.artist);
                evt.stopPropagation();
                return;
            }
            if (field) {
                document.getElementById('f_' + field).value = item[field];
                searchCatalog(1);
                evt.stopPropagation();
                return;
            }
            openCatalogPanel(item);
        });
    });

    renderCatalogSortIndicators();
    renderPagination('catalog-pagination', catalogPage, catalogTotalPages, searchCatalog);
}

function renderFirstListened(it) {
    if (it.first_listened) {
        return new Date(it.first_listened).toLocaleDateString('es-ES');
    }
    if (it.artist_first_listened) {
        const d = new Date(it.artist_first_listened).toLocaleDateString('es-ES');
        return `<span class="hint-approx" title="Ningún scrobble tiene el tag de álbum exacto (reediciones, recopilatorios...), pero la banda se escucha desde esta fecha">~ ${d}</span>`;
    }
    return '—';
}

// ── Escuchas y descargas (antes discos_olvidados) ───────────────────────

function formatDaysSince(days) {
    if (days === null || days === undefined) return '<span class="never">Nunca</span>';
    const cls = days > 365 ? 'stale-high' : '';
    if (days < 1) return '<span class="' + cls + '">hoy</span>';
    if (days < 30) return `<span class="${cls}">${Math.round(days)} días</span>`;
    if (days < 365) return `<span class="${cls}">${Math.round(days / 30)} meses</span>`;
    return `<span class="${cls}">${(days / 365).toFixed(1)} años</span>`;
}

function setStalenessStatus(text) {
    document.querySelectorAll('.staleness-status').forEach(el => el.textContent = text);
}

async function loadStalenessStatus() {
    const data = await (await fetch('/api/catalog/staleness/status')).json();
    setStalenessStatus(data.computed_at ? `Calculado: ${new Date(data.computed_at).toLocaleString('es-ES')}` : '');
}

async function refreshStaleness() {
    setStalenessStatus('Recalculando (puede tardar unos segundos)…');
    await fetch('/api/catalog/staleness/refresh', { method: 'POST' });
    await loadStalenessStatus();
    if (window._catalogLoaded) searchCatalog(catalogPage);
    if (window._artistsLoaded) searchArtists(artistsPage);
}

let artistsPage = 1, artistsTotalPages = 1;
let artistsSortKey = 'days_since', artistsSortDir = 'desc';

function clearArtistFilters() {
    document.getElementById('fr_artist').value = '';
    document.getElementById('fr_never').value = '';
    searchArtists(1);
}

document.querySelectorAll('#tab-artistas th.sortable').forEach(th => {
    th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (artistsSortKey === key) artistsSortDir = artistsSortDir === 'asc' ? 'desc' : 'asc';
        else { artistsSortKey = key; artistsSortDir = key === 'days_since' ? 'desc' : 'asc'; }
        searchArtists(1);
    });
});

async function searchArtists(page) {
    artistsPage = page;
    const params = new URLSearchParams({ page: page, sort: artistsSortKey, dir: artistsSortDir });
    const artist = document.getElementById('fr_artist').value.trim();
    const never = document.getElementById('fr_never').value;
    if (artist) params.set('artist', artist);
    if (never) params.set('never_listened', never);
    const data = await (await fetch('/api/catalog/artists?' + params.toString())).json();
    artistsTotalPages = Math.max(1, Math.ceil(data.total / data.per_page));
    document.getElementById('summary-artists').textContent = `${data.total} artistas encontrados`;
    document.getElementById('artists-results').innerHTML = data.items.map(it => `
        <tr>
            <td class="clickable" data-value="${escapeAttr(it.artist)}" data-owned-artist="${escapeAttr(it.artist)}" title="Ver escuchas a lo largo del tiempo">${escapeHtml(it.artist)}</td>
            <td>${it.album_count}</td>
            <td>${it.backed_up_count} / ${it.album_count}</td>
            <td>${formatDaysSince(it.days_since)}</td>
        </tr>
    `).join('');
    document.querySelectorAll('#artists-results [data-value]').forEach(el => {
        el.addEventListener('click', () => openArtistChart(el.dataset.value));
    });
    document.querySelectorAll('#tab-artistas th.sortable').forEach(th => {
        th.querySelector('.sort-arrow').textContent = th.dataset.sort === artistsSortKey ? (artistsSortDir === 'asc' ? '▲' : '▼') : '';
    });
    renderPagination('artists-pagination', artistsPage, artistsTotalPages, searchArtists);
}

// ── Biblioteca de Navidrome: Sin escuchas / Escuchados ──────────────────

function formatTs(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString('es-ES') : '—';
}

function renderListenDate(it, ts) {
    if (it.match_kind !== 'tracks') return formatTs(ts);
    return `<span class="hint-approx" title="Ningún scrobble lleva el tag de este álbum, pero ${it.tracks_heard} de sus ${it.song_count} pistas sí tienen escuchas (reediciones, etc.)">~ ${formatTs(ts)}</span>`;
}

const LIBRARY_VIEWS = {
    'sin-escuchas': {
        listened: 'no', sortKey: 'created', sortDir: 'desc', page: 1, items: [], loaded: false,
        search: searchSinEscuchas,
        noun: 'álbumes sin escuchar',
        columns: [
            { key: 'created', label: 'Añadido', render: it => it.created ? new Date(it.created).toLocaleDateString('es-ES') : '—' },
            { key: 'tracks_heard', label: 'Pistas sueltas', title: 'Pistas de este álbum con scrobbles, sin llegar a contar como escuchado (menos de la mitad y ningún scrobble con el tag del álbum)',
              render: it => it.tracks_heard ? `${it.tracks_heard} / ${it.song_count}` : '—' },
        ],
    },
    'escuchados': {
        listened: 'yes', sortKey: 'last_ts', sortDir: 'desc', page: 1, items: [], loaded: false,
        search: searchEscuchados,
        noun: 'álbumes escuchados',
        columns: [
            { key: 'first_ts', label: 'Primera escucha', title: 'Last.fm + ListenBrainz', render: it => renderListenDate(it, it.first_ts) },
            { key: 'last_ts', label: 'Última escucha', title: 'Last.fm + ListenBrainz', render: it => renderListenDate(it, it.last_ts) },
            { key: 'plays', label: 'Scrobbles', render: it => it.plays.toLocaleString('es-ES') },
        ],
    },
};

const LIBRARY_BASE_COLUMNS = [
    { key: 'artist', label: 'Artista' }, { key: 'album', label: 'Álbum' },
    { key: 'year', label: 'Año' }, { key: 'genre', label: 'Género' },
];
const LIBRARY_ASC_FIRST = new Set(['artist', 'album', 'genre']);

// Nombres globales: renderPagination() los invoca por fn.name.
function searchSinEscuchas(page) { return searchLibrary('sin-escuchas', page); }
function searchEscuchados(page) { return searchLibrary('escuchados', page); }

function libraryEl(mode, suffix) {
    return document.getElementById(`lib-${mode}-${suffix}`);
}

function buildLibraryPanel(mode) {
    const view = LIBRARY_VIEWS[mode];
    const th = c => `<th class="sortable" data-sort="${c.key}"${c.title ? ` title="${escapeAttr(c.title)}"` : ''}>${c.label}<span class="sort-arrow"></span></th>`;
    const field = (name, label, attrs) => `<div><label>${label}</label><input id="lib-${mode}-${name}" ${attrs || 'type="text"'}></div>`;
    const panel = document.getElementById('tab-' + mode);
    panel.innerHTML = `
        <div class="filters">
            ${field('artist', 'Artista')}${field('album', 'Álbum')}${field('genre', 'Género')}
            ${field('year_from', 'Año desde', 'type="number" placeholder="1990"')}
            ${field('year_to', 'Año hasta', 'type="number" placeholder="2026"')}
            <div class="actions">
                <button data-action="search">Buscar</button>
                <button class="secondary" data-action="clear">Limpiar</button>
                <button class="secondary" data-action="sync" title="Vuelve a leer la biblioteca de Navidrome y a cruzarla con Last.fm + ListenBrainz">Actualizar desde Navidrome</button>
                <span class="staleness-status library-status"></span>
            </div>
        </div>
        <div class="library-notice" id="lib-${mode}-notice" hidden></div>
        <div class="library-summary" id="lib-${mode}-summary"></div>
        <table>
            <thead><tr>
                <th class="check-cell"><input type="checkbox" id="lib-${mode}-all" title="Seleccionar esta página"></th>
                ${LIBRARY_BASE_COLUMNS.concat(view.columns).map(th).join('')}
            </tr></thead>
            <tbody id="lib-${mode}-results"></tbody>
        </table>
        <div class="pagination" id="lib-${mode}-pagination"></div>
    `;
    panel.querySelector('[data-action=search]').addEventListener('click', () => view.search(1));
    panel.querySelector('[data-action=sync]').addEventListener('click', syncLibrary);
    panel.querySelector('[data-action=clear]').addEventListener('click', () => {
        panel.querySelectorAll('.filters input').forEach(i => i.value = '');
        view.search(1);
    });
    panel.querySelectorAll('.filters input').forEach(i => i.addEventListener('keydown', e => {
        if (e.key === 'Enter') view.search(1);
    }));
    panel.querySelectorAll('th.sortable').forEach(el => el.addEventListener('click', () => {
        const key = el.dataset.sort;
        if (view.sortKey === key) view.sortDir = view.sortDir === 'asc' ? 'desc' : 'asc';
        else { view.sortKey = key; view.sortDir = LIBRARY_ASC_FIRST.has(key) ? 'asc' : 'desc'; }
        view.search(1);
    }));
    libraryEl(mode, 'all').addEventListener('change', e => {
        view.items.forEach(it => setSelected(it, e.target.checked));
        renderLibrarySelection();
    });
}

async function searchLibrary(mode, page) {
    const view = LIBRARY_VIEWS[mode];
    view.page = page;
    const params = new URLSearchParams({ listened: view.listened, page: page, sort: view.sortKey, dir: view.sortDir });
    ['artist', 'album', 'genre', 'year_from', 'year_to'].forEach(name => {
        const value = libraryEl(mode, name).value.trim();
        if (value) params.set(name, value);
    });
    const [data] = await Promise.all([
        fetch('/api/library?' + params.toString()).then(r => r.json()),
        loadLibraryStatus(),
    ]);
    view.items = data.items;

    libraryEl(mode, 'summary').textContent = `${data.total} ${view.noun}`;
    libraryEl(mode, 'results').innerHTML = data.items.map((it, i) => `
        <tr class="row-link" data-idx="${i}">
            <td class="check-cell"><input type="checkbox" data-idx="${i}"></td>
            <td class="clickable" data-field="artist" title="Ver escuchas a lo largo del tiempo">${escapeHtml(it.artist)}</td>
            <td data-owned-artist="${escapeAttr(it.artist)}" data-owned-album="${escapeAttr(it.album)}">${escapeHtml(it.album)}</td>
            <td>${it.year || ''}</td>
            <td>${escapeHtml(it.genre || '')}</td>
            ${view.columns.map(c => `<td>${c.render(it)}</td>`).join('')}
        </tr>
    `).join('');
    libraryEl(mode, 'results').querySelectorAll('tr').forEach(tr => {
        const item = view.items[parseInt(tr.dataset.idx)];
        tr.addEventListener('click', evt => {
            if (evt.target.closest('.check-cell')) return;
            if (evt.target.dataset.field === 'artist') openArtistChart(item.artist);
            else openLibraryPanel(item);
        });
        tr.querySelector('input').addEventListener('change', e => {
            setSelected(item, e.target.checked);
            renderLibrarySelection();
        });
    });

    document.querySelectorAll(`#tab-${mode} th.sortable`).forEach(th => {
        th.querySelector('.sort-arrow').textContent = th.dataset.sort === view.sortKey ? (view.sortDir === 'asc' ? '▲' : '▼') : '';
    });
    renderPagination(`lib-${mode}-pagination`, page, Math.max(1, Math.ceil(data.total / data.per_page)), view.search);
    renderLibrarySelection();
}

function openLibraryPanel(item) {
    const body = document.getElementById('panel-body');
    body.innerHTML = `
        <div class="panel-title">${escapeHtml(item.album)}</div>
        <div class="panel-sub">${escapeHtml(item.artist)}</div>
        <span class="owned-badge">En Navidrome</span>
        <div id="panel-extra"><div class="panel-empty" style="margin-top:16px">Cargando…</div></div>
    `;
    document.getElementById('panel').classList.add('open');
    fetch(`/api/library/${encodeURIComponent(item.id)}/enrichment`)
        .then(r => r.json())
        .then(info => { document.getElementById('panel-extra').innerHTML = renderPanelExtra(info); })
        .catch(() => { document.getElementById('panel-extra').innerHTML = '<div class="panel-empty">No se pudo cargar la info.</div>'; });
}

async function loadLibraryStatus() {
    const st = await (await fetch('/api/library/status')).json();
    let text = '', notice = '';
    if (st.running) text = 'Sincronizando…';
    else if (st.synced_at) text = `Sincronizado: ${new Date(st.synced_at).toLocaleString('es-ES')} (usuario ${st.user})`;
    if (!st.configured) notice = 'Faltan NAVIDROME_USER y NAVIDROME_PASS en el .env del servicio.';
    else if (st.error) notice = 'Última sincronización fallida: ' + st.error;
    else if (!st.synced_at && !st.running) notice = 'Aún no se ha leído la biblioteca: pulsa «Actualizar desde Navidrome».';
    document.querySelectorAll('.library-status').forEach(el => el.textContent = text);
    Object.keys(LIBRARY_VIEWS).forEach(mode => {
        const el = libraryEl(mode, 'notice');
        el.textContent = notice;
        el.hidden = !notice;
    });
    return st;
}

async function syncLibrary() {
    const r = await fetch('/api/library/sync', { method: 'POST' });
    const data = await r.json();
    if (!r.ok) { await loadLibraryStatus(); return; }
    if (!data.started) {
        document.querySelectorAll('.library-status').forEach(el => el.textContent = 'Ya hay una sincronización en curso; prueba en un momento.');
        return;
    }
    while ((await loadLibraryStatus()).running) {
        await new Promise(resolve => setTimeout(resolve, 2000));
    }
    Object.values(LIBRARY_VIEWS).forEach(v => { if (v.loaded) v.search(v.page); });
}

// ── Selección y playlists ───────────────────────────────────────────────
// La selección sobrevive a la paginación y al cambio de pestaña.

const selectedAlbums = new Map();
let playlistsLoaded = false;

function setSelected(item, on) {
    if (on) selectedAlbums.set(item.id, item);
    else selectedAlbums.delete(item.id);
}

function clearSelection() {
    selectedAlbums.clear();
    setPlaylistStatus('');
    renderLibrarySelection();
}

function setPlaylistStatus(text, isError) {
    const el = document.getElementById('pl_status');
    el.textContent = text;
    el.classList.toggle('error', !!isError);
}

function renderLibrarySelection() {
    Object.entries(LIBRARY_VIEWS).forEach(([mode, view]) => {
        if (!view.loaded) return;
        libraryEl(mode, 'results').querySelectorAll('input[type=checkbox]').forEach(cb => {
            cb.checked = selectedAlbums.has(view.items[parseInt(cb.dataset.idx)].id);
        });
        const all = libraryEl(mode, 'all');
        const selectedHere = view.items.filter(it => selectedAlbums.has(it.id)).length;
        all.checked = view.items.length > 0 && selectedHere === view.items.length;
        all.indeterminate = selectedHere > 0 && !all.checked;
    });
    updatePlaylistBar();
}

function updatePlaylistBar() {
    const activeTab = document.querySelector('.tab-btn.active').dataset.tab;
    const n = selectedAlbums.size;
    const visible = n > 0 && !!LIBRARY_VIEWS[activeTab];
    document.getElementById('playlist-bar').classList.toggle('visible', visible);
    document.body.classList.toggle('has-playlist-bar', visible);
    document.getElementById('pl_count').textContent = `${n} ${n === 1 ? 'álbum' : 'álbumes'}`;
    document.getElementById('pl_name').hidden = document.getElementById('pl_target').value !== '';
    if (visible && !playlistsLoaded) { playlistsLoaded = true; loadPlaylists(); }
}

async function loadPlaylists() {
    const select = document.getElementById('pl_target');
    const current = select.value;
    let items = [];
    try {
        const r = await fetch('/api/navidrome/playlists');
        const data = await r.json();
        if (!r.ok) throw new Error(data.error);
        items = data.items;
    } catch (e) {
        playlistsLoaded = false;  // reintenta en la próxima selección
        setPlaylistStatus('No se pudieron cargar las playlists: ' + e.message, true);
    }
    select.innerHTML = '<option value="">Nueva playlist…</option>' + items.map(p =>
        `<option value="${escapeAttr(p.id)}">${escapeHtml(p.name)} (${p.song_count})</option>`).join('');
    select.value = items.some(p => p.id === current) ? current : '';
    updatePlaylistBar();
}

async function addSelectionToPlaylist() {
    const playlistId = document.getElementById('pl_target').value;
    const name = document.getElementById('pl_name').value.trim();
    if (!playlistId && !name) { setPlaylistStatus('Escribe el nombre de la nueva playlist.', true); return; }
    const button = document.getElementById('pl_add');
    button.disabled = true;
    setPlaylistStatus('Añadiendo…');
    try {
        const r = await fetch('/api/navidrome/playlist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album_ids: [...selectedAlbums.keys()], playlist_id: playlistId, name: playlistId ? '' : name }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error);
        const failed = data.failed_albums.map(id => selectedAlbums.get(id));
        selectedAlbums.clear();
        failed.forEach(it => selectedAlbums.set(it.id, it));  // los fallidos quedan para reintentar
        document.getElementById('pl_name').value = '';
        await loadPlaylists();
        document.getElementById('pl_target').value = data.playlist_id;
        renderLibrarySelection();
        const msg = `${data.added} canciones añadidas a «${data.name}»`;
        if (failed.length) {
            setPlaylistStatus(`${msg}. Fallaron ${failed.length} álbumes (siguen seleccionados).`, true);
        } else {
            alert(msg);
            setPlaylistStatus('');
        }
    } catch (e) {
        setPlaylistStatus(e.message, true);
    } finally {
        button.disabled = false;
    }
}

Object.keys(LIBRARY_VIEWS).forEach(buildLibraryPanel);

// ── Paginación (compartida) ─────────────────────────────────────────────

function renderPagination(elId, page, totalPages, fn) {
    const el = document.getElementById(elId);
    let html = `<button ${page <= 1 ? 'disabled' : ''} onclick="(${fn.name})(${page - 1})">&laquo;</button>`;
    const start = Math.max(1, page - 3), end = Math.min(totalPages, page + 3);
    for (let p = start; p <= end; p++) {
        html += `<button class="${p === page ? 'active' : ''}" onclick="(${fn.name})(${p})">${p}</button>`;
    }
    html += `<button ${page >= totalPages ? 'disabled' : ''} onclick="(${fn.name})(${page + 1})">&raquo;</button>`;
    el.innerHTML = html;
}

// ── Panel lateral ───────────────────────────────────────────────────────

function openTaskPanel(task) {
    const panel = document.getElementById('panel');
    const body = document.getElementById('panel-body');
    body.innerHTML = `
        <div class="panel-title">${escapeHtml(task.album)}</div>
        <div class="panel-sub">${escapeHtml(task.artist)}</div>
        ${task.downloaded ? '<span class="owned-badge">Ya descargado</span>' : ''}
        <div id="panel-extra"><div class="panel-empty" style="margin-top:16px">Cargando…</div></div>
    `;
    panel.classList.add('open');
    fetch(`/api/tasks/${encodeURIComponent(task.uid)}/enrichment`)
        .then(r => r.json())
        .then(info => { document.getElementById('panel-extra').innerHTML = renderPanelExtra(info); })
        .catch(() => { document.getElementById('panel-extra').innerHTML = '<div class="panel-empty">No se pudo cargar la info.</div>'; });
}

function openCatalogPanel(item) {
    const panel = document.getElementById('panel');
    const body = document.getElementById('panel-body');
    body.innerHTML = `
        <div class="panel-title">${escapeHtml(item.album)}</div>
        <div class="panel-sub">${escapeHtml(item.artist)}</div>
        <span class="owned-badge">En tu colección</span>
        <div id="panel-extra"><div class="panel-empty" style="margin-top:16px">Cargando…</div></div>
    `;
    panel.classList.add('open');
    fetch(`/api/catalog/album/${item.id}`)
        .then(r => r.json())
        .then(info => { document.getElementById('panel-extra').innerHTML = renderPanelExtra(info); })
        .catch(() => { document.getElementById('panel-extra').innerHTML = '<div class="panel-empty">No se pudo cargar la info.</div>'; });
}

function closePanel() {
    document.getElementById('panel').classList.remove('open');
}

// ── Gráfico de escuchas por artista (banda) ────────────────────────────

async function openArtistChart(artistName) {
    const panel = document.getElementById('panel');
    const body = document.getElementById('panel-body');
    body.innerHTML = `
        <div class="panel-title">${escapeHtml(artistName)}</div>
        <div class="panel-sub">Escuchas a lo largo del tiempo</div>
        <div id="panel-extra"><div class="panel-empty" style="margin-top:16px">Cargando…</div></div>
    `;
    panel.classList.add('open');
    try {
        const r = await fetch('/api/catalog/artist_listens?artist=' + encodeURIComponent(artistName));
        const data = await r.json();
        document.getElementById('panel-extra').innerHTML = renderArtistChart(data);
    } catch {
        document.getElementById('panel-extra').innerHTML = '<div class="panel-empty" style="margin-top:16px">No se pudo cargar el histórico de escuchas.</div>';
    }
}

function renderArtistChart(data) {
    if (!data.timeline || data.timeline.length === 0) {
        return '<div class="panel-empty" style="margin-top:16px">Sin escuchas registradas en Last.fm para esta banda.</div>';
    }
    const firstDate = new Date(data.first_listened).toLocaleDateString('es-ES');
    const peak = data.timeline.reduce((m, p) => p.plays > m.plays ? p : m, data.timeline[0]);
    return panelSection('Escuchas (Last.fm)', `
        <div class="chart-stats">
            <div class="chart-stat">Primera escucha<strong>${escapeHtml(firstDate)}</strong></div>
            <div class="chart-stat">Total scrobbles<strong>${data.total_plays.toLocaleString('es-ES')}</strong></div>
            <div class="chart-stat">Mes con más escuchas<strong>${escapeHtml(peak.month)} (${peak.plays})</strong></div>
        </div>
        ${buildLineChartSvg(data.timeline)}
        <div class="chart-tooltip-hint">Pasa el ratón por un punto para ver el mes y el nº de escuchas.</div>
    `);
}

function buildLineChartSvg(points) {
    const W = 330, H = 140, PAD_L = 8, PAD_R = 8, PAD_T = 14, PAD_B = 20;
    const innerW = W - PAD_L - PAD_R, innerH = H - PAD_T - PAD_B;
    const maxPlays = Math.max(...points.map(p => p.plays));
    const stepX = points.length > 1 ? innerW / (points.length - 1) : 0;

    const coords = points.map((p, i) => ({
        x: PAD_L + i * stepX,
        y: PAD_T + innerH - (maxPlays ? (p.plays / maxPlays) * innerH : 0),
        ...p,
    }));

    const linePath = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
    const areaPath = `${linePath} L${coords[coords.length - 1].x.toFixed(1)},${PAD_T + innerH} L${coords[0].x.toFixed(1)},${PAD_T + innerH} Z`;

    const dots = coords.map(c => `
        <circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="3" fill="var(--accent)">
            <title>${escapeHtml(c.month)}: ${c.plays} escucha${c.plays === 1 ? '' : 's'}</title>
        </circle>
    `).join('');

    return `
        <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="display:block">
            <line x1="${PAD_L}" y1="${PAD_T + innerH}" x2="${W - PAD_R}" y2="${PAD_T + innerH}" stroke="var(--border)" stroke-width="1"/>
            <path d="${areaPath}" fill="var(--accent)" fill-opacity="0.12" stroke="none"/>
            <path d="${linePath}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            ${dots}
            <text x="${PAD_L}" y="${H - 4}" font-size="10" fill="var(--text-muted)">${escapeHtml(points[0].month)}</text>
            <text x="${W - PAD_R}" y="${H - 4}" font-size="10" fill="var(--text-muted)" text-anchor="end">${escapeHtml(points[points.length - 1].month)}</text>
        </svg>
    `;
}

function panelSection(title, html) {
    return `<div class="panel-section"><h4>${title}</h4>${html}</div>`;
}

function renderPanelExtra(info) {
    let html = '';

    const al = info.album || {};
    if (al.name) {
        const meta = [al.label, al.year, al.genre].filter(Boolean).join(' · ');
        html += panelSection('Álbum', `
            ${al.image ? `<img class="panel-img" src="${escapeHtml(al.image)}">` : ''}
            ${meta ? `<div class="panel-sub">${escapeHtml(meta)}</div>` : ''}
            ${al.wiki ? `<div class="panel-text">${escapeHtml(al.wiki)}</div>` : ''}
            ${al.url ? `<a class="panel-link" href="${escapeHtml(al.url)}" target="_blank" rel="noopener">Ver en Last.fm ↗</a>` : ''}
            ${al.wikipedia_url ? `<a class="panel-link" href="${escapeHtml(al.wikipedia_url)}" target="_blank" rel="noopener">Wikipedia ↗</a>` : ''}
            ${al.discogs_url ? `<a class="panel-link" href="${escapeHtml(al.discogs_url)}" target="_blank" rel="noopener">Discogs ↗</a>` : ''}
        `);
    }

    const ar = info.artist || {};
    if (ar.name) {
        const bio = ar.bio || ar.wikipedia_content || '';
        html += panelSection('Artista', `
            ${ar.image ? `<img class="panel-img" src="${escapeHtml(ar.image)}">` : ''}
            ${ar.listeners ? `<div class="panel-sub">${parseInt(ar.listeners).toLocaleString('es-ES')} oyentes</div>` : ''}
            ${bio ? `<div class="panel-text">${escapeHtml(bio)}</div>` : ''}
            ${ar.url ? `<a class="panel-link" href="${escapeHtml(ar.url)}" target="_blank" rel="noopener">Ver en Last.fm ↗</a>` : ''}
            ${ar.wikipedia_url ? `<a class="panel-link" href="${escapeHtml(ar.wikipedia_url)}" target="_blank" rel="noopener">Wikipedia ↗</a>` : ''}
        `);
    }

    const wiki = info.wikipedia || {};
    if (wiki.extract || wiki.url) {
        html += panelSection('Wikipedia', `
            ${wiki.extract ? `<div class="panel-text">${escapeHtml(wiki.extract)}</div>` : ''}
            ${wiki.url ? `<a class="panel-link" href="${escapeHtml(wiki.url)}" target="_blank" rel="noopener">Ver en Wikipedia ↗</a>` : ''}
        `);
    }

    const mb = info.musicbrainz || {};
    if (mb.artist_url || mb.release_group_url) {
        html += panelSection('MusicBrainz', `
            ${mb.artist_url ? `<a class="panel-link" href="${escapeHtml(mb.artist_url)}" target="_blank" rel="noopener">Artista en MusicBrainz ↗</a><br>` : ''}
            ${mb.release_group_url ? `<a class="panel-link" href="${escapeHtml(mb.release_group_url)}" target="_blank" rel="noopener">Álbum en MusicBrainz ↗</a>` : ''}
        `);
    }

    const lb = info.label || {};
    if (lb.name) {
        html += panelSection('Sello', `
            <div class="panel-sub" style="font-weight:bold;color:#fff">${escapeHtml(lb.name)}</div>
            ${lb.description ? `<div class="panel-text">${escapeHtml(lb.description)}</div>` : ''}
            ${lb.wikipedia_url ? `<a class="panel-link" href="${escapeHtml(lb.wikipedia_url)}" target="_blank" rel="noopener">Wikipedia ↗</a>` : ''}
            ${lb.discogs_url ? `<a class="panel-link" href="${escapeHtml(lb.discogs_url)}" target="_blank" rel="noopener">Discogs ↗</a>` : ''}
        `);
    }

    return html || '<div class="panel-empty" style="margin-top:16px">Sin información adicional.</div>';
}

searchTasks(1);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
