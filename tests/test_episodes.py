import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import metadata as M
from episodes import MAX_EPISODES, episode_list


class EpisodeListTests(unittest.TestCase):
    def test_finished_series_lists_all_episodes(self):
        items = episode_list(SimpleNamespace(episodes=3, aired=3))
        self.assertEqual(items, [{"number": 1}, {"number": 2}, {"number": 3}])

    def test_airing_series_lists_only_aired_episodes(self):
        # AniList: episodes=None dla emitowanych; aired = nastepny w emisji - 1.
        items = episode_list(SimpleNamespace(episodes=0, aired=7))
        self.assertEqual(len(items), 7)

    def test_unknown_or_absurd_totals_give_an_empty_list(self):
        self.assertEqual(episode_list(SimpleNamespace(episodes=0, aired=0)), [])
        self.assertEqual(episode_list(SimpleNamespace(episodes=MAX_EPISODES + 1, aired=0)), [])

    def test_extra_data_is_merged_but_cannot_change_the_number(self):
        extra = {2: {"title": "Dwa", "description": "opis", "number": 99}, 5: {"title": "poza zakresem"}}
        items = episode_list(SimpleNamespace(episodes=3, aired=3), extra=extra)
        self.assertEqual(items[1], {"number": 2, "title": "Dwa", "description": "opis"})
        self.assertEqual(items[0], {"number": 1})
        self.assertEqual(len(items), 3)

    def test_garbage_extra_is_ignored(self):
        items = episode_list(SimpleNamespace(episodes=2, aired=2), extra={1: "tekst", 2: None})
        self.assertEqual(items, [{"number": 1}, {"number": 2}])


class AiredEpisodesTests(unittest.TestCase):
    def test_airing_series_uses_next_airing_minus_one(self):
        node = {"episodes": None, "nextAiringEpisode": {"episode": 8}}
        self.assertEqual(M._aired_episodes(node), 7)

    def test_finished_series_uses_total(self):
        self.assertEqual(M._aired_episodes({"episodes": 26, "nextAiringEpisode": None}), 26)

    def test_nothing_known_is_zero(self):
        self.assertEqual(M._aired_episodes({}), 0)

    def test_queries_request_next_airing_episode(self):
        self.assertIn("nextAiringEpisode", M.SEARCH_QUERY)
        self.assertIn("nextAiringEpisode", M.DETAILS_QUERY)


if __name__ == "__main__":
    unittest.main()
