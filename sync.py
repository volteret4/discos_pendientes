#!/usr/bin/env python3
"""Sincronización: refresca las tareas del calendario "discos pendientes",
comprueba qué está descargado (Airsonic/qBittorrent) y precalienta el
enriquecimiento en vivo (Last.fm/Wikipedia/MusicBrainz) para los que no
están en la biblioteca local. También recorre el calendario de
lanzamientos ("discos", ver radicale_writer.py) marcando en Radicale
(icono + fecha) lo que ya esté descargado, esté o no en la lista de
pendientes. Y cruza el catálogo local con los scrobbles de Last.fm para
cachear la "primera escucha" de cada álbum (ver listens.py). Y lo mismo
con la biblioteca de Navidrome para las pestañas Sin escuchas/Escuchados.

Uso:
    python3 sync.py --all               # backfill completo (ignora el caché de edad)
    python3 sync.py                     # incremental (solo lo que falta/está viejo)
    python3 sync.py --release-calendar --dry-run  # cuenta cuántos del calendario de lanzamientos se marcarían
    python3 sync.py --release-calendar  # fuerza una pasada completa del calendario de lanzamientos
"""
import argparse
from datetime import datetime, timedelta, timezone

import navidrome_client
import radicale_client
import radicale_writer
from airsonic_client import search_airsonic
from db import get_conn, get_lastfm_conn, get_musica_local_conn
from enrich import get_or_fetch_enrichment
from listens import compute_album_first_listens, compute_artist_first_listens, compute_library_rows, load_listen_index
from qbittorrent_client import get_torrents, search_qbittorrent

STALE_HOURS = 24


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_tasks() -> int:
    tasks = radicale_client.fetch_pending_tasks()
    conn = get_conn()
    for t in tasks:
        conn.execute(
            """
            INSERT INTO tasks (uid, href, artist, album, artist_norm, album_norm, status, dtstart, summary_raw, synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                href=excluded.href, artist=excluded.artist, album=excluded.album,
                artist_norm=excluded.artist_norm, album_norm=excluded.album_norm,
                status=excluded.status, dtstart=excluded.dtstart,
                summary_raw=excluded.summary_raw, synced_at=excluded.synced_at
            """,
            (
                t["uid"], t["href"], t["artist"], t["album"], t["artist_norm"], t["album_norm"],
                t["status"], t["dtstart"].isoformat() if t["dtstart"] else None, t["summary_raw"], _now(),
            ),
        )
    conn.commit()
    conn.close()
    print(f"tasks: {len(tasks)} sincronizadas")
    return len(tasks)


