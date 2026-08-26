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

No escribe nada en Radicale (ver `CLAUDE.md` — colisionaría con el uso
que ya hace `dieta_sonora` de `STATUS=COMPLETED` en el mismo calendario
para marcar "escuchado").

## Variables de entorno

```bash
RADICALE_URL=https://radicale.example.org/pollo/
RADICALE_USERNAME=...
RADICALE_PW=...
RADICALE_CALENDAR=            # vacío si los calendarios cuelgan directo de RADICALE_URL
CALENDAR_TASKS=...            # UID del calendario de VTODOs "discos pendientes"
AIRSONIC_URL=...
AIRSONIC_USER=...
AIRSONIC_PASS=...
AIRSONIC_API_VERSION=1.15.0
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
python3 sync.py --all      # backfill completo (tareas + descargas + enriquecimiento)
python3 sync.py            # incremental (job diario en producción)
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
