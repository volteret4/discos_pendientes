# discos_pendientes

## Arquitectura

- `radicale_client.py` — lectura de VTODOs de Radicale por HTTP crudo
  (`REPORT` + XML), sin el paquete `caldav`. Mismo patrón que
  `services/dieta_sonora/estadisticas/albums_log/{airsonic,qbittorrent}_checker.py`.
- `radicale_writer.py` — **v2 sí escribe en Radicale**, pero solo
  `SUMMARY`/`DESCRIPTION` de `VEVENT` en `RELEASE_CALENDAR_NAME` (el
  calendario de lanzamientos, hermano de `CALENDAR_TASKS` -- el mismo
  calendario que `dieta_sonora` conoce como `CALENDAR_NAME`; se usa un
  nombre de variable distinto aquí porque el `.env` raíz del monorepo ya
  reutiliza el nombre "CALENDAR_NAME" para un calendario *diferente* en
  otras secciones -- colisión preexistente, no introducida por esta
  feature, que causó que el primer despliegue resolviera al calendario
  equivocado hasta renombrar la variable), nunca `STATUS` de ningún
  `VTODO` (ver más abajo por qué). Al detectar una descarga
  (`sync_downloaded_status()` en `sync.py`), busca el `VEVENT` del
  lanzamiento por `(artist_norm, album_norm)` y le cambia el icono
  (💿/📀/🎵 → 💾) + añade `💾 Descargado: YYYY-MM-DD` a su `DESCRIPTION`.
  PUT idempotente por `href` (sin `If-Match`/ETag — mismo patrón simple
  que `services/bot_conciertos/apis/radicale.py::push_ics_event()`).
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

## Por qué nunca se toca STATUS de VTODO

`services/dieta_sonora/estadisticas/albums_log/cal_to_estadisticas.py` ya
usa `VTODO STATUS=COMPLETED` en `CALENDAR_TASKS` para marcar "escuchado"
(vía scrobbles de Last.fm). Si este servicio también escribiera
`STATUS=COMPLETED` para "ya descargado" pisaría esa semántica ya
establecida. El estado "descargado" sigue viviendo en `pending_status`
(DB propia, columna `radicale_marked_at` para no re-escribir el mismo
VEVENT en cada sync) — lo único que se escribe en Radicale es
`SUMMARY`/`DESCRIPTION` del `VEVENT` de lanzamiento en el *otro*
calendario (`CALENDAR_NAME`), no el `VTODO`.

## El icono de "descargado" (radicale_writer.py)

No es un campo especial de iCal (no hay `CATEGORIES`/`COLOR` en uso en
estos datos) — es un emoji literal al principio del `SUMMARY` del
`VEVENT`, ej. `SUMMARY:💿 Cypress Hill - Dios Bendiga`. Ese convenio
(💿 Album / 📀 EP / 🎵 Single, visto en
`services/bot_conciertos/handlers/artist_handlers.py`) ya existía en los
datos de producción antes de esta feature — el script original que creó
esos VEVENT con icono no está en ningún repo conocido (no hace falta
encontrarlo). `radicale_writer._replace_icon()` reutiliza
`radicale_client._strip_emojis()` para quitar cualquier icono existente
y anteponer `💾`, así que funciona igual haya o no un icono previo.

**Solo el ~36% de los pendientes ya descargados tienen un VEVENT
correspondiente** (cruzando por artista+álbum normalizado) — el resto
son tareas sin lanzamiento rastreado en Muspy/`CALENDAR_NAME`.
`mark_downloaded()` devuelve `False` en ese caso (no es un error, no se
loguea como fallo).

## Backfill (`sync.py --backfill-icons`)

Migración de datos histórica para lo que ya estaba marcado como
descargado antes de que existiera esta feature — no se ejecuta en el
cron diario. Usa `--dry-run` primero para ver cuántos candidatos
matchean sin escribir nada. Usa la fecha real de descarga
(`airsonic_date`/`qbittorrent_date` ya guardadas en `pending_status`),
no la fecha en que se corre el backfill.

## Los dos calendarios de Radicale

`CALENDAR_NAME` y `CALENDAR_TASKS` son calendarios **hermanos** (no uno
anidado en el otro) bajo `RADICALE_URL`. `CALENDAR_NAME` son los `VEVENT`
de lanzamientos (no se usa aquí); `CALENDAR_TASKS` son los `VTODO` de
"discos pendientes" — el que sí se usa.
