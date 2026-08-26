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
from datetime import datetime, timedelta, timezone

import radicale_client
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


def sync_all() -> None:
    sync_tasks()
    sync_downloaded_status(stale_only=False)
    sync_enrichment(stale_only=False)
    print("Hecho.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="backfill completo, ignora el caché de edad")
    args = parser.parse_args()

    if args.all:
        sync_all()
    else:
        sync_tasks()
        sync_downloaded_status(stale_only=True)
        sync_enrichment(stale_only=True)
