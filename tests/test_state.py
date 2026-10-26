import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ani_runner import ResolvedStream, SourceError
from players import PlayerError, SimulatedPlayer
from state import AppState, SettingsStore


class RecordingPlayer(SimulatedPlayer):
    """SimulatedPlayer, ktory zapamietuje z czym zostal wywolany."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def play(self, stream_url, headers=None, position=0, title="", subtitles="", audio="", skip=None):
        self.calls.append(
            {"stream_url": stream_url, "headers": headers, "position": position, "title": title,
             "skip": skip}
        )
        return super().play(stream_url, headers, position, title)


class StubSource:
    name = "stub"

    def __init__(self, error=None):
        self.error = error

    def check(self):
        return []

    def resolve(self, provider_id, episode, quality=None):
        if self.error:
            raise self.error
        return ResolvedStream(
            stream_url="http://cdn.example.test/v.mp4",
            referer="https://youtu-chan.com",
            headers={"Referer": "https://youtu-chan.com"},
        )


class AppStateTests(unittest.TestCase):
    def setUp(self):
        self.player = RecordingPlayer()
        self.state = AppState(self.player, StubSource())

    def test_snapshot_merges_player_description_and_status(self):
        snapshot = self.state.as_dict()
        self.assertEqual(snapshot["player"], "simulated")
        self.assertEqual(snapshot["state"], "idle")
        self.assertIn("capabilities", snapshot)
        self.assertIsNone(snapshot["current"])
        self.assertIsNone(snapshot["last_error"])

    def test_play_forwards_url_headers_and_position_to_player(self):
        self.state.play("one piece", episode=3, quality="720", position=90)
        call = self.player.calls[0]
        self.assertEqual(call["stream_url"], "http://cdn.example.test/v.mp4")
        self.assertEqual(call["headers"], {"Referer": "https://youtu-chan.com"})
        self.assertEqual(call["position"], 90)
        self.assertIn("odc. 3", call["title"])

    def test_skip_times_are_fetched_only_on_request_and_with_mal_id(self):
        from types import SimpleNamespace
        import state as state_module

        meta = SimpleNamespace(
            anilist_id=20, mal_id=20, title="NARUTO", cover="", episodes=220
        )

        class Mapper:
            def resolve(self, anilist_id, episode=1, quality=None):
                return StubSource().resolve("x", episode), meta

        fetched = []

        def fake_fetch(mal_id, episode):
            fetched.append((mal_id, episode))
            return {"op": (1.0, 102.0)}

        original = state_module.fetch_skip_times
        state_module.fetch_skip_times = fake_fetch
        try:
            st = AppState(self.player, StubSource(), mapper=Mapper())
            st.play(anilist_id=20, episode=4)  # bez zyczenia: brak zapytania
            self.assertEqual(fetched, [])
            self.assertEqual(self.player.calls[-1]["skip"], {})

            snapshot = st.play(anilist_id=20, episode=4, skip_intro=True)
            self.assertEqual(fetched, [(20, 4)])
            self.assertEqual(self.player.calls[-1]["skip"], {"op": (1.0, 102.0)})
            self.assertEqual(snapshot["current"]["skip"], {"op": [1.0, 102.0]})

            meta.mal_id = 0  # seria bez MAL ID: nie pytamy w ogole
            st.play(anilist_id=20, episode=5, skip_intro=True)
            self.assertEqual(fetched, [(20, 4)])
        finally:
            state_module.fetch_skip_times = original

    def test_seek_is_refused_when_player_has_no_step_capability(self):
        with self.assertRaises(PlayerError):
            self.state.seek(10)

    def test_seek_is_forwarded_and_published(self):
        class Stepper(RecordingPlayer):
            from players import Capabilities
            capabilities = Capabilities(pause=True, stop=True, step=True)
            steps = []

            def seek(self, delta):
                self.steps.append(delta)

        player = Stepper()
        state = AppState(player, StubSource())
        q = state.subscribe()
        state.seek(-60)
        self.assertEqual(player.steps, [-60])
        self.assertFalse(q.empty())

    def test_play_without_source_is_a_source_error(self):
        state = AppState(RecordingPlayer(), source=None)
        with self.assertRaises(SourceError):
            state.play("x")

    def test_source_failure_is_recorded_and_reraised(self):
        state = AppState(RecordingPlayer(), StubSource(error=SourceError("padlo")))
        with self.assertRaises(SourceError):
            state.play("x")
        self.assertEqual(state.as_dict()["last_error"], "padlo")

    def test_failed_play_does_not_reach_the_player(self):
        player = RecordingPlayer()
        state = AppState(player, StubSource(error=SourceError("padlo")))
        with self.assertRaises(SourceError):
            state.play("x")
        self.assertEqual(player.calls, [])

    def test_successful_play_clears_previous_error(self):
        self.state.last_error = "stare"
        self.state.play("x")
        self.assertIsNone(self.state.as_dict()["last_error"])

    def test_stop_clears_current(self):
        self.state.play("x")
        self.assertIsNotNone(self.state.current)
        self.state.stop()
        self.assertIsNone(self.state.current)

    def test_pause_is_refused_when_backend_lacks_capability(self):
        player = RecordingPlayer()
        player.capabilities = player.capabilities.__class__(pause=False)
        state = AppState(player, StubSource())
        with self.assertRaises(PlayerError):
            state.pause()


class PubSubTests(unittest.TestCase):
    def setUp(self):
        self.state = AppState(RecordingPlayer(), StubSource())

    def test_subscriber_receives_snapshot_on_play(self):
        q = self.state.subscribe()
        self.state.play("one piece", episode=2)
        snapshot = q.get_nowait()
        self.assertEqual(snapshot["state"], "playing")
        self.assertEqual(snapshot["current"]["episode"], 2)

    def test_every_subscriber_gets_its_own_copy(self):
        first, second = self.state.subscribe(), self.state.subscribe()
        self.state.play("x")
        self.assertEqual(first.get_nowait()["state"], second.get_nowait()["state"])

    def test_unsubscribe_stops_delivery(self):
        q = self.state.subscribe()
        self.state.unsubscribe(q)
        self.state.play("x")
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_unsubscribing_twice_is_harmless(self):
        q = self.state.subscribe()
        self.state.unsubscribe(q)
        self.state.unsubscribe(q)

    def test_slow_subscriber_loses_frames_instead_of_blocking(self):
        # Kluczowe dla SSE: jeden zamrozony telefon nie moze zatrzymac backendu.
        q = self.state.subscribe()
        for _ in range(q.maxsize + 5):
            self.state.publish()
        self.assertEqual(q.qsize(), q.maxsize)

    def test_error_path_also_publishes(self):
        state = AppState(RecordingPlayer(), StubSource(error=SourceError("padlo")))
        q = state.subscribe()
        with self.assertRaises(SourceError):
            state.play("x")
        self.assertEqual(q.get_nowait()["last_error"], "padlo")


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / "settings.json"
        self.store = SettingsStore(self.path)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(self.store.get("uid-1"), {})

    def test_update_then_get(self):
        self.store.update("uid-1", {"quality": "720"})
        self.assertEqual(self.store.get("uid-1"), {"quality": "720"})

    def test_update_merges_keys(self):
        self.store.update("uid-1", {"quality": "720"})
        merged = self.store.update("uid-1", {"episode": 4})
        self.assertEqual(merged, {"quality": "720", "episode": 4})

    def test_users_are_isolated(self):
        self.store.update("uid-1", {"quality": "480"})
        self.assertEqual(self.store.get("uid-2"), {})

    def test_corrupt_file_does_not_raise(self):
        self.path.write_text("nie-json", encoding="utf-8")
        self.assertEqual(self.store.get("uid-1"), {})

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.store.update("uid-1", {"quality": "1080"})
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"uid-1": {"quality": "1080"}})

    def test_polish_characters_survive_the_round_trip(self):
        self.store.update("uid-1", {"tytul": "Zażółć gęślą jaźń"})
        self.assertEqual(self.store.get("uid-1")["tytul"], "Zażółć gęślą jaźń")


if __name__ == "__main__":
    unittest.main()
