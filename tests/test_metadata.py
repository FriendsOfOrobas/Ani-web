import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import metadata as M
from db import init_schema
from metadata import (
    AnimeMeta,
    AniListProvider,
    MetadataError,
    MetadataService,
    candidate_titles,
    strip_html,
)

BEBOP_NODE = {
    "id": 1,
    "title": {"romaji": "Cowboy Bebop", "english": "Cowboy Bebop", "native": "カウボーイビバップ"},
    "synonyms": ["Kowboj Bebop", "카우보이 비밥"],
    "description": "Enter a world <br>in the <i>distant</i> future.",
    "episodes": 26,
    "status": "FINISHED",
    "seasonYear": 1998,
    "format": "TV",
    "averageScore": 86,
    "coverImage": {"large": "https://x/large.png", "medium": "https://x/medium.png"},
    "bannerImage": "https://x/banner.jpg",
}


def anilist_page(nodes):
    return {"data": {"Page": {"media": nodes}}}


class StripHtmlTests(unittest.TestCase):
    def test_tags_are_removed(self):
        # AniList wstawia <br> i <i> nawet przy asHtml:false.
        self.assertEqual(strip_html("a <br>b <i>c</i>"), "a b c")

    def test_empty_input(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html(""), "")

    def test_excess_blank_lines_collapse(self):
        self.assertEqual(strip_html("a\n\n\n\nb"), "a\n\nb")


class AnimeMetaTests(unittest.TestCase):
    def test_title_prefers_romaji(self):
        meta = AnimeMeta(anilist_id=1, romaji="R", english="E", native="N")
        self.assertEqual(meta.title, "R")

    def test_title_falls_back_through_english_then_native(self):
        self.assertEqual(AnimeMeta(anilist_id=1, english="E", native="N").title, "E")
        self.assertEqual(AnimeMeta(anilist_id=1, native="N").title, "N")

    def test_title_of_last_resort_is_the_id(self):
        self.assertEqual(AnimeMeta(anilist_id=42).title, "#42")

    def test_as_dict_includes_computed_title(self):
        self.assertEqual(AnimeMeta(anilist_id=1, romaji="R").as_dict()["title"], "R")


class CandidateTitlesTests(unittest.TestCase):
    def test_romaji_comes_first(self):
        meta = AnimeMeta(anilist_id=1, romaji="R", english="E")
        self.assertEqual(candidate_titles(meta)[0], "R")

    def test_non_latin_synonyms_are_dropped(self):
        # Kazda proba to osobne uruchomienie ani-cli, a allanime nie zna
        # tytulow koreanskich czy hebrajskich — nie marnujemy na nie prob.
        meta = AnimeMeta(anilist_id=1, romaji="R", synonyms=["Latin Name", "카우보이 비밥"])
        self.assertEqual(candidate_titles(meta), ["R", "Latin Name"])

    def test_duplicates_are_collapsed_case_insensitively(self):
        meta = AnimeMeta(anilist_id=1, romaji="Cowboy Bebop", english="cowboy bebop")
        self.assertEqual(candidate_titles(meta), ["Cowboy Bebop"])

    def test_empty_titles_are_skipped(self):
        self.assertEqual(candidate_titles(AnimeMeta(anilist_id=1, romaji="", english="E")), ["E"])


class AniListProviderTests(unittest.TestCase):
    def test_search_maps_all_fields(self):
        with mock.patch.object(M, "_post_json", return_value=anilist_page([BEBOP_NODE])):
            (meta,) = AniListProvider().search("cowboy")

        self.assertEqual(meta.anilist_id, 1)
        self.assertEqual(meta.romaji, "Cowboy Bebop")
        self.assertEqual(meta.episodes, 26)
        self.assertEqual(meta.year, 1998)
        self.assertEqual(meta.score, 86)
        self.assertEqual(meta.cover, "https://x/large.png")
        self.assertNotIn("<br>", meta.description)

    def test_graphql_errors_become_metadata_error(self):
        with mock.patch.object(M, "_post_json", return_value={"errors": [{"message": "za duzo"}]}):
            with self.assertRaises(MetadataError) as ctx:
                AniListProvider().search("x")
        self.assertIn("za duzo", str(ctx.exception))

    def test_network_failure_becomes_metadata_error(self):
        with mock.patch.object(M, "_post_json", side_effect=OSError("brak sieci")):
            with self.assertRaises(MetadataError):
                AniListProvider().search("x")

    def test_missing_media_in_details_is_an_error(self):
        with mock.patch.object(M, "_post_json", return_value={"data": {"Media": None}}):
            with self.assertRaises(MetadataError):
                AniListProvider().details(1)

    def test_empty_result_list_is_not_an_error(self):
        with mock.patch.object(M, "_post_json", return_value=anilist_page([])):
            self.assertEqual(AniListProvider().search("nic"), [])


class MetadataServiceTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / "m.sqlite3"
        init_schema(self.path)

    def service(self, provider=None, fallback=None):
        return MetadataService(self.path, provider=provider, fallback=fallback)

    def test_search_hits_provider_once_then_serves_cache(self):
        provider = mock.Mock()
        provider.search.return_value = [M._meta_from_anilist(BEBOP_NODE)]
        service = self.service(provider=provider, fallback=None)

        first = service.search("cowboy")
        second = service.search("cowboy")

        self.assertEqual(provider.search.call_count, 1)
        self.assertEqual([m.anilist_id for m in first], [m.anilist_id for m in second])

    def test_search_cache_is_case_insensitive(self):
        provider = mock.Mock()
        provider.search.return_value = [M._meta_from_anilist(BEBOP_NODE)]
        service = self.service(provider=provider, fallback=None)

        service.search("Cowboy")
        service.search("cowboy")
        self.assertEqual(provider.search.call_count, 1)

    def test_search_preserves_relevance_order_through_cache(self):
        nodes = [dict(BEBOP_NODE, id=i) for i in (5, 1, 17205)]
        provider = mock.Mock()
        provider.search.return_value = [M._meta_from_anilist(n) for n in nodes]
        service = self.service(provider=provider, fallback=None)

        service.search("cowboy")
        self.assertEqual([m.anilist_id for m in service.search("cowboy")], [5, 1, 17205])

    def test_blank_query_never_reaches_the_network(self):
        provider = mock.Mock()
        self.assertEqual(self.service(provider=provider).search("   "), [])
        provider.search.assert_not_called()

    def test_details_are_cached(self):
        provider = mock.Mock()
        provider.details.return_value = M._meta_from_anilist(BEBOP_NODE)
        service = self.service(provider=provider, fallback=None)

        service.details(1)
        service.details(1)
        self.assertEqual(provider.details.call_count, 1)

    def test_fallback_takes_over_when_primary_fails(self):
        primary = mock.Mock()
        primary.search.side_effect = MetadataError("AniList padl")
        fallback = mock.Mock()
        fallback.search.return_value = [M._meta_from_anilist(BEBOP_NODE)]

        results = self.service(provider=primary, fallback=fallback).search("cowboy")
        self.assertEqual(len(results), 1)
        fallback.search.assert_called_once()

    def test_original_error_wins_when_fallback_also_fails(self):
        primary = mock.Mock()
        primary.search.side_effect = MetadataError("AniList padl")
        fallback = mock.Mock()
        fallback.search.side_effect = MetadataError("Jikan tez padl")

        with self.assertRaises(MetadataError) as ctx:
            self.service(provider=primary, fallback=fallback).search("x")
        # Diagnostycznie wazniejszy jest blad zrodla glownego.
        self.assertIn("AniList padl", str(ctx.exception))

    def test_stale_cache_is_refetched(self):
        provider = mock.Mock()
        provider.details.return_value = M._meta_from_anilist(BEBOP_NODE)
        service = self.service(provider=provider, fallback=None)
        service.details(1)

        with mock.patch.object(M, "ANIME_TTL_SECONDS", -1):
            service.details(1)
        self.assertEqual(provider.details.call_count, 2)

    def test_cached_payload_round_trips_all_fields(self):
        provider = mock.Mock()
        provider.details.return_value = M._meta_from_anilist(BEBOP_NODE)
        service = self.service(provider=provider, fallback=None)

        fresh = service.details(1)
        provider.details.side_effect = AssertionError("nie wolno siegac do sieci")
        cached = service.details(1)
        self.assertEqual(cached.as_dict(), fresh.as_dict())


class MappingCacheTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / "m.sqlite3"
        init_schema(self.path)
        self.service = MetadataService(self.path, provider=mock.Mock(), fallback=None)

    def test_unknown_mapping_is_none(self):
        self.assertIsNone(self.service.cached_mapping(1))

    def test_remember_then_read(self):
        self.service.remember_mapping(1, "Cowboy Bebop")
        self.assertEqual(self.service.cached_mapping(1), "Cowboy Bebop")

    def test_remember_overwrites(self):
        self.service.remember_mapping(1, "stary")
        self.service.remember_mapping(1, "nowy")
        self.assertEqual(self.service.cached_mapping(1), "nowy")

    def test_forget(self):
        self.service.remember_mapping(1, "Cowboy Bebop")
        self.service.forget_mapping(1)
        self.assertIsNone(self.service.cached_mapping(1))


if __name__ == "__main__":
    unittest.main()
