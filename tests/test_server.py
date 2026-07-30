import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ani_runner import ResolvedStream, SourceError
from players import AniCliPlayer, SimulatedPlayer
from server import UID_COOKIE, create_app
from state import SettingsStore

CONFIG = {
    "host": "127.0.0.1",
    "port": 8000,
    "player": "simulated",
    "quality": "1080",
    "ani_cli_command": "ani-cli",
    "mpv_command": "mpv",
    "mpv_ipc_socket": "",
    "adb_command": "adb",
    "android_player_package": "is.xyz.mpv",
    "data_dir": "data",
}


class StubSource:
    """Zrodlo bez ani-cli — notuje wywolania resolve()."""

    name = "stub"

    def __init__(self, error=None, missing=None):
        self.error = error
        self.missing = missing or []
        self.calls = []

    def check(self):
        return list(self.missing)

    def resolve(self, provider_id, episode, quality=None):
        self.calls.append((provider_id, episode, quality))
        if self.error:
            raise self.error
        return ResolvedStream(
            stream_url="http://cdn.example.test/v.mp4",
            referer="https://youtu-chan.com",
            headers={"Referer": "https://youtu-chan.com"},
        )


class ServerTestCase(unittest.TestCase):
    """Wspolna fabryka: wszystko wstrzykniete, zero sieci i zero pisania w repo."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.settings_path = Path(tmpdir.name) / "settings.json"

    def build(self, player=None, source=None):
        self.player = player or SimulatedPlayer()
        self.source = source if source is not None else StubSource()
        self.app = create_app(
            config=dict(CONFIG),
            player=self.player,
            source=self.source,
            settings=SettingsStore(self.settings_path),
        )
        return self.app.test_client()

    @property
    def state(self):
        return self.app.config["ANI_WEB"]["state"]


class StatusTests(ServerTestCase):
    def test_status_exposes_state_player_and_capabilities(self):
        response = self.build().get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["state"], "idle")
        self.assertEqual(payload["player"], "simulated")
        self.assertTrue(payload["capabilities"]["pause"])
        self.assertFalse(payload["capabilities"]["seek"])

    def test_status_reports_missing_source_dependencies(self):
        client = self.build(source=StubSource(missing=["botan"]))
        payload = client.get("/api/status").get_json()
        self.assertEqual(payload["source"]["name"], "stub")
        self.assertEqual(payload["source"]["missing"], ["botan"])


class FrontendTests(ServerTestCase):
    def test_index_is_served(self):
        response = self.build().get("/")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ani-Web", response.get_data(as_text=True))

    def test_assets_are_served(self):
        client = self.build()
        for path in ("/styles.css", "/app.js"):
            with self.subTest(path=path):
                response = client.get(path)
                self.addCleanup(response.close)
                self.assertEqual(response.status_code, 200)

    def test_unknown_asset_is_404_not_a_crash(self):
        self.assertEqual(self.build().get("/nie-ma.js").status_code, 404)

    def test_unknown_api_endpoint_answers_with_json_404(self):
        response = self.build().get("/api/nie-ma")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["kind"], "request")

    def test_api_routes_win_over_the_static_catch_all(self):
        # /<path:filename> nie moze przechwytywac /api/*.
        self.assertEqual(self.build().get("/api/status").status_code, 200)


class PlaybackTests(ServerTestCase):
    def test_play_requires_query(self):
        response = self.build().post("/api/play", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["kind"], "request")

    def test_blank_query_is_also_rejected(self):
        self.assertEqual(self.build().post("/api/play", json={"query": "   "}).status_code, 400)

    def test_play_resolves_then_starts_and_returns_state(self):
        client = self.build()
        payload = client.post("/api/play", json={"query": "one piece", "episode": 5}).get_json()

        self.assertEqual(payload["state"], "playing")
        self.assertEqual(payload["current"]["query"], "one piece")
        self.assertEqual(payload["current"]["episode"], 5)
        self.assertEqual(payload["current"]["stream_url"], "http://cdn.example.test/v.mp4")
        # Adres MUSI byc rozwiazany tuz przed odtwarzaniem — jest krotko zywy.
        self.assertEqual(self.source.calls, [("one piece", 5, "1080")])

    def test_play_defaults_episode_and_quality_from_config(self):
        client = self.build()
        client.post("/api/play", json={"query": "naruto"})
        self.assertEqual(self.source.calls, [("naruto", 1, "1080")])

    def test_play_honours_explicit_quality(self):
        client = self.build()
        client.post("/api/play", json={"query": "naruto", "quality": "720"})
        self.assertEqual(self.source.calls[0][2], "720")

    def test_non_numeric_episode_is_a_request_error(self):
        response = self.build().post("/api/play", json={"query": "x", "episode": "piec"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["kind"], "request")

    def test_pause_toggles_when_supported(self):
        client = self.build()
        client.post("/api/play", json={"query": "x"})
        self.assertEqual(client.post("/api/pause").get_json()["state"], "paused")
        self.assertEqual(client.post("/api/pause").get_json()["state"], "playing")

    def test_pause_is_409_when_backend_cannot_pause(self):
        # ani-cli nie potrafi pauzy — API musi to powiedziec, nie udawac.
        client = self.build(player=AniCliPlayer(ani_cli_command="/nie/ma/ani-cli"))
        response = client.post("/api/pause")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["kind"], "player")

    def test_stop_clears_current(self):
        client = self.build()
        client.post("/api/play", json={"query": "x"})
        payload = client.post("/api/stop").get_json()
        self.assertEqual(payload["state"], "stopped")
        self.assertIsNone(payload["current"])

    def test_no_seek_endpoint_exists(self):
        # docs.md D2 — swiadomie nie ma seeka. Test pilnuje, zeby nie wrocil.
        self.assertEqual(self.build().post("/api/seek", json={"position": 30}).status_code, 404)


class ErrorMappingTests(ServerTestCase):
    def test_source_error_becomes_502_with_readable_message(self):
        client = self.build(source=StubSource(error=SourceError("Zrodlo niedostepne")))
        response = client.post("/api/play", json={"query": "x"})
        self.assertEqual(response.status_code, 502)
        payload = response.get_json()
        self.assertEqual(payload["kind"], "source")
        self.assertIn("Zrodlo niedostepne", payload["error"])

    def test_source_error_is_remembered_in_status(self):
        client = self.build(source=StubSource(error=SourceError("padlo")))
        client.post("/api/play", json={"query": "x"})
        self.assertEqual(client.get("/api/status").get_json()["last_error"], "padlo")

    def test_catalogue_is_honest_about_not_existing_yet(self):
        response = self.build().get("/api/anime")
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["kind"], "todo")


class SettingsTests(ServerTestCase):
    def test_settings_start_empty_and_issue_a_uid_cookie(self):
        client = self.build()
        response = client.get("/api/settings")
        self.assertEqual(response.get_json(), {})
        self.assertIn(UID_COOKIE, response.headers.get("Set-Cookie", ""))

    def test_uid_cookie_is_not_reissued_on_every_request(self):
        client = self.build()
        client.get("/api/settings")
        second = client.get("/api/settings")
        self.assertNotIn("Set-Cookie", second.headers)

    def test_settings_round_trip(self):
        client = self.build()
        saved = client.post("/api/settings", json={"quality": "720"}).get_json()
        self.assertEqual(saved["quality"], "720")
        self.assertEqual(client.get("/api/settings").get_json()["quality"], "720")

    def test_settings_merge_instead_of_replacing(self):
        client = self.build()
        client.post("/api/settings", json={"quality": "720"})
        merged = client.post("/api/settings", json={"episode": 3}).get_json()
        self.assertEqual(merged, {"quality": "720", "episode": 3})

    def test_settings_are_per_uid(self):
        first = self.build()
        first.post("/api/settings", json={"quality": "480"})
        # Nowy klient = brak ciasteczka = inny uzytkownik, ten sam plik na dysku.
        second = self.app.test_client()
        self.assertEqual(second.get("/api/settings").get_json(), {})

    def test_settings_reject_non_object_payload(self):
        response = self.build().post(
            "/api/settings", data=json.dumps([1, 2]), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_corrupt_settings_file_does_not_break_the_endpoint(self):
        self.settings_path.write_text("{ to nie jest json", encoding="utf-8")
        self.assertEqual(self.build().get("/api/settings").get_json(), {})


class EventStreamTests(ServerTestCase):
    @staticmethod
    def read_frame(response):
        for chunk in response.response:
            text = chunk.decode("utf-8")
            if text.startswith("data: "):
                return json.loads(text[len("data: ") :])
        raise AssertionError("strumien nie oddal ramki danych")

    def test_first_frame_carries_current_state_immediately(self):
        response = self.build().get("/api/events")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")

        try:
            payload = self.read_frame(response)
        finally:
            response.close()

        self.assertEqual(payload["state"], "idle")
        self.assertEqual(payload["player"], "simulated")
        self.assertIn("capabilities", payload)

    def test_disconnect_unsubscribes_so_publish_has_no_dead_queues(self):
        client = self.build()
        response = client.get("/api/events")
        self.read_frame(response)
        self.assertEqual(len(self.state._subscribers), 1)

        response.close()
        self.assertEqual(self.state._subscribers, [])


if __name__ == "__main__":
    unittest.main()
