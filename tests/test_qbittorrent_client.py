from datetime import date, datetime, timezone
from types import SimpleNamespace

import qbittorrent_client as qc


def _torrent(name, added_on):
    return SimpleNamespace(name=name, added_on=added_on)


def test_search_qbittorrent_match():
    added = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())
    torrents = [
        _torrent("Boards of Canada - Music Has The Right To Children [FLAC]", added),
        _torrent("Some Other Album", added),
    ]
    result = qc.search_qbittorrent(torrents, "Boards of Canada", "Music Has The Right To Children")
    assert result == date(2026, 3, 1)


def test_search_qbittorrent_no_match():
    torrents = [_torrent("Irrelevante", 0)]
    assert qc.search_qbittorrent(torrents, "Nadie", "Nada") is None


def test_search_qbittorrent_earliest_wins():
    older = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    newer = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    torrents = [
        _torrent("Artist - Album (Remaster)", newer),
        _torrent("Artist - Album [FLAC]", older),
    ]
    result = qc.search_qbittorrent(torrents, "Artist", "Album")
    assert result == date(2025, 1, 1)
