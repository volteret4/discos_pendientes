# discos_pendientes

## Arquitectura

- `radicale_client.py` — lectura de VTODOs de Radicale por HTTP crudo
  (`REPORT` + XML), sin el paquete `caldav`. Mismo patrón que
  `services/dieta_sonora/estadisticas/albums_log/{airsonic,qbittorrent}_checker.py`.
- `radicale_writer.py` — **v2 sí escribe en Radicale**, pero solo
  `SUMMARY`/`DESCRIPTION` de `VEVENT` en `RELEASE_CALENDAR_NAME` (el
  calendario **"discos"**, hermano de `CALENDAR_TASKS` -- el mismo
  calendario que `dieta_sonora` conoce como `CALENDAR_NAME`; se usa un
  nombre de variable distinto aquí porque el `.env` raíz del monorepo ya
  reutiliza el nombre "CALENDAR_NAME" para un calendario *diferente*
  -- **"Discos descargados"**, ver más abajo -- en otras secciones,
  colisión preexistente que causó que el primer despliegue resolviera al
  calendario equivocado hasta renombrar la variable), nunca `STATUS` de
  ningún `VTODO` (ver más abajo por qué). `mark_downloaded(artist, album,
  download_date, events=None)` busca el `VEVENT` del lanzamiento por
  `(artist_norm, album_norm)` y le cambia el icono (💿/📀/🎵 → 💾) + añade
  `💾 Descargado: YYYY-MM-DD` a su `DESCRIPTION`. `events` opcional para
  reutilizar un `fetch_release_events()` ya hecho en vez de repetir el
  `REPORT` completo del calendario por cada llamada (ver
  `sync_release_calendar()` más abajo). PUT idempotente por `href` (sin
  `If-Match`/ETag — mismo patrón simple que
  `services/bot_conciertos/apis/radicale.py::push_ics_event()`).
  **Cuidado con la URL**: `RADICALE_URL` ya incluye el path base (ej.
  ".../pollo") y el `href` que devuelve el `REPORT` es ya una ruta
  absoluta que también empieza por ese mismo path -- concatenarlos tal
  cual duplica el prefijo (bug real visto en producción: 403). Se usa
  solo el origin (scheme+host) de `RADICALE_URL` + `href`.
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
  `release_marks`/`enrichment`) + conexión de solo lectura a
  `musica_local.sqlite` con `immutable=1` en la URI (mismo problema de
  bind-mount `:ro` ya documentado en `lastfm_explorer`/`yt_playlists`).
- `sync.py` — `sync_tasks()` (barato, solo CalDAV), `sync_downloaded_status()`
  (caro, Airsonic+qBittorrent en vivo), `sync_enrichment()` y
  `sync_release_calendar()` se sincronizan por separado — patrón
  "backfill completo + incremental" ya usado en
  `lastfm_explorer`/`yt_playlists`.
- `server.py` — Flask, una plantilla HTML con dos pestañas (Pendientes /
  Catálogo) y panel lateral flotante compartido, mismo patrón visual que
  el resto de servicios del monorepo.

## Por qué nunca se toca STATUS de VTODO

`services/dieta_sonora/estadisticas/albums_log/cal_to_estadisticas.py` ya
usa `VTODO STATUS=COMPLETED` en `CALENDAR_TASKS` para marcar "escuchado"
(vía scrobbles de Last.fm). Si este servicio también escribiera
`STATUS=COMPLETED` para "ya descargado" pisaría esa semántica ya
establecida. Lo único que se escribe en Radicale es `SUMMARY`/
`DESCRIPTION` del `VEVENT` de lanzamiento en el calendario "discos"
(`RELEASE_CALENDAR_NAME`), nunca el `VTODO` de `CALENDAR_TASKS`.

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

## sync_release_calendar(): por qué no basta con la lista de pendientes

Primer diseño (equivocado): marcar el icono solo cuando un álbum de la
lista de "discos_pendientes" (`tasks`/`pending_status`, `CALENDAR_TASKS`)
pasaba a estar descargado. **Un álbum puede descargarse sin haber pasado
nunca por esa lista** — ej. Bonobo/FKJ, comprados directamente al salir,
nunca llegaron a ser tarea VTODO — así que ese diseño se quedaba corto:
la mayoría de descargas reales del calendario "discos" no se marcaban.

`sync_release_calendar()` corrige esto recorriendo directamente **todo**
el calendario "discos" (vía `radicale_writer.fetch_release_events()`,
669 VEVENT en producción) y comprobando cada uno contra Airsonic/
qBittorrent, sin pasar por `tasks`/`pending_status` en absoluto. Tiene su
propia tabla, `release_marks` (indexada por UID de VEVENT, no por
artista/álbum), para no repetir el chequeo de Airsonic contra el mismo
VEVENT en cada sync una vez confirmado que sigue sin descargar
(`stale_only=True`, mismo patrón de 24h que `sync_downloaded_status()`)
o una vez ya marcado (`radicale_marked_at IS NOT NULL` → se salta sin
comprobar nada).

**Solo ~126 de 669 lanzamientos del calendario están descargados** en
producción a fecha de esta feature — la mayoría son lanzamientos futuros
o que aún no se han comprado. Normal, no es un fallo.

**Eficiencia**: `fetch_release_events()` (un `REPORT` completo del
calendario) se hace **una vez** por ejecución de `sync_release_calendar()`
y se reutiliza para los cientos de comprobaciones — pasarlo por `events=`
a `mark_downloaded()` evita repetir el `REPORT` por cada álbum candidato
(bug de rendimiento real: la primera versión sin este cache tardaba
~10 minutos en comprobar 348 candidatos; con el cache, ~1.5s).

`sync.py --release-calendar [--dry-run]` fuerza una pasada completa
(`stale_only=False`) a mano — usar `--dry-run` primero para ver cuántos
se marcarían sin escribir nada. El cron diario ya incluye
`sync_release_calendar(stale_only=True)` en la pasada incremental
normal, así que no hace falta correrlo a mano salvo la primera vez
(backfill histórico) o para forzar un recheck completo.

## Los tres calendarios de Radicale relevantes

Bajo el mismo `RADICALE_URL`, tres calendarios **hermanos** (no uno
anidado en el otro) que es fácil confundir por nombre parecido:

| Variable                | Displayname en Radicale | UID (pepecono)                        | Uso aquí |
|--------------------------|--------------------------|-----------------------------------------|----------|
| `RELEASE_CALENDAR_NAME`  | **discos**               | `d1573ec1-e837-6918-1dfe-bc0b6c04681d` | VEVENT de lanzamientos — el que lleva el icono 💿/📀/🎵→💾. El mismo que `dieta_sonora` conoce como `CALENDAR_NAME`. |
| `CALENDAR_TASKS`         | discos_pendientes        | `00169e81-e5f4-d26a-d1c9-23a3dad5ea5b` | VTODO de la lista de pendientes — el que sí se usa para `tasks`/`pending_status`. |
| *(no usado aquí)*        | Discos descargados       | `982339b6-2686-86aa-068c-d6dcdb8f712c` | Calendario **distinto**, usado por otra parte de `dieta_sonora` — **no** confundir con "discos". Es la fuente de la colisión de nombre de variable "CALENDAR_NAME" en el `.env` raíz (ver arriba). |

Si en el futuro algo deja de funcionar y "el icono no cambia" o "cambia
en el calendario equivocado", lo primero es verificar con un `PROPFIND`
a `RADICALE_URL/` (displayname de cada UID) qué calendario es cuál —
los tres tienen nombres fácilmente confundibles entre sí.
