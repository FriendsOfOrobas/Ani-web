import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ani_runner import ResolvedStream, SourceError
from mapping import LazyMapper
from metadata import AnimeMeta

BEBOP = AnimeMeta(
    anilist_id=1,
    romaji="Cowboy Bebop",
    english="Cowboy Bebop Eng",
    synonyms=["Kowboj Bebop", "카우보이 비밥"],
    episodes=26,
    cover="c.jpg",
)


class FakeSource:
    """Zrodlo, ktore zwraca adres tylko dla wybranych tytulow."""

    def __init__(self, working=(), error=None):
        self.working = set(working)
        self.error = error
        self.calls = []

    def resolve(self, provider_id, episode, quality=None):
        self.calls.append((provider_id, episode, quality))
        if self.error:
            raise self.error
        if provider_id not in self.working:
            raise SourceError(f"brak zrodel dla {provider_id!r}")
        return ResolvedStream(stream_url=f"http://cdn.test/{provider_id}.mp4")


class FakeMetadata:
    def __init__(self, meta=BEBOP, mapping=None):
        self.meta = meta
        self.mappings = dict(mapping or {})

    def details(self, anilist_id):
        return self.meta

    def cached_mapping(self, anilist_id):
        return self.mappings.get(int(anilist_id))

    def remember_mapping(self, anilist_id, provider_query):
        self.mappings[int(anilist_id)] = provider_query

    def forget_mapping(self, anilist_id):
        self.mappings.pop(int(anilist_id), None)


class CandidateOrderTests(unittest.TestCase):
    def test_without_cache_candidates_come_from_anilist(self):
        mapper = LazyMapper(FakeSource(), FakeMetadata())
        self.assertEqual(
            mapper.candidates_for(BEBOP), ["Cowboy Bebop", "Cowboy Bebop Eng", "Kowboj Bebop"]
        )

    def test_confirmed_mapping_goes_first(self):
        mapper = LazyMapper(FakeSource(), FakeMetadata(mapping={1: "Kowboj Bebop"}))
        self.assertEqual(mapper.candidates_for(BEBOP)[0], "Kowboj Bebop")

    def test_confirmed_mapping_is_not_duplicated(self):
        mapper = LazyMapper(FakeSource(), FakeMetadata(mapping={1: "Cowboy Bebop"}))
        candidates = mapper.candidates_for(BEBOP)
        self.assertEqual(candidates.count("Cowboy Bebop"), 1)

    def test_candidate_count_is_capped(self):
        # Kazda proba to osobne uruchomienie ani-cli — pudlo nie moze trwac minute.
        meta = AnimeMeta(
            anilist_id=1,
            romaji="A",
            english="B",
            synonyms=["C", "D", "E", "F", "G"],
        )
        mapper = LazyMapper(FakeSource(), FakeMetadata(meta=meta), max_candidates=3)
        self.assertEqual(len(mapper.candidates_for(meta)), 3)


class ResolveTests(unittest.TestCase):
    def test_first_working_candidate_wins(self):
        source = FakeSource(working={"Cowboy Bebop"})
        metadata = FakeMetadata()
        stream, meta = LazyMapper(source, metadata).resolve(1, episode=3)

        self.assertEqual(stream.stream_url, "http://cdn.test/Cowboy Bebop.mp4")
        self.assertEqual(meta.anilist_id, 1)
        self.assertEqual(source.calls, [("Cowboy Bebop", 3, None)])

    def test_falls_through_to_a_later_candidate(self):
        source = FakeSource(working={"Kowboj Bebop"})
        LazyMapper(source, FakeMetadata()).resolve(1)
        self.assertEqual(
            [call[0] for call in source.calls],
            ["Cowboy Bebop", "Cowboy Bebop Eng", "Kowboj Bebop"],
        )

    def test_winning_candidate_is_remembered(self):
        metadata = FakeMetadata()
        LazyMapper(FakeSource(working={"Kowboj Bebop"}), metadata).resolve(1)
        self.assertEqual(metadata.cached_mapping(1), "Kowboj Bebop")

    def test_remembered_mapping_short_circuits_next_time(self):
        source = FakeSource(working={"Kowboj Bebop"})
        metadata = FakeMetadata(mapping={1: "Kowboj Bebop"})
        LazyMapper(source, metadata).resolve(1)
        # Zaden nieudany kandydat nie byl probowany.
        self.assertEqual([call[0] for call in source.calls], ["Kowboj Bebop"])

    def test_stale_mapping_falls_back_to_fresh_candidates(self):
        # Zrodlo zmienilo tytul: zapamietany przestal dzialac, ale romaji dziala.
        source = FakeSource(working={"Cowboy Bebop"})
        metadata = FakeMetadata(mapping={1: "Nieistniejacy"})
        stream, _ = LazyMapper(source, metadata).resolve(1)

        self.assertEqual(stream.stream_url, "http://cdn.test/Cowboy Bebop.mp4")
        self.assertEqual(metadata.cached_mapping(1), "Cowboy Bebop")

    def test_total_failure_forgets_the_mapping(self):
        # Zeby nastepna proba zaczela od czystych kandydatow, nie od trupa.
        metadata = FakeMetadata(mapping={1: "Nieistniejacy"})
        with self.assertRaises(SourceError):
            LazyMapper(FakeSource(), metadata).resolve(1)
        self.assertIsNone(metadata.cached_mapping(1))

    def test_failure_message_lists_what_was_tried(self):
        with self.assertRaises(SourceError) as ctx:
            LazyMapper(FakeSource(), FakeMetadata()).resolve(1)
        message = str(ctx.exception)
        self.assertIn("Cowboy Bebop", message)
        self.assertIn("probowane", message)

    def test_anime_without_any_latin_title_is_a_clear_error(self):
        meta = AnimeMeta(anilist_id=2, native="カウボーイ", synonyms=["카우보이"])
        with self.assertRaises(SourceError) as ctx:
            LazyMapper(FakeSource(), FakeMetadata(meta=meta)).resolve(2)
        self.assertIn("Brak tytulu", str(ctx.exception))

    def test_quality_and_episode_are_passed_through(self):
        source = FakeSource(working={"Cowboy Bebop"})
        LazyMapper(source, FakeMetadata()).resolve(1, episode=12, quality="720")
        self.assertEqual(source.calls[0], ("Cowboy Bebop", 12, "720"))


if __name__ == "__main__":
    unittest.main()
