import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from db import init_schema
from history import END_MARGIN_SECONDS, MIN_RESUME_SECONDS, HistoryStore

UID = "uid-testowy"


class HistoryStoreTestCase(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / "h.sqlite3"
        init_schema(self.path)
        self.store = HistoryStore(self.path)


class RecordTests(HistoryStoreTestCase):
    def test_record_creates_entry(self):
        entry = self.store.record(UID, 1, 3, title="Cowboy Bebop", cover="c.jpg")
        self.assertEqual(entry["anilist_id"], 1)
        self.assertEqual(entry["episode"], 3)
        self.assertEqual(entry["title"], "Cowboy Bebop")

    def test_record_updates_existing_entry(self):
        self.store.record(UID, 1, 1)
        entry = self.store.record(UID, 1, 2)
        self.assertEqual(entry["episode"], 2)
        self.assertEqual(len(self.store.recent(UID)), 1)

    def test_missing_fields_do_not_wipe_stored_values(self):
        # Kluczowe: `stop` z backendu bez pozycji nie moze wyzerowac pozycji
        # zapisanej wczesniej przez backend, ktory ja zna.
        self.store.record(UID, 1, 1, position=500, duration=1400, title="T", cover="c.jpg")
        entry = self.store.record(UID, 1, 1)
        self.assertEqual(entry["position"], 500)
        self.assertEqual(entry["duration"], 1400)
        self.assertEqual(entry["title"], "T")
        self.assertEqual(entry["cover"], "c.jpg")

    def test_clear_position_zeroes_only_position(self):
        self.store.record(UID, 1, 1, position=500, duration=1400, title="T")
        self.store.clear_position(UID, 1)
        entry = self.store.entry(UID, 1)
        self.assertIsNone(entry["position"])
        self.assertIsNone(entry["duration"])
        self.assertEqual(entry["title"], "T")

    def test_users_are_isolated(self):
        self.store.record(UID, 1, 5)
        self.assertIsNone(self.store.entry("inny-uid", 1))
        self.assertEqual(self.store.recent("inny-uid"), [])

    def test_recent_is_newest_first(self):
        self.store.record(UID, 1, 1)
        self.store.record(UID, 2, 1)
        self.store.record(UID, 3, 1)
        # Ostatnio zapisane musi byc pierwsze — to karmi sekcje "Kontynuuj".
        self.assertEqual([e["anilist_id"] for e in self.store.recent(UID)][0], 3)

    def test_recent_respects_limit(self):
        for anilist_id in range(1, 6):
            self.store.record(UID, anilist_id, 1)
        self.assertEqual(len(self.store.recent(UID, limit=2)), 2)

    def test_delete(self):
        self.store.record(UID, 1, 1)
        self.assertTrue(self.store.delete(UID, 1))
        self.assertIsNone(self.store.entry(UID, 1))

    def test_delete_missing_reports_false(self):
        self.assertFalse(self.store.delete(UID, 999))


class ResumeTests(HistoryStoreTestCase):
    def test_unknown_anime_starts_from_the_beginning(self):
        self.assertEqual(
            self.store.resume(UID, 1), {"episode": 1, "position": 0, "reason": "nowe"}
        )

    def test_saved_position_is_offered(self):
        self.store.record(UID, 1, 4, position=600, duration=1400)
        resume = self.store.resume(UID, 1)
        self.assertEqual(resume["episode"], 4)
        self.assertEqual(resume["position"], 600)
        self.assertEqual(resume["reason"], "wznowienie")

    def test_barely_started_episode_restarts_from_zero(self):
        self.store.record(UID, 1, 4, position=MIN_RESUME_SECONDS - 1, duration=1400)
        resume = self.store.resume(UID, 1)
        self.assertEqual(resume["position"], 0)
        self.assertEqual(resume["episode"], 4)

    def test_episode_without_position_resumes_at_that_episode(self):
        # Sciezka ani-cli / android-intent: znamy odcinek, nie znamy czasu.
        self.store.record(UID, 1, 7)
        resume = self.store.resume(UID, 1)
        self.assertEqual(resume["episode"], 7)
        self.assertEqual(resume["position"], 0)

    def test_finished_episode_advances(self):
        self.store.record(UID, 1, 4, position=1400 - END_MARGIN_SECONDS, duration=1400)
        resume = self.store.resume(UID, 1)
        self.assertEqual(resume["episode"], 5)
        self.assertEqual(resume["position"], 0)
        self.assertEqual(resume["reason"], "nastepny odcinek")

    def test_last_episode_finished_does_not_advance_past_the_series(self):
        self.store.record(UID, 1, 26, position=1395, duration=1400)
        resume = self.store.resume(UID, 1, total_episodes=26)
        self.assertEqual(resume["episode"], 26)
        self.assertEqual(resume["reason"], "koniec serii")

    def test_unknown_total_still_advances(self):
        # Trwajace serie maja episodes=0 w AniList — nie mozemy blokowac postepu.
        self.store.record(UID, 1, 12, position=1395, duration=1400)
        self.assertEqual(self.store.resume(UID, 1, total_episodes=0)["episode"], 13)


if __name__ == "__main__":
    unittest.main()