def sync_downloaded_status(stale_only: bool = True) -> int:
    conn = get_conn()
    if stale_only:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)).isoformat()
        rows = conn.execute(
            """
            SELECT t.uid, t.artist, t.album FROM tasks t
            LEFT JOIN pending_status ps ON ps.uid = t.uid
            WHERE t.status = 'NEEDS-ACTION' AND (ps.uid IS NULL OR ps.checked_at < ?)
            """,
            (cutoff,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT uid, artist, album FROM tasks WHERE status = 'NEEDS-ACTION'"
        ).fetchall()

    if not rows:
        conn.close()
        print("downloaded_status: nada que comprobar")
        return 0

    print(f"downloaded_status: comprobando {len(rows)} pendientes...")
    try:
        torrents = get_torrents()
    except Exception as e:
        print(f"  aviso: no se pudo conectar a qBittorrent ({e}), se omite ese check")
        torrents = []

    checked = 0
    for r in rows:
        airsonic_date = search_airsonic(r["artist"], r["album"])
        qb_date = search_qbittorrent(torrents, r["artist"], r["album"]) if torrents else None
        downloaded = int(bool(airsonic_date or qb_date))
        conn.execute(
            """
            INSERT INTO pending_status (uid, airsonic_found, airsonic_date, qbittorrent_found, qbittorrent_date, downloaded, checked_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                airsonic_found=excluded.airsonic_found, airsonic_date=excluded.airsonic_date,
                qbittorrent_found=excluded.qbittorrent_found, qbittorrent_date=excluded.qbittorrent_date,
                downloaded=excluded.downloaded, checked_at=excluded.checked_at
            """,
            (
                r["uid"], int(bool(airsonic_date)), airsonic_date.isoformat() if airsonic_date else None,
                int(bool(qb_date)), qb_date.isoformat() if qb_date else None, downloaded, _now(),
            ),
        )
        checked += 1
    conn.commit()
    conn.close()
    print(f"downloaded_status: {checked} comprobadas")
    return checked


def sync_enrichment(stale_only: bool = True) -> int:
    conn = get_conn()
    if stale_only:
        rows = conn.execute(
            """
            SELECT DISTINCT t.artist, t.album FROM tasks t
            LEFT JOIN enrichment e ON e.artist_norm = t.artist_norm AND e.album_norm = t.album_norm
            WHERE t.status = 'NEEDS-ACTION' AND e.artist_norm IS NULL
            """
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT artist, album FROM tasks WHERE status = 'NEEDS-ACTION'"
        ).fetchall()

    print(f"enrichment: precalentando {len(rows)} álbumes...")
    done = 0
    for r in rows:
        try:
            get_or_fetch_enrichment(conn, r["artist"], r["album"])
        except Exception as e:
            print(f"  fallo en {r['artist']} - {r['album']}: {e}")
        done += 1
        if done % 20 == 0:
            print(f"  {done}/{len(rows)}")
    conn.close()
    print(f"enrichment: {done} procesados")
    return done


def sync_album_first_listens() -> int:
    """Recalcula por completo la caché de "primera escucha" por álbum
    (album_first_listen), cruzando musica_local.sqlite con los scrobbles
    de Last.fm. Cruce local SQLite->SQLite sin red -- a diferencia de
    sync_downloaded_status()/sync_enrichment(), no hace falta staleness,
    se recalcula entero en cada pasada (mismo patrón de recálculo
    completo que discos_olvidados/staleness.py)."""
    musica_conn = get_musica_local_conn()
    lastfm_conn = get_lastfm_conn()
    try:
        rows = compute_album_first_listens(musica_conn, lastfm_conn)
    finally:
        musica_conn.close()
        lastfm_conn.close()

    conn = get_conn()
    now = _now()
    for r in rows:
        conn.execute(
            """
            INSERT INTO album_first_listen (artist_norm, album_norm, first_ts, synced_at)
            VALUES (?,?,?,?)
            ON CONFLICT(artist_norm, album_norm) DO UPDATE SET
                first_ts=excluded.first_ts, synced_at=excluded.synced_at
            """,
            (r["artist_norm"], r["album_norm"], r["first_ts"], now),
        )
    conn.commit()
    conn.close()
    print(f"album_first_listens: {len(rows)} álbumes procesados")
    return len(rows)


def sync_artist_first_listens() -> int:
    """Fallback de sync_album_first_listens() a nivel de artista -- ver
    listens.compute_artist_first_listens(). Mismo patrón de recálculo
    completo sin staleness (cruce local SQLite->SQLite, sin red)."""
    musica_conn = get_musica_local_conn()
    lastfm_conn = get_lastfm_conn()
    try:
        rows = compute_artist_first_listens(musica_conn, lastfm_conn)
    finally:
        musica_conn.close()
        lastfm_conn.close()

    conn = get_conn()
    now = _now()
    for r in rows:
        conn.execute(
            """
            INSERT INTO artist_first_listen (artist_norm, first_ts, synced_at)
            VALUES (?,?,?)
            ON CONFLICT(artist_norm) DO UPDATE SET
                first_ts=excluded.first_ts, synced_at=excluded.synced_at
            """,
            (r["artist_norm"], r["first_ts"], now),
        )
    conn.commit()
    conn.close()
    print(f"artist_first_listens: {len(rows)} artistas procesados")
    return len(rows)


def sync_library() -> int:
    """Reemplaza library_albums con lo que ve NAVIDROME_USER cruzado con
    los scrobbles. Lanza NavidromeError si Navidrome falla: todo se lee
    antes de abrir la transacción, así un fallo conserva los datos previos
    en vez de vaciar las pestañas."""
    albums = navidrome_client.fetch_albums()
    songs = navidrome_client.fetch_songs()
    if not albums:
        raise navidrome_client.NavidromeError("Navidrome no ha devuelto ningún álbum; se conservan los datos anteriores")

    lastfm_conn = get_lastfm_conn()
    try:
        rows = compute_library_rows(albums, songs, load_listen_index(lastfm_conn))
    finally:
        lastfm_conn.close()

    now = _now()
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM library_albums")
        conn.executemany(
            """
            INSERT INTO library_albums (id, artist, album, year, genre, created, song_count, tracks_heard,
                                        match_kind, first_ts, last_ts, plays, synced_at)
            VALUES (:id, :artist, :album, :year, :genre, :created, :song_count, :tracks_heard,
                    :match_kind, :first_ts, :last_ts, :plays, :synced_at)
            """,
            [{**r, "synced_at": now} for r in rows],
        )
    conn.close()
    heard = sum(1 for r in rows if r["match_kind"])
    print(f"library: {len(rows)} álbumes, {heard} escuchados, {len(rows) - heard} sin escuchas")
    return len(rows)


def _sync_library_safe() -> None:
    """Para la pasada de cron: sin credenciales o con Navidrome caído no
    debe tumbar el resto del sync."""
    if not navidrome_client.is_configured():
        print("library: omitido, faltan NAVIDROME_USER / NAVIDROME_PASS")
        return
    try:
        sync_library()
    except navidrome_client.NavidromeError as e:
        print(f"library: aviso, no se pudo sincronizar ({e})")


def sync_all() -> None:
    sync_tasks()
    sync_downloaded_status(stale_only=False)
    sync_enrichment(stale_only=False)
    sync_release_calendar(stale_only=False)
    sync_album_first_listens()
    sync_artist_first_listens()
    _sync_library_safe()
    print("Hecho.")


def sync_release_calendar(stale_only: bool = True, dry_run: bool = False) -> dict:
    """Recorre TODO el calendario de lanzamientos ("discos", ver
    radicale_writer.RELEASE_CALENDAR_NAME) -- no solo los álbumes que
    llegaron a convertirse en tarea VTODO en CALENDAR_TASKS -- y marca en
    Radicale (icono 💾 + fecha) los que ya estén descargados
    (Airsonic/qBittorrent). Un álbum puede descargarse sin haber pasado
    nunca por la lista de "discos_pendientes" (ej. se compró directamente
    al salir), así que este chequeo es independiente de
    sync_downloaded_status()/pending_status -- se apoya en su propia
    tabla, release_marks, indexada por UID de VEVENT.

    stale_only=True (uso diario): solo comprueba los VEVENT aún no
    marcados cuyo último chequeo tiene más de STALE_HOURS, para no
    repetir cientos de búsquedas en Airsonic cada día contra álbumes que
    ya se comprobó que no estaban."""
    events = radicale_writer.fetch_release_events()
    conn = get_conn()
    known = {r["uid"]: r for r in conn.execute(
        "SELECT uid, radicale_marked_at, checked_at FROM release_marks"
    ).fetchall()}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)).isoformat()
    to_check = []
    for e in events:
        prev = known.get(e["uid"])
        if prev and prev["radicale_marked_at"]:
            continue  # ya marcado
        if stale_only and prev and prev["checked_at"] >= cutoff:
            continue  # comprobado hace poco, seguía sin descargar
        to_check.append(e)

    stats = {"total": len(to_check), "marked": 0, "still_pending": 0, "errors": 0}
    if not to_check:
        conn.close()
        print("release_calendar: nada que comprobar")
        return stats

    print(f"release_calendar: comprobando {len(to_check)} lanzamientos{' (dry-run)' if dry_run else ''}...")
    try:
        torrents = get_torrents()
    except Exception as e:
        print(f"  aviso: no se pudo conectar a qBittorrent ({e}), se omite ese check")
        torrents = []

    for e in to_check:
        try:
            airsonic_date = search_airsonic(e["artist"], e["album"])
            qb_date = search_qbittorrent(torrents, e["artist"], e["album"]) if torrents else None
        except Exception as ex:
            print(f"  error comprobando {e['artist']} - {e['album']}: {ex}")
            stats["errors"] += 1
            continue

        download_date = airsonic_date or qb_date
        now = _now()
        radicale_marked_at = None
        if download_date:
            if dry_run:
                stats["marked"] += 1
            else:
                try:
                    ok = radicale_writer.mark_downloaded(e["artist"], e["album"], download_date, events=events)
                except Exception as ex:
                    print(f"  error marcando en Radicale {e['artist']} - {e['album']}: {ex}")
                    ok = False
                if ok:
                    radicale_marked_at = now
                    stats["marked"] += 1
        else:
            stats["still_pending"] += 1

        if not dry_run:
            conn.execute(
                """
                INSERT INTO release_marks (uid, artist, album, downloaded, checked_at, radicale_marked_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET
                    downloaded=excluded.downloaded, checked_at=excluded.checked_at,
                    radicale_marked_at=COALESCE(release_marks.radicale_marked_at, excluded.radicale_marked_at)
                """,
                (e["uid"], e["artist"], e["album"], int(bool(download_date)), now, radicale_marked_at),
            )

    if not dry_run:
        conn.commit()
    conn.close()
    print(f"release_calendar: {stats['marked']} marcados, {stats['still_pending']} aún sin descargar, "
          f"{stats['errors']} errores (de {stats['total']})")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="backfill completo, ignora el caché de edad")
    parser.add_argument("--release-calendar", action="store_true",
                         help="fuerza una pasada completa (stale_only=False) del calendario de lanzamientos")
    parser.add_argument("--dry-run", action="store_true",
                         help="con --release-calendar: solo cuenta, no escribe en Radicale ni en la DB")
    args = parser.parse_args()

    if args.release_calendar:
        sync_release_calendar(stale_only=False, dry_run=args.dry_run)
    elif args.all:
        sync_all()
    else:
        sync_tasks()
        sync_downloaded_status(stale_only=True)
        sync_enrichment(stale_only=True)
        sync_release_calendar(stale_only=True)
        sync_album_first_listens()
        sync_artist_first_listens()
        _sync_library_safe()
