import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ani_runner import ResolvedStream, SourceError
from db import init_schema
from history import HistoryStore
from metadata import AnimeMeta, MetadataError
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
    "watch_interval": 2.0,
    "android_player_package": "is.xyz.mpv",
    "data_dir": "data",
}


class StubSource:
    """Zrodlo bez ani-cli — notuje wywolania resolve()."""

    name = "stub"

    def __init__(self, error=None, missing=None, fail_for=None):
        self.error = error
        self.missing = missing or []
        # Tytuly, dla ktorych to zrodlo udaje pudlo — pod testy kolejnych
        # kandydatow w leniwym mapowaniu.
        self.fail_for = set(fail_for or ())
        self.calls = []

    def check(self):
        return list(self.missing)

    def resolve(self, provider_id, episode, quality=None):
        self.calls.append((provider_id, episode, quality))
        if self.error:
            raise self.error
        if provider_id in self.fail_for:
            raise SourceError(f"brak zrodel dla {provider_id!r}")
        return ResolvedStream(
            stream_url="http://cdn.example.test/v.mp4",
            referer="https://youtu-chan.com",
            headers={"Referer": "https://youtu-chan.com"},
        )


class StubMetadata:
    """Katalog bez sieci. Trzyma mapowania w pamieci, tak jak MetadataService w SQLite."""

    def __init__(self, metas=None, error=None):
        self.metas = {m.anilist_id: m for m in (metas or [DEFAULT_META])}
        self.error = error
        self.mappings = {}
        self.searches = []

    def search(self, query, limit=20):
        self.searches.append((query, limit))
        if self.error:
            raise self.error
        return list(self.metas.values())[:limit]

    def details(self, anilist_id):
        if self.error:
            raise self.error
        if int(anilist_id) not in self.metas:
            raise MetadataError(f"Brak anime {anilist_id}")
        return self.metas[int(anilist_id)]

    def cached_mapping(self, anilist_id):
        return self.mappings.get(int(anilist_id))

    def remember_mapping(self, anilist_id, provider_query):
        self.mappings[int(anilist_id)] = provider_query

    def forget_mapping(self, anilist_id):
        self.mappings.pop(int(anilist_id), None)


class PositionPlayer(SimulatedPlayer):
    """Backend, ktory raportuje pozycje — jak mpv-ipc, inaczej niz ani-cli.

    Potrzebny, bo caly zapis pozycji w Stage 3 zalezy od `capabilities.position`.
    """

    name = "pozycyjny"
    capabilities = SimulatedPlayer.capabilities.__class__(
        pause=True, stop=True, position=True, ended_events=True
    )

    def __init__(self, position=0, duration=0):
        super().__init__()
        self.position = position
        self.duration = duration

    def status(self):
        payload = super().status()
        if payload["state"] in {"playing", "paused"}:
            payload["position"] = self.position
            payload["duration"] = self.duration
        return payload


DEFAULT_META = AnimeMeta(
    anilist_id=1,
    romaji="Cowboy Bebop",
    english="Cowboy Bebop",
    synonyms=["Kowboj Bebop"],
    description="Opis testowy.",
    episodes=26,
    year=1998,
    format="TV",
    score=86,
    cover="https://example.test/cover.jpg",
)


