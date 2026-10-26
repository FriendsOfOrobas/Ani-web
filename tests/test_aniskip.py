import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from aniskip import fetch_skip_times, parse_skip_times, script_opts

# Odpowiedz AniSkip v1 (skrocona, prawdziwy ksztalt: Naruto, odc. 1).
NARUTO_EP1 = {
    "found": True,
    "results": [
        {"interval": {"start_time": 1, "end_time": 102}, "skip_type": "op", "episode_length": 1429.2},
        {"interval": {"start_time": 1280.484, "end_time": 1369.984}, "skip_type": "ed", "episode_length": 1400.5},
    ],
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(payload):
    return lambda request, timeout=None: FakeResponse(json.dumps(payload).encode())


class ParseTests(unittest.TestCase):
    def test_parses_op_and_ed(self):
        self.assertEqual(
            parse_skip_times(NARUTO_EP1), {"op": (1.0, 102.0), "ed": (1280.484, 1369.984)}
        )

    def test_not_found_or_garbage_is_empty(self):
        self.assertEqual(parse_skip_times({"found": False, "results": []}), {})
        self.assertEqual(parse_skip_times(None), {})
        self.assertEqual(parse_skip_times("x"), {})

    def test_invalid_intervals_are_dropped(self):
        payload = {
            "found": True,
            "results": [
                {"interval": {"start_time": 50, "end_time": 40}, "skip_type": "op"},
                {"interval": {"start_time": 0, "end_time": 5000}, "skip_type": "ed"},
                {"interval": {"start_time": 10, "end_time": 20}, "skip_type": "recap"},
                {"interval": {"start_time": "a", "end_time": 20}, "skip_type": "op"},
            ],
        }
        self.assertEqual(parse_skip_times(payload), {})

    def test_first_interval_of_a_type_wins(self):
        payload = {"found": True, "results": [
            {"interval": {"start_time": 5, "end_time": 50}, "skip_type": "op"},
            {"interval": {"start_time": 6, "end_time": 60}, "skip_type": "op"},
        ]}
        self.assertEqual(parse_skip_times(payload), {"op": (5.0, 50.0)})


class FetchTests(unittest.TestCase):
    def test_fetch_returns_parsed_times(self):
        times = fetch_skip_times(20, 1, opener=opener_returning(NARUTO_EP1))
        self.assertIn("op", times)

    def test_network_errors_are_soft(self):
        def boom(request, timeout=None):
            raise urllib.error.URLError("brak sieci")

        self.assertEqual(fetch_skip_times(20, 1, opener=boom), {})

    def test_http_404_means_no_data_not_an_exception(self):
        def not_found(request, timeout=None):
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)

        self.assertEqual(fetch_skip_times(20, 1, opener=not_found), {})

    def test_invalid_json_is_soft(self):
        bad = lambda request, timeout=None: FakeResponse(b"<html>")
        self.assertEqual(fetch_skip_times(20, 1, opener=bad), {})

    def test_missing_ids_do_not_hit_the_network(self):
        def must_not_call(request, timeout=None):
            raise AssertionError("nie powinno byc zapytania")

        self.assertEqual(fetch_skip_times(0, 1, opener=must_not_call), {})
        self.assertEqual(fetch_skip_times(20, 0, opener=must_not_call), {})
        self.assertEqual(fetch_skip_times(None, 1, opener=must_not_call), {})


class ScriptOptsTests(unittest.TestCase):
    def test_formats_without_trailing_zeros(self):
        opts = script_opts({"op": (1.0, 102.0), "ed": (1280.484, 1369.984)})
        self.assertEqual(
            opts,
            "aniweb_skip-op_start=1,aniweb_skip-op_end=102,"
            "aniweb_skip-ed_start=1280.484,aniweb_skip-ed_end=1369.984",
        )

    def test_empty_when_nothing_to_skip(self):
        self.assertEqual(script_opts({}), "")


if __name__ == "__main__":
    unittest.main()
