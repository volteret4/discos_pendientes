from datetime import date

import airsonic_client as ac


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_search_airsonic_match(monkeypatch):
    monkeypatch.setattr(ac, "AIRSONIC_URL", "http://airsonic.example")
    monkeypatch.setattr(ac, "AIRSONIC_USER", "admin")

    payload = {
        "subsonic-response": {
            "status": "ok",
            "searchResult3": {
                "album": [
                    {"artist": "Björk", "name": "Homogenic", "created": "2020-05-01T00:00:00.000Z"},
                    {"artist": "Björk", "name": "Homogenic (Remaster)", "created": "2022-01-01T00:00:00.000Z"},
                ]
            },
        }
    }

    def fake_get(url, params, timeout):
        return _FakeResp(payload)

    monkeypatch.setattr(ac.requests, "get", fake_get)

    result = ac.search_airsonic("Bjork", "Homogenic")
    assert result == date(2020, 5, 1)


def test_search_airsonic_no_match(monkeypatch):
    monkeypatch.setattr(ac, "AIRSONIC_URL", "http://airsonic.example")
    monkeypatch.setattr(ac, "AIRSONIC_USER", "admin")

    payload = {"subsonic-response": {"status": "ok", "searchResult3": {}}}

    def fake_get(url, params, timeout):
        return _FakeResp(payload)

    monkeypatch.setattr(ac.requests, "get", fake_get)

    assert ac.search_airsonic("Nadie", "Nada") is None


def test_search_airsonic_no_config(monkeypatch):
    monkeypatch.setattr(ac, "AIRSONIC_URL", "")
    monkeypatch.setattr(ac, "AIRSONIC_USER", "")
    assert ac.search_airsonic("x", "y") is None