class ServerTestCase(unittest.TestCase):
    """Wspolna fabryka: wszystko wstrzykniete, zero sieci i zero pisania w repo."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.settings_path = Path(tmpdir.name) / "settings.json"
        # Historia idzie na prawdziwym SQLite — jest szybka, a atrapa nie
        # sprawdzilaby SQL-a z COALESCE, ktory jest tu cala trudnoscia.
        self.db_path = Path(tmpdir.name) / "test.sqlite3"
        init_schema(self.db_path)

    def build(self, player=None, source=None, metadata=None):
        self.player = player or SimulatedPlayer()
        self.source = source if source is not None else StubSource()
        self.metadata = metadata if metadata is not None else StubMetadata()
        self.history = HistoryStore(self.db_path)
        self.app = create_app(
            config=dict(CONFIG),
            player=self.player,
            source=self.source,
            settings=SettingsStore(self.settings_path),
            metadata=self.metadata,
            history=self.history,
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

    def test_status_sets_the_uid_cookie_once(self):
        # Pierwsze zadanie klienta (status) ma ustalic uid; kolejne go nie zmieniaja.
        client = self.build()
        first = client.get("/api/status")
        self.assertIn(UID_COOKIE, first.headers.get("Set-Cookie", ""))
        second = client.get("/api/status")
        self.assertNotIn("Set-Cookie", second.headers)

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


class SeekTests(ServerTestCase):
    def test_seek_needs_an_integer_delta(self):
        client = self.build()
        self.assertEqual(client.post("/api/seek", json={}).status_code, 400)
        self.assertEqual(client.post("/api/seek", json={"delta": "x"}).status_code, 400)

    def test_seek_on_a_player_without_steps_is_an_error_not_a_crash(self):
        response = self.build().post("/api/seek", json={"delta": 10})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_status_reports_step_capability(self):
        caps = self.build().get("/api/status").get_json()["capabilities"]
        self.assertIn("step", caps)
        self.assertFalse(caps["step"])  # symulacja nie przewija


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

    def test_no_seek_to_position_exists(self):
        # docs.md D2 — nie ma seeka do pozycji. /api/seek przyjmuje tylko krok
        # (`delta`); `position` nic nie znaczy i konczy sie bledem zadania.
        self.assertEqual(self.build().post("/api/seek", json={"position": 30}).status_code, 400)


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

    def test_catalogue_failure_is_its_own_kind(self):
        # Katalog i zrodlo padaja niezaleznie (docs.md D7) — UI musi je rozroznic,
        # bo awaria AniList nie blokuje odtwarzania po tytule wprost.
        client = self.build(metadata=StubMetadata(error=MetadataError("AniList padl")))
        response = client.get("/api/anime?q=cokolwiek")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["kind"], "catalogue")


class CatalogueTests(ServerTestCase):
    def test_search_requires_query(self):
        response = self.build().get("/api/anime")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["kind"], "request")

    def test_search_returns_results_with_covers(self):
        payload = self.build().get("/api/anime?q=cowboy").get_json()
        self.assertEqual(payload["query"], "cowboy")
        first = payload["results"][0]
        self.assertEqual(first["anilist_id"], 1)
        self.assertEqual(first["title"], "Cowboy Bebop")
        self.assertTrue(first["cover"])

    def test_search_limit_is_clamped(self):
        client = self.build()
        client.get("/api/anime?q=x&limit=999")
        self.assertLessEqual(self.metadata.searches[0][1], 50)

    def test_details_include_resume_hint(self):
        payload = self.build().get("/api/anime/1").get_json()
        self.assertEqual(payload["title"], "Cowboy Bebop")
        self.assertEqual(payload["episodes"], 26)
        self.assertEqual(payload["resume"], {"episode": 1, "position": 0, "reason": "nowe"})

    def test_details_of_unknown_anime_is_a_catalogue_error(self):
        response = self.build().get("/api/anime/999")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["kind"], "catalogue")


class LazyMappingTests(ServerTestCase):
    def test_play_by_anilist_id_maps_title_lazily(self):
        client = self.build()
        payload = client.post("/api/play", json={"anilist_id": 1, "episode": 2}).get_json()

        self.assertEqual(payload["state"], "playing")
        self.assertEqual(payload["current"]["title"], "Cowboy Bebop")
        self.assertEqual(payload["current"]["anilist_id"], 1)
        # Do zrodla poszedl tytul, nie id.
        self.assertEqual(self.source.calls, [("Cowboy Bebop", 2, "1080")])
        # Zwyciezca zapamietany, zeby druga proba nie zgadywala od nowa.
        self.assertEqual(self.metadata.cached_mapping(1), "Cowboy Bebop")

    def test_play_falls_through_to_next_candidate(self):
        # Pierwszy kandydat (romaji) pada, drugi ma zadzialac.
        source = StubSource(fail_for={"Cowboy Bebop"})
        client = self.build(source=source)

        payload = client.post("/api/play", json={"anilist_id": 1}).get_json()
        self.assertEqual(payload["state"], "playing")
        self.assertEqual([call[0] for call in source.calls], ["Cowboy Bebop", "Kowboj Bebop"])
        self.assertEqual(self.metadata.cached_mapping(1), "Kowboj Bebop")

    def test_all_candidates_failing_is_a_source_error(self):
        source = StubSource(error=SourceError("brak zrodel"))
        response = self.build(source=source).post("/api/play", json={"anilist_id": 1})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["kind"], "source")

    def test_play_still_accepts_a_bare_title(self):
        client = self.build()
        payload = client.post("/api/play", json={"query": "naruto"}).get_json()
        self.assertEqual(payload["current"]["query"], "naruto")
        self.assertIsNone(payload["current"]["anilist_id"])


class HistoryTests(ServerTestCase):
    def test_history_starts_empty(self):
        self.assertEqual(self.build().get("/api/history").get_json(), {"entries": []})

    def test_play_records_history(self):
        client = self.build()
        client.post("/api/play", json={"anilist_id": 1, "episode": 3})

        entries = client.get("/api/history").get_json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["anilist_id"], 1)
        self.assertEqual(entries[0]["episode"], 3)
        self.assertEqual(entries[0]["title"], "Cowboy Bebop")

    def test_playing_by_bare_title_records_nothing(self):
        # Bez anilist_id nie mamy czego wpisac do historii katalogowej.
        client = self.build()
        client.post("/api/play", json={"query": "naruto"})
        self.assertEqual(client.get("/api/history").get_json()["entries"], [])

    def test_stop_saves_position_when_player_reports_it(self):
        client = self.build(player=PositionPlayer(position=640, duration=1400))
        client.post("/api/play", json={"anilist_id": 1, "episode": 1})
        client.post("/api/stop")

        entry = client.get("/api/history/1").get_json()["entry"]
        self.assertEqual(entry["position"], 640)
        self.assertEqual(entry["duration"], 1400)

    def test_stop_saves_nothing_when_player_has_no_position(self):
        # SimulatedPlayer nie raportuje pozycji — historia zostaje przy odcinku.
        client = self.build()
        client.post("/api/play", json={"anilist_id": 1, "episode": 1})
        client.post("/api/stop")
        self.assertIsNone(client.get("/api/history/1").get_json()["entry"]["position"])

    def test_resume_offers_saved_position(self):
        client = self.build(player=PositionPlayer(position=640, duration=1400))
        client.post("/api/play", json={"anilist_id": 1, "episode": 1})
        client.post("/api/stop")

        resume = client.get("/api/anime/1").get_json()["resume"]
        self.assertEqual(resume, {"episode": 1, "position": 640, "reason": "wznowienie"})

    def test_changing_episode_clears_stale_position(self):
        # Bez tego wznowienie odcinka 2 skakaloby do czasu z odcinka 1.
        client = self.build(player=PositionPlayer(position=640, duration=1400))
        client.post("/api/play", json={"anilist_id": 1, "episode": 1})
        client.post("/api/stop")
        client.post("/api/play", json={"anilist_id": 1, "episode": 2})

        entry = client.get("/api/history/1").get_json()["entry"]
        self.assertEqual(entry["episode"], 2)
        self.assertIsNone(entry["position"])

    def test_finished_episode_resumes_at_the_next_one(self):
        client = self.build(player=PositionPlayer(position=1390, duration=1400))
        client.post("/api/play", json={"anilist_id": 1, "episode": 5})
        client.post("/api/stop")

        resume = client.get("/api/anime/1").get_json()["resume"]
        self.assertEqual(resume["episode"], 6)
        self.assertEqual(resume["reason"], "nastepny odcinek")

    def test_history_entry_reports_resume(self):
        client = self.build()
        client.post("/api/play", json={"anilist_id": 1, "episode": 4})
        payload = client.get("/api/history/1").get_json()
        self.assertEqual(payload["entry"]["episode"], 4)
        self.assertEqual(payload["resume"]["episode"], 4)

    def test_delete_removes_entry(self):
        client = self.build()
        client.post("/api/play", json={"anilist_id": 1})
        self.assertEqual(client.delete("/api/history/1").get_json(), {"deleted": True})
        self.assertEqual(client.get("/api/history").get_json()["entries"], [])

    def test_deleting_missing_entry_is_404(self):
        self.assertEqual(self.build().delete("/api/history/424242").status_code, 404)

    def test_history_is_per_uid(self):
        first = self.build()
        first.post("/api/play", json={"anilist_id": 1})
        second = self.app.test_client()
        self.assertEqual(second.get("/api/history").get_json()["entries"], [])


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
