import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ani_runner import (
    DEFAULT_REFERER,
    MP4UPLOAD_REFERER,
    USER_AGENT,
    AniCliSource,
    SourceError,
    build_resolve_command,
    headers_for,
    parse_debug_output,
    referer_for,
    resolve_env,
)

DEBUG_OUTPUT = """
Searching...
All links:
1080 >https://cdn.example.test/1080/video.mp4
720 >https://cdn.example.test/720/video.mp4
Selected link:
https://cdn.example.test/1080/video.mp4
"""


def write_script(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class BuildCommandTests(unittest.TestCase):
    def test_command_passes_select_and_episode_to_skip_both_fzf_prompts(self):
        command = build_resolve_command("one piece", episode=5, quality="1080")
        self.assertEqual(
            command,
            ["ani-cli", "-q", "1080", "-S", "1", "-e", "5", "one piece"],
        )

    def test_numbers_are_stringified(self):
        command = build_resolve_command("x", episode=12, quality=720, select=3)
        self.assertEqual(command[2], "720")
        self.assertEqual(command[4], "3")
        self.assertEqual(command[6], "12")

    def test_custom_command_is_used(self):
        command = build_resolve_command("x", 1, ani_cli_command="/opt/ani-cli")
        self.assertEqual(command[0], "/opt/ani-cli")

    def test_resolve_env_forces_debug_player(self):
        self.assertEqual(resolve_env({"PATH": "/bin"})["ANI_CLI_PLAYER"], "debug")

    def test_resolve_env_does_not_mutate_input(self):
        base = {"PATH": "/bin"}
        resolve_env(base)
        self.assertNotIn("ANI_CLI_PLAYER", base)

    def test_resolve_env_inherits_path_by_default(self):
        # Regresja: zbudowanie env od zera gubi PATH, a wtedy nie da sie znalezc
        # ani samego ani-cli, ani curl/sed/grep, ktorych uzywa. Na Termuksie to
        # oznacza brak calego userlandu.
        self.assertEqual(resolve_env()["PATH"], os.environ["PATH"])

    def test_explicit_env_is_still_honoured_verbatim(self):
        self.assertEqual(resolve_env({"PATH": "/tylko/to"})["PATH"], "/tylko/to")


class ParseDebugOutputTests(unittest.TestCase):
    def test_parses_qualities_and_selected_url(self):
        selected, qualities = parse_debug_output(DEBUG_OUTPUT)
        self.assertEqual(selected, "https://cdn.example.test/1080/video.mp4")
        self.assertEqual(
            qualities,
            {
                "1080": "https://cdn.example.test/1080/video.mp4",
                "720": "https://cdn.example.test/720/video.mp4",
            },
        )

    def test_falls_back_to_first_link_when_selected_marker_missing(self):
        output = "All links:\n720 >https://cdn.example.test/720/v.mp4\n"
        selected, qualities = parse_debug_output(output)
        self.assertEqual(selected, "https://cdn.example.test/720/v.mp4")
        self.assertEqual(len(qualities), 1)

    def test_empty_output_yields_nothing(self):
        self.assertEqual(parse_debug_output(""), ("", {}))

    def test_noise_without_links_yields_nothing(self):
        self.assertEqual(parse_debug_output("Nie znaleziono\nblah\n"), ("", {}))


class RefererTests(unittest.TestCase):
    def test_mp4upload_gets_its_own_referer(self):
        self.assertEqual(
            referer_for("https://www2.mp4upload.com/x.mp4"), MP4UPLOAD_REFERER
        )

    def test_sharepoint_needs_no_referer(self):
        self.assertEqual(referer_for("https://my.sharepoint.com/x.mp4"), "")

    def test_anything_else_gets_default_referer(self):
        self.assertEqual(referer_for("https://cdn.allanime.test/x.mp4"), DEFAULT_REFERER)

    def test_matching_is_case_insensitive(self):
        self.assertEqual(referer_for("HTTPS://MP4UPLOAD.COM/X"), MP4UPLOAD_REFERER)

    def test_headers_always_carry_user_agent(self):
        headers = headers_for("https://cdn.allanime.test/x.mp4")
        self.assertEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["Referer"], DEFAULT_REFERER)

    def test_headers_omit_empty_referer(self):
        self.assertNotIn("Referer", headers_for("https://my.sharepoint.com/x.mp4"))

    def test_default_referer_matches_installed_ani_cli(self):
        # Sprawdzone przeciwko ani-cli 4.15.0: allanime_refr="https://mkissa.to".
        # Poprzednia wartosc (youtu-chan.com) byla nieaktualna i cicho psulaby
        # odtwarzanie, bo CDN-y allanime odrzucaja zly Referer.
        self.assertEqual(DEFAULT_REFERER, "https://mkissa.to")

    def test_default_referer_is_overridable(self):
        # allanime zmienia domeny czesciej niz my wydajemy kod, wiec musi sie dac
        # podmienic konfiguracja, bez ruszania zrodel.
        self.assertEqual(referer_for("https://cdn.x/y.mp4", "https://inna.test"), "https://inna.test")
        headers = headers_for("https://cdn.x/y.mp4", "https://inna.test")
        self.assertEqual(headers["Referer"], "https://inna.test")

    def test_per_cdn_referer_still_wins_over_override(self):
        self.assertEqual(
            referer_for("https://mp4upload.com/x", "https://inna.test"), MP4UPLOAD_REFERER
        )


