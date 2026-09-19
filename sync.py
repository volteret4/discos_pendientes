#!/usr/bin/env python3
"""Sincronización: refresca las tareas del calendario "discos pendientes",
comprueba qué está descargado (Airsonic/qBittorrent) y precalienta el
enriquecimiento en vivo (Last.fm/Wikipedia/MusicBrainz) para los que no
están en la biblioteca local.

Uso:
    python3 sync.py --all      # backfill completo (ignora el caché de edad)
    python3 sync.py            # incremental (solo lo que falta/está viejo)
"""
import argparse
from datetime import date, datetime, timedelta, timezone

import radicale_client
import radicale_writer
from airsonic_client import search_airsonic
from db import get_conn
from enrich import get_or_fetch_enrichment
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
            SELECT t.uid, t.artist, t.album, ps.downloaded AS prev_downloaded,
                   ps.radicale_marked_at AS prev_marked
            FROM tasks t
            LEFT JOIN pending_status ps ON ps.uid = t.uid
            WHERE t.status = 'NEEDS-ACTION' AND (ps.uid IS NULL OR ps.checked_at < ?)
            """,
            (cutoff,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT t.uid, t.artist, t.album, ps.downloaded AS prev_downloaded,
                   ps.radicale_marked_at AS prev_marked
            FROM tasks t
            LEFT JOIN pending_status ps ON ps.uid = t.uid
            WHERE t.status = 'NEEDS-ACTION'
            """
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

        newly_downloaded = downloaded == 1 and r["prev_downloaded"] != 1
        if newly_downloaded and not r["prev_marked"]:
            try:
                marked = radicale_writer.mark_downloaded(r["artist"], r["album"], date.today())
            except Exception as e:
                print(f"  aviso: no se pudo marcar en Radicale ({r['artist']} - {r['album']}): {e}")
                marked = False
            if marked:
                conn.execute(
                    "UPDATE pending_status SET radicale_marked_at = ? WHERE uid = ?",
                    (_now(), r["uid"]),
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


def sync_all() -> None:
    sync_tasks()
    sync_downloaded_status(stale_only=False)
    sync_enrichment(stale_only=False)
    print("Hecho.")


def backfill_radicale_icons(dry_run: bool = False, limit: int | None = None) -> dict:
    """Migración de datos histórica (correr a mano una vez, no en el cron
    diario): para álbumes ya marcados como descargados antes de que
    existiera radicale_writer.py, busca su VEVENT y le añade el icono +
    fecha. Usa la fecha de descarga real (airsonic/qbittorrent), no
    "hoy". No todas las tareas tienen un VEVENT de lanzamiento
    correspondiente -- se cuentan aparte, no es un error."""
    conn = get_conn()
    query = (
        "SELECT t.uid, t.artist, t.album, ps.airsonic_date, ps.qbittorrent_date "
        "FROM pending_status ps JOIN tasks t ON t.uid = ps.uid "
        "WHERE ps.downloaded = 1 AND ps.radicale_marked_at IS NULL"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()

    stats = {"total": len(rows), "marked": 0, "not_found": 0, "errors": 0}
    print(f"backfill_icons: {len(rows)} candidatos{' (dry-run)' if dry_run else ''}...")

    # Un solo REPORT del calendario completo, reutilizado para los N
    # candidatos -- mark_downloaded() por defecto refetchea en cada
    # llamada (correcto para el uso normal, uno a uno, desde
    # sync_downloaded_status()), pero repetirlo cientos de veces aquí sería
    # igual de lento que N sincronizaciones completas.
    events = radicale_writer.fetch_release_events()

    for r in rows:
        date_str = r["airsonic_date"] or r["qbittorrent_date"]
        download_date = date.fromisoformat(date_str[:10]) if date_str else date.today()
        try:
            if dry_run:
                found = bool(next(
                    (e for e in events
                     if e["artist_norm"] == radicale_writer.normalize(r["artist"])
                     and e["album_norm"] == radicale_writer.normalize(r["album"])),
                    None,
                ))
            else:
                found = radicale_writer.mark_downloaded(r["artist"], r["album"], download_date, events=events)
        except Exception as e:
            print(f"  error en {r['artist']} - {r['album']}: {e}")
            stats["errors"] += 1
            continue

        if found:
            stats["marked"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE pending_status SET radicale_marked_at = ? WHERE uid = ?",
                    (_now(), r["uid"]),
                )
        else:
            stats["not_found"] += 1

    if not dry_run:
        conn.commit()
    conn.close()
    print(f"backfill_icons: {stats['marked']} marcados, {stats['not_found']} sin VEVENT, "
          f"{stats['errors']} errores (de {stats['total']})")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="backfill completo, ignora el caché de edad")
    parser.add_argument("--backfill-icons", action="store_true",
                         help="marca en Radicale los álbumes ya descargados antes de esta feature (correr una vez)")
    parser.add_argument("--dry-run", action="store_true", help="con --backfill-icons: solo cuenta, no escribe")
    parser.add_argument("--limit", type=int, default=None, help="con --backfill-icons: máximo de candidatos a procesar")
    args = parser.parse_args()

    if args.backfill_icons:
        backfill_radicale_icons(dry_run=args.dry_run, limit=args.limit)
    elif args.all:
        sync_all()
    else:
        sync_tasks()
        sync_downloaded_status(stale_only=True)
        sync_enrichment(stale_only=True)
