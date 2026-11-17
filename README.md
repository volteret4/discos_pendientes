# discos_pendientes

Web para gestionar los discos pendientes de escuchar/comprar: muestra las
tareas del calendario "discos pendientes" de Radicale, comprueba si cada
álbum ya está descargado (Airsonic / qBittorrent) para poder filtrar por
ese estado, y enriquece cada álbum con info de Last.fm, Wikipedia y de la
biblioteca propia (`musica_local.sqlite`, la base de music-fuzzy). También
permite buscar/navegar la colección propia por género, sello, año y
artista. Parte de [repeat-one](https://github.com/volteret4/repeat-one).

## Cómo funciona

- **Pendientes**: lee el calendario de tareas de Radicale (`VTODO`,
  `SUMMARY` en formato "Artista - Álbum") por HTTP crudo (`REPORT`), sin
  el paquete `caldav`. Por cada pendiente, comprueba si ya está en
  Airsonic (`search3`) o en qBittorrent (nombre de torrent), y cachea el
  resultado en una DB propia — no se repite en cada carga de página.
- **Enriquecimiento**: `musica_local.sqlite` primero (instantáneo, sin
  red) — si el álbum aún no está en la biblioteca propia (lo normal para
  un pendiente), Last.fm + Wikipedia + MusicBrainz en vivo, cacheado.
- **Catálogo**: búsqueda/filtro por género, sello, año y artista sobre la
  tabla `albums` de `musica_local.sqlite` (la colección que ya se posee).
- **Marcado en Radicale al descargar**: `sync_release_calendar()` recorre
  **todo** el calendario de lanzamientos ("discos", `RELEASE_CALENDAR_NAME`)
  — no solo los álbumes que llegaron a ser tarea VTODO en la lista de
  pendientes — y a cada `VEVENT` ya descargado (Airsonic/qBittorrent) le
  cambia el icono (💿/📀/🎵 → 💾) y añade la fecha de descarga a su
  `DESCRIPTION`. Un álbum puede descargarse sin haber pasado nunca por la
  lista de pendientes (ej. comprado directamente al salir) y aun así se
  marca. No toca `STATUS` de ningún `VTODO` en `CALENDAR_TASKS`
  (colisionaría con el uso que ya hace `dieta_sonora` de
  `STATUS=COMPLETED` en ese calendario para marcar "escuchado"). Ver
  `CLAUDE.md` para la tabla de los tres calendarios de Radicale
  involucrados (fácil confundirlos por nombre parecido).

## Variables de entorno

```bash
RADICALE_URL=https://radicale.example.org/pollo/
RADICALE_USERNAME=...
RADICALE_PW=...
RADICALE_CALENDAR=            # vacío si los calendarios cuelgan directo de RADICALE_URL
CALENDAR_TASKS=...            # UID del calendario de VTODOs "discos pendientes"
RELEASE_CALENDAR_NAME=...     # UID del calendario de VEVENTs de lanzamientos (el mismo que dieta_sonora
                               # conoce como CALENDAR_NAME -- nombre distinto aquí a propósito, ver CLAUDE.md)
AIRSONIC_URL=...
AIRSONIC_USER=...
AIRSONIC_PASS=...
AIRSONIC_API_VERSION=1.15.0
# Pestañas Sin escuchas / Escuchados y playlists: usuario de escucha de Navidrome.
# NAVIDROME_URL es opcional (por defecto AIRSONIC_URL).
NAVIDROME_USER=music
NAVIDROME_PASS=...
QB_HOST=host.docker.internal  # NO localhost dentro de un contenedor
QB_PORT=8080
QB_USER=...
QB_PASS=...
LASTFM_API_KEY=...
DB_PATH=/app/discos_pendientes.db
MUSICA_LOCAL_DB=/musica_local.sqlite
PORT=5000
```

## Uso

```bash
pip install -r requirements.txt
python3 sync.py --all      # backfill completo (tareas + descargas + enriquecimiento + calendario de lanzamientos)
python3 sync.py            # incremental (job diario en producción, incluye sync_release_calendar)
python3 sync.py --release-calendar --dry-run  # cuenta cuántos VEVENT del calendario "discos" se marcarían
python3 sync.py --release-calendar            # fuerza una pasada completa del calendario "discos" (backfill histórico)
python3 server.py          # web en 0.0.0.0:5000
```

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

Fixtures: XML de CalDAV sintético (VTODOs con emoji, sin `DTSTART`, ya
`COMPLETED`), Airsonic/qBittorrent mockeados, `musica_local.sqlite`
sintética con el directorio en solo lectura (reproduce el bind-mount
`:ro` real de Docker).

## Instalación con Docker Compose

```bash
cp .env.example .env
```

Rellena `.env` (credenciales y carpetas de tu equipo) y arranca:

```bash
docker compose up -d --build
```

Los datos que genera (bases, cachés) se quedan en esta misma carpeta, fuera de git.

## Actualizaciones automáticas

El `docker-compose.yml` trae su propia [Ofelia](https://github.com/mcuadros/ofelia), que lanza dentro del contenedor los mismos trabajos y a las mismas horas que en REPEAT-ONE (formato con segundos: `seg min hora día mes día-semana`, hora de `TZ`, por defecto Europe/Madrid):

| Trabajo | Cuándo | Qué lanza |
|---|---|---|
| `discos-pendientes-daily` | `0 40 0 * * *` | `python3 sync.py` |

Para cambiar una hora, edita su etiqueta `ofelia.job-exec.<trabajo>.schedule` en `docker-compose.yml` y vuelve a hacer `docker compose up -d`. Para lanzarlo a mano: `docker compose exec <servicio> <orden>`. Para no tener actualizaciones automáticas: `docker compose stop ofelia`.

Esta Ofelia solo ve los contenedores de este repo (etiqueta `repeat-one.ofelia`), así que puedes tener varios módulos instalados por separado sin que se pisen. Necesita el socket de Docker en solo lectura.
