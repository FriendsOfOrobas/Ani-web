import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ani_runner import ResolvedStream
from players import Capabilities, SimulatedPlayer
from state import AppState
from watcher import StateWatcher


class ScriptedPlayer(SimulatedPlayer):
    """Odtwarzacz o z gory zadanej sekwencji stanow — udaje uplyw czasu."""

    name = "scripted"
    capabilities = Capabilities(pause=True, stop=True, position=True, ended_events=True)

    def __init__(self, sequence):
        super().__init__()
        self.sequence = list(sequence)
        self.last = self.sequence[0] if self.sequence else {"state": "idle"}

    def status(self):
        if self.sequence:
            self.last = self.sequence.pop(0)
        return dict(self.last)


class StubSource:
    name = "stub"

    def check(self):
        return []

    def resolve(self, provider_id, episode, quality=None):
        return ResolvedStream(stream_url="http://cdn.test/v.mp4")


def make_state(sequence):
    return AppState(ScriptedPlayer(sequence), StubSource())


class TickTests(unittest.TestCase):
    def test_first_tick_only_establishes_a_baseline(self):
        watcher = StateWatcher(make_state([{"state": "playing"}, {"state": "playing"}]))
        self.assertIsNone(watcher.tick())

    def test_unchanged_state_does_not_publish(self):
        state = make_state([{"state": "playing"}] * 3)
        watcher = StateWatcher(state)
        queue_ = state.subscribe()
        watcher.tick()
        self.assertIsNone(watcher.tick())
        self.assertTrue(queue_.empty())

    def test_state_change_publishes(self):
        state = make_state([{"state": "playing"}, {"state": "paused"}])
        watcher = StateWatcher(state)
        queue_ = state.subscribe()
        watcher.tick()

        self.assertIsNotNone(watcher.tick())
        self.assertEqual(queue_.get_nowait()["state"], "paused")

    def test_position_change_publishes_so_ui_can_tick_along(self):
        state = make_state(
            [{"state": "playing", "position": 10}, {"state": "playing", "position": 12}]
        )
        watcher = StateWatcher(state)
        watcher.tick()
        self.assertIsNotNone(watcher.tick())

    def test_watcher_survives_a_player_that_blows_up(self):
        class Exploding(ScriptedPlayer):
            def status(self):
                raise RuntimeError("socket padl")

        state = AppState(Exploding([{"state": "playing"}]), StubSource())
        watcher = StateWatcher(state)
        # Nie moze rzucic — inaczej watek ginie i stan zamarza na dobre.
        self.assertIsNone(watcher.tick())
        self.assertIn("socket padl", state.last_error)


class FinishedCallbackTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def watcher_for(self, sequence):
        state = make_state(sequence)
        state.current = {"anilist_id": 1, "episode": 3, "title": "T"}
        return StateWatcher(state, on_finished=lambda before, after: self.calls.append((before, after)))

    def test_playing_to_ended_fires_callback(self):
        watcher = self.watcher_for([{"state": "playing", "position": 100}, {"state": "ended"}])
        watcher.tick()
        watcher.tick()
        self.assertEqual(len(self.calls), 1)

    def test_callback_receives_the_snapshot_from_before_the_end(self):
        # Kluczowe: pozycja i odcinek sa jeszcze w poprzednim snapshocie.
        watcher = self.watcher_for(
            [{"state": "playing", "position": 640, "duration": 1400}, {"state": "ended"}]
        )
        watcher.tick()
        watcher.tick()
        before, after = self.calls[0]
        self.assertEqual(before["position"], 640)
        self.assertEqual(before["current"]["episode"], 3)
        self.assertEqual(after["state"], "ended")

    def test_player_closed_to_idle_also_counts_as_finished(self):
        # Tak wyglada zamkniecie okna mpv z pilota.
        watcher = self.watcher_for([{"state": "playing"}, {"state": "idle"}])
        watcher.tick()
        watcher.tick()
        self.assertEqual(len(self.calls), 1)

    def test_starting_to_playing_does_not_fire(self):
        watcher = self.watcher_for([{"state": "starting"}, {"state": "playing"}])
        watcher.tick()
        watcher.tick()
        self.assertEqual(self.calls, [])

    def test_idle_to_idle_does_not_fire(self):
        watcher = self.watcher_for([{"state": "idle"}, {"state": "idle"}])
        watcher.tick()
        watcher.tick()
        self.assertEqual(self.calls, [])

    def test_callback_failure_does_not_kill_the_watcher(self):
        state = make_state([{"state": "playing"}, {"state": "ended"}])

        def boom(before, after):
            raise RuntimeError("baza padla")

        watcher = StateWatcher(state, on_finished=boom)
        watcher.tick()
        self.assertIsNotNone(watcher.tick())  # publish nadal sie stal
        self.assertIn("baza padla", state.last_error)

    def test_ended_still_publishes_to_subscribers(self):
        # To jest cala poprawka zgloszonego bledu: apka MUSI dowiedziec sie
        # o zamknieciu odtwarzacza bez zadnego klikniecia.
        state = make_state([{"state": "playing"}, {"state": "ended"}])
        watcher = StateWatcher(state)
        queue_ = state.subscribe()
        watcher.tick()
        watcher.tick()
        self.assertEqual(queue_.get_nowait()["state"], "ended")


class ThreadTests(unittest.TestCase):
    def test_start_and_stop_are_idempotent_enough(self):
        watcher = StateWatcher(make_state([{"state": "idle"}]), interval=0.01)
        watcher.start()
        watcher.start()  # drugi start nie moze zrobic drugiego watku
        watcher.stop()
        watcher.stop()  # stop bez watku nie moze rzucic


if __name__ == "__main__":
    unittest.main()