class AniCliSourceTests(unittest.TestCase):
    def test_check_reports_missing_binaries(self):
        # which() zamockowane, zeby wynik nie zalezal od tego, co ma maszyna CI.
        with mock.patch("ani_runner.shutil.which", return_value=None):
            missing = AniCliSource(ani_cli_command="ani-cli").check()
        self.assertIn("ani-cli", missing)

    def test_check_flags_botan_as_hard_dependency(self):
        # Botan jest wymagany przez ani-cli >= 4.15; brak musi byc widoczny.
        with mock.patch("ani_runner.shutil.which", side_effect=lambda name: "/usr/bin/ani-cli" if name == "ani-cli" else None):
            missing = AniCliSource(ani_cli_command="ani-cli").check()
        self.assertEqual(len(missing), 1)
        self.assertIn("botan", missing[0])

    def test_check_is_empty_when_everything_present(self):
        with mock.patch("ani_runner.shutil.which", return_value="/usr/bin/x"):
            self.assertEqual(AniCliSource().check(), [])

    def test_resolve_returns_stream_with_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(
                Path(tmpdir) / "ani-cli",
                "#!/bin/sh\n"
                "echo 'All links:'\n"
                "echo '1080 >https://cdn.example.test/1080/v.mp4'\n"
                "echo 'Selected link:'\n"
                "echo 'https://cdn.example.test/1080/v.mp4'\n",
            )
            stream = AniCliSource(ani_cli_command=str(script)).resolve("query", 1)

        self.assertEqual(stream.stream_url, "https://cdn.example.test/1080/v.mp4")
        self.assertEqual(stream.referer, DEFAULT_REFERER)
        self.assertEqual(stream.headers["Referer"], DEFAULT_REFERER)
        self.assertEqual(stream.available_qualities, {"1080": "https://cdn.example.test/1080/v.mp4"})

    def test_resolve_finds_binary_by_bare_name_on_path(self):
        # Tak wola to server.py: ani_cli_command="ani-cli", bez sciezki.
        # Gdy podproces nie dziedziczy PATH, ten przypadek konczyl sie mylacym
        # bledem "zainstaluj ani-cli", mimo ze binarka byla na PATH.
        with tempfile.TemporaryDirectory() as tmpdir:
            write_script(
                Path(tmpdir) / "ani-cli",
                "#!/bin/sh\necho 'Selected link:'\necho 'https://x.test/v.mp4'\n",
            )
            patched = {"PATH": tmpdir + os.pathsep + os.environ.get("PATH", "")}
            with mock.patch.dict(os.environ, patched):
                stream = AniCliSource(ani_cli_command="ani-cli").resolve("q", 1)
        self.assertEqual(stream.stream_url, "https://x.test/v.mp4")

    def test_resolve_surfaces_what_ani_cli_actually_said(self):
        # Realny przypadek: allanime za zagadka Cloudflare. ani-cli jest
        # aktualne, wiec komunikat NIE moze kazac go aktualizowac — ma pokazac
        # prawdziwa przyczyne, ktora zna tylko ani-cli.
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(
                Path(tmpdir) / "ani-cli",
                "#!/bin/sh\n"
                "echo 'Checking dependencies...' >&2\n"
                "echo 'Episode is released, but no valid sources!' >&2\n"
                "exit 1\n",
            )
            with self.assertRaises(SourceError) as ctx:
                AniCliSource(ani_cli_command=str(script)).resolve("query", 1)

        message = str(ctx.exception)
        self.assertIn("Episode is released, but no valid sources!", message)
        self.assertNotIn("zaktualizuj ani-cli", message)

    def test_resolve_error_falls_back_when_ani_cli_says_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(Path(tmpdir) / "ani-cli", "#!/bin/sh\nexit 1\n")
            with self.assertRaises(SourceError) as ctx:
                AniCliSource(ani_cli_command=str(script)).resolve("query", 1)
        self.assertIn("nie zwrocilo adresu", str(ctx.exception))

    def test_resolve_uses_configured_referer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(
                Path(tmpdir) / "ani-cli",
                "#!/bin/sh\necho 'Selected link:'\necho 'https://cdn.x/y.mp4'\n",
            )
            stream = AniCliSource(
                ani_cli_command=str(script), default_referer="https://nowa.test"
            ).resolve("query", 1)
        self.assertEqual(stream.referer, "https://nowa.test")
        self.assertEqual(stream.headers["Referer"], "https://nowa.test")

    def test_resolve_raises_source_error_when_binary_missing(self):
        source = AniCliSource(ani_cli_command="/nie/ma/takiego/ani-cli")
        with self.assertRaises(SourceError) as ctx:
            source.resolve("query", 1)
        self.assertIn("zainstaluj ani-cli", str(ctx.exception))

    def test_resolve_times_out_into_source_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(Path(tmpdir) / "ani-cli", "#!/bin/sh\nsleep 5\n")
            source = AniCliSource(ani_cli_command=str(script), timeout=0.2)
            with self.assertRaises(SourceError) as ctx:
                source.resolve("query", 1)
        self.assertIn("nie odpowiedzialo", str(ctx.exception))

    def test_search_is_not_implemented_yet(self):
        with self.assertRaises(NotImplementedError):
            AniCliSource().search("x")


if __name__ == "__main__":
    unittest.main()
