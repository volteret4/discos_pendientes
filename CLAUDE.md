# discos_pendientes

## Arquitectura

- `radicale_client.py` — lectura de VTODOs de Radicale por HTTP crudo
  (`REPORT` + XML), sin el paquete `caldav`. Mismo patrón que
  `services/dieta_sonora/estadisticas/albums_log/{airsonic,qbittorrent}_checker.py`.
  **v1 no escribe nada en Radicale** (ver más abajo).
- `airsonic_client.py` / `qbittorrent_client.py` — comprobación "¿ya
  descargado?", adaptados de los mismos checker scripts de arriba.
  `QB_HOST=host.docker.internal` (con `extra_hosts` en docker-compose) es
  obligatorio dentro de un contenedor — `QB_HOST=localhost` del `.env`
  apunta al propio contenedor, no al host donde corre qBittorrent.
- `enrich.py` — `musica_local.sqlite` (biblioteca propia) primero, si el
  álbum no está ahí (lo normal para un pendiente), Last.fm + Wikipedia +
  MusicBrainz en vivo. Adaptado de `services/yt_playlists/enrich_info.py`
  (ahí resolvían canción→álbum; aquí ya se conoce el álbum directamente
  desde el `SUMMARY` del VTODO, así que es más simple). Solo se cachea en
  la tabla `enrichment` el camino "live" — el camino "local_db" es
  instantáneo y se recalcula siempre.
- `db.py` — `discos_pendientes.db` propia (`tasks`/`pending_status`/
  `enrichment`) + conexión de solo lectura a `musica_local.sqlite` con
  `immutable=1` en la URI (mismo problema de bind-mount `:ro` ya
  documentado en `lastfm_explorer`/`yt_playlists`).
- `sync.py` — `sync_tasks()` (barato, solo CalDAV), `sync_downloaded_status()`
  (caro, Airsonic+qBittorrent en vivo) y `sync_enrichment()` se sincronizan
  por separado — patrón "backfill completo + incremental" ya usado en
  `lastfm_explorer`/`yt_playlists`.
- `server.py` — Flask, una plantilla HTML con dos pestañas (Pendientes /
  Catálogo) y panel lateral flotante compartido, mismo patrón visual que
  el resto de servicios del monorepo.

## Por qué v1 no escribe en Radicale

`services/dieta_sonora/estadisticas/albums_log/cal_to_estadisticas.py` ya
usa `VTODO STATUS=COMPLETED` en el mismo calendario (`CALENDAR_TASKS`) para
marcar "escuchado" (vía scrobbles de Last.fm). Si este servicio también
escribiera `STATUS=COMPLETED` para "ya descargado" pisaría esa semántica
ya establecida. El estado "descargado" vive solo en `pending_status`
(DB propia) — si en el futuro se quiere marcar algo en Radicale, habría
que usar una propiedad custom (`X-DESCARGADO`, por ejemplo), nunca
`STATUS`.

## Los dos calendarios de Radicale

`CALENDAR_NAME` y `CALENDAR_TASKS` son calendarios **hermanos** (no uno
anidado en el otro) bajo `RADICALE_URL`. `CALENDAR_NAME` son los `VEVENT`
de lanzamientos (no se usa aquí); `CALENDAR_TASKS` son los `VTODO` de
"discos pendientes" — el que sí se usa.
