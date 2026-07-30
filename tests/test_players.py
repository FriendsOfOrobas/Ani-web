import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from players import (
    BACKENDS,
    AndroidIntentPlayer,
    parse_media_session,
    AniCliPlayer,
    Capabilities,
    MpvIpcPlayer,
    PlayerError,
    SimulatedPlayer,
    build_backend,
    select_backend,
)


def write_script(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class FakeMpvIpc:
    """Atrapa serwera JSON IPC mpv na sockecie unixowym.

    Istnieje, zeby przetestowac protokol IPC bez zainstalowanego mpv. Celowo
    wtraca przed kazda odpowiedzia asynchroniczne zdarzenie — dokladnie tak jak
    robi mpv — zeby sprawdzic, ze klient je pomija i czeka na linie z "error".
    """

    def __init__(self, path, properties=None):
        self.path = path
        self.properties = dict(properties or {})
        self.commands = []
        self._closed = False
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._closed:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                buffer = b""
                try:
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                        while b"\n" in buffer:
                            line, _, buffer = buffer.partition(b"\n")
                            if line.strip():
                                conn.sendall(self._reply(json.loads(line)))
                except OSError:
                    pass

    def _reply(self, message):
        command = message.get("command", [])
        self.commands.append(command)
        name = command[0] if command else ""

        if name == "get_property":
            prop = command[1]
            if prop in self.properties:
                body = {"error": "success", "data": self.properties[prop]}
            else:
                body = {"error": "property unavailable"}
        elif name == "cycle":
            self.properties[command[1]] = not self.properties.get(command[1], False)
            body = {"error": "success"}
        elif name in {"set_property", "quit", "loadfile"}:
            body = {"error": "success"}
        else:
            body = {"error": "invalid parameter"}

        noise = json.dumps({"event": "property-change"}).encode() + b"\n"
        return noise + json.dumps(body).encode() + b"\n"

    def close(self):
        self._closed = True
        self._server.close()


class FakeProcess:
    """Udaje zywy proces mpv — MpvIpcPlayer patrzy tylko na poll()."""

    def __init__(self, alive=True, returncode=None):
        self._alive = alive
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = -15

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        self._alive = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class CapabilitiesTests(unittest.TestCase):
    def test_defaults_are_conservative(self):
        caps = Capabilities()
        self.assertTrue(caps.play)
        self.assertTrue(caps.stop)
        # Wszystko, co niepewne na Androidzie, domyslnie wylaczone.
        self.assertFalse(caps.pause)
        self.assertFalse(caps.seek)
        self.assertFalse(caps.position)
        self.assertFalse(caps.ended_events)

    def test_describe_exposes_name_and_capabilities(self):
        described = SimulatedPlayer().describe()
        self.assertEqual(described["player"], "simulated")
        self.assertTrue(described["capabilities"]["pause"])


class SimulatedPlayerTests(unittest.TestCase):
    def setUp(self):
        self.player = SimulatedPlayer()

    def test_starts_idle(self):
        self.assertEqual(self.player.status(), {"state": "idle"})

    def test_play_then_pause_toggles(self):
        self.assertEqual(self.player.play("http://x/y.mp4")["state"], "playing")
        self.assertEqual(self.player.pause()["state"], "paused")
        self.assertEqual(self.player.pause()["state"], "playing")

    def test_stop(self):
        self.player.play("http://x/y.mp4")
        self.assertEqual(self.player.stop()["state"], "stopped")

    def test_reports_no_position(self):
        self.player.play("http://x/y.mp4")
        self.assertNotIn("position", self.player.status())


class AniCliPlayerTests(unittest.TestCase):
    def test_play_spawns_process_and_reports_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(Path(tmpdir) / "ani-cli", "#!/bin/sh\nsleep 5\n")
            player = AniCliPlayer(ani_cli_command=str(script))
            status = player.play("http://x/y.mp4")
            try:
                self.assertEqual(status["state"], "playing")
                self.assertIsInstance(status["pid"], int)
            finally:
                player.stop()

    def test_stop_clears_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(Path(tmpdir) / "ani-cli", "#!/bin/sh\nsleep 5\n")
            player = AniCliPlayer(ani_cli_command=str(script))
            player.play("http://x/y.mp4")
            self.assertEqual(player.stop(), {"state": "stopped"})
            self.assertIsNone(player.process)
            self.assertEqual(player.status(), {"state": "idle"})

    def test_finished_process_reports_ended(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(Path(tmpdir) / "ani-cli", "#!/bin/sh\nexit 0\n")
            player = AniCliPlayer(ani_cli_command=str(script))
            player.play("http://x/y.mp4")
            player.process.wait(timeout=5)
            status = player.status()
        self.assertEqual(status["state"], "ended")
        self.assertEqual(status["exit_code"], 0)

    def test_pause_is_refused(self):
        # ani-cli nie daje zadnej kontroli poza zabiciem procesu.
        self.assertFalse(AniCliPlayer.capabilities.pause)
        with self.assertRaises(PlayerError):
            AniCliPlayer().pause()

    def test_second_play_does_not_spawn_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = write_script(Path(tmpdir) / "ani-cli", "#!/bin/sh\nsleep 5\n")
            player = AniCliPlayer(ani_cli_command=str(script))
            first = player.play("http://x/y.mp4")
            second = player.play("http://x/z.mp4")
            try:
                self.assertEqual(first["pid"], second["pid"])
            finally:
                player.stop()


class MpvIpcArgumentTests(unittest.TestCase):
    def test_headers_go_in_one_append_flag_each(self):
        player = MpvIpcPlayer(ipc_socket="/tmp/x.sock")
        args = player._build_args(
            "http://x/y.mp4",
            {"Referer": "https://a,b.test", "User-Agent": "UA"},
            0,
            "",
        )
        # Osobna flaga na naglowek: wartosc z przecinkiem nie moze sie rozjechac.
        self.assertIn("--http-header-fields-append=Referer: https://a,b.test", args)
        self.assertIn("--http-header-fields-append=User-Agent: UA", args)

    def test_position_becomes_start_flag(self):
        args = MpvIpcPlayer(ipc_socket="/tmp/x.sock")._build_args("u", None, 90, "")
        self.assertIn("--start=90", args)

    def test_no_start_flag_when_starting_from_zero(self):
        args = MpvIpcPlayer(ipc_socket="/tmp/x.sock")._build_args("u", None, 0, "")
        self.assertFalse([a for a in args if a.startswith("--start=")])

    def test_url_is_last_so_it_is_never_parsed_as_option(self):
        args = MpvIpcPlayer(ipc_socket="/tmp/x.sock")._build_args("http://x/y.mp4", None, 5, "T")
        self.assertEqual(args[-1], "http://x/y.mp4")

    def test_socket_path_defaults_into_tmp(self):
        self.assertTrue(MpvIpcPlayer().ipc_socket.endswith(".sock"))

    def test_missing_binary_gives_readable_error(self):
        player = MpvIpcPlayer(mpv_command="/nie/ma/mpv", ipc_socket="/tmp/ani-web-test.sock")
        with self.assertRaises(PlayerError) as ctx:
            player.play("http://x/y.mp4")
        self.assertIn("zainstaluj mpv", str(ctx.exception))


class MpvIpcProtocolTests(unittest.TestCase):
    """Protokol IPC przeciwko atrapie — bez mpv w systemie."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "mpv.sock")
        self.fake = FakeMpvIpc(
            self.socket_path,
            {"pause": False, "time-pos": 42.4, "duration": 1440.0},
        )
        self.player = MpvIpcPlayer(ipc_socket=self.socket_path)
        self.player.process = FakeProcess()

    def tearDown(self):
        self.fake.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        os.rmdir(self.tmpdir)

    def test_status_reads_state_and_rounds_position(self):
        status = self.player.status()
        self.assertEqual(status["state"], "playing")
        self.assertEqual(status["position"], 42)
        self.assertEqual(status["duration"], 1440)

    def test_status_reports_paused(self):
        self.fake.properties["pause"] = True
        self.assertEqual(self.player.status()["state"], "paused")

    def test_pause_sends_cycle_pause(self):
        self.player.pause()
        self.assertIn(["cycle", "pause"], self.fake.commands)
        self.assertTrue(self.fake.properties["pause"])

    def test_unavailable_property_is_swallowed_not_raised(self):
        # Przed zaladowaniem pliku mpv nie zna time-pos; status musi to znosic.
        del self.fake.properties["time-pos"]
        status = self.player.status()
        self.assertNotIn("position", status)
        self.assertEqual(status["state"], "playing")

    def test_pause_on_dead_process_is_refused(self):
        self.player.process = FakeProcess(alive=False, returncode=0)
        with self.assertRaises(PlayerError):
            self.player.pause()

    def test_status_of_dead_process_is_ended(self):
        self.player.process = FakeProcess(alive=False, returncode=0)
        status = self.player.status()
        self.assertEqual(status["state"], "ended")
        self.assertEqual(status["exit_code"], 0)

    def test_status_without_process_is_idle(self):
        self.player.process = None
        self.assertEqual(self.player.status(), {"state": "idle"})

    def test_stop_sends_quit_and_clears_process(self):
        result = self.player.stop()
        self.assertEqual(result, {"state": "stopped"})
        self.assertIn(["quit"], self.fake.commands)
        self.assertIsNone(self.player.process)

    def test_ipc_error_becomes_player_error(self):
        self.fake.close()
        os.unlink(self.socket_path)
        with self.assertRaises(PlayerError) as ctx:
            self.player.pause()
        self.assertIn("IPC", str(ctx.exception))

    def test_wait_for_socket_fails_fast_when_mpv_dies(self):
        player = MpvIpcPlayer(ipc_socket=os.path.join(self.tmpdir, "nieistnieje.sock"))
        player.process = FakeProcess(alive=False, returncode=2)
        start = time.monotonic()
        with self.assertRaises(PlayerError) as ctx:
            player._wait_for_socket()
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertIn("zakonczyl sie od razu", str(ctx.exception))


class SelectBackendTests(unittest.TestCase):
    BASE = {"ani_cli_command": "ani-cli", "mpv_command": "mpv", "mpv_ipc_socket": ""}

    def test_explicit_name_wins(self):
        player = select_backend({**self.BASE, "player": "simulated"})
        self.assertIsInstance(player, SimulatedPlayer)

    def test_which_argument_overrides_config(self):
        player = select_backend({**self.BASE, "player": "simulated"}, which="mpv-ipc")
        self.assertIsInstance(player, MpvIpcPlayer)

    def test_unknown_name_lists_available_backends(self):
        with self.assertRaises(PlayerError) as ctx:
            select_backend({**self.BASE, "player": "vlc-http"})
        message = str(ctx.exception)
        self.assertIn("vlc-http", message)
        for name in BACKENDS:
            self.assertIn(name, message)

    def test_auto_falls_back_to_simulated_without_ani_cli(self):
        with mock.patch("players.shutil.which", return_value=None):
            player = select_backend({**self.BASE, "player": "auto"})
        self.assertIsInstance(player, SimulatedPlayer)

    def test_auto_prefers_ani_cli_when_present(self):
        with mock.patch("players.shutil.which", return_value="/usr/bin/ani-cli"):
            player = select_backend({**self.BASE, "player": "auto"})
        self.assertIsInstance(player, AniCliPlayer)

    def test_build_backend_passes_config_through(self):
        player = build_backend("mpv-ipc", {**self.BASE, "mpv_command": "/opt/mpv"})
        self.assertEqual(player.mpv_command, "/opt/mpv")

    def test_android_backend_gets_package_from_config(self):
        player = build_backend("android-intent", {"android_player_package": "org.videolan.vlc"})
        self.assertIsInstance(player, AndroidIntentPlayer)
        self.assertEqual(player.package, "org.videolan.vlc")


DUMPSYS_PLAYING = """Media session state:
Sessions Stack - have 2 sessions:
  package=is.xyz.mpv
  mediaSession=android.media.session.ISessionController$Stub$Proxy@abc
  state=PlaybackState {state=3, position=124500, buffered position=0, speed=1.0, updated=99, actions=822, duration=1420000}
  package=com.spotify.music
  state=PlaybackState {state=2, position=5000, duration=200000}
"""

DUMPSYS_EMPTY = "Media session state:\nSessions Stack - have 0 sessions:\n"


class FakeAdb:
    """Udaje `am` i `adb shell` — Stage 2 bez Androida."""

    def __init__(self, dumpsys=DUMPSYS_PLAYING, adb_ok=True, am_returncode=0):
        self.dumpsys = dumpsys
        self.adb_ok = adb_ok
        self.am_returncode = am_returncode
        self.calls = []

    def __call__(self, command, timeout=None):
        self.calls.append(list(command))
        result = subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["adb", "get-state"]:
            result.stdout = "device\n" if self.adb_ok else ""
            result.returncode = 0 if self.adb_ok else 1
        elif "dumpsys" in command:
            result.stdout = self.dumpsys
        elif command[:2] == ["am", "start"]:
            result.returncode = self.am_returncode
            if self.am_returncode:
                result.stderr = "Error: Activity not started"
        return result

    def shell_calls(self):
        return [c for c in self.calls if c[:2] == [self.calls[0][0], "shell"]] if self.calls else []


class MediaSessionParsingTests(unittest.TestCase):
    def test_parses_state_position_and_duration(self):
        parsed = parse_media_session(DUMPSYS_PLAYING, "is.xyz.mpv")
        self.assertEqual(parsed, {"state": "playing", "position": 124, "duration": 1420})

    def test_buffered_position_does_not_shadow_real_position(self):
        # dumpsys zawiera i "position=124500", i "buffered position=0" — naiwne
        # parsowanie nadpisywalo prawdziwa pozycje zerem.
        self.assertEqual(parse_media_session(DUMPSYS_PLAYING, "is.xyz.mpv")["position"], 124)

    def test_reads_the_right_package_block(self):
        parsed = parse_media_session(DUMPSYS_PLAYING, "com.spotify.music")
        self.assertEqual(parsed["state"], "paused")

    def test_absent_package_means_no_session(self):
        self.assertIsNone(parse_media_session(DUMPSYS_PLAYING, "org.videolan.vlc"))
        self.assertIsNone(parse_media_session(DUMPSYS_EMPTY, "is.xyz.mpv"))

    def test_buffering_counts_as_playing(self):
        dump = "  package=p\n  state=PlaybackState {state=6, position=1000}\n"
        self.assertEqual(parse_media_session(dump, "p")["state"], "playing")

    def test_unparseable_block_is_unknown_not_a_crash(self):
        dump = "  package=p\n  jakies inne smieci\n"
        self.assertEqual(parse_media_session(dump, "p"), {"state": "unknown"})

    def test_negative_position_is_dropped(self):
        # PlaybackState podaje -1, gdy pozycja jest nieznana.
        dump = "  package=p\n  state=PlaybackState {state=3, position=-1, duration=0}\n"
        parsed = parse_media_session(dump, "p")
        self.assertNotIn("position", parsed)
        self.assertNotIn("duration", parsed)


class AndroidIntentPlayerTests(unittest.TestCase):
    def test_intent_targets_mpv_activity_by_default(self):
        args = AndroidIntentPlayer()._intent_args("http://x/y.mp4", 0, "")
        self.assertIn("-n", args)
        self.assertIn("is.xyz.mpv/.MPVActivity", args)
        self.assertIn("android.intent.action.VIEW", args)

    def test_position_goes_in_extras_as_milliseconds(self):
        args = AndroidIntentPlayer()._intent_args("http://x/y.mp4", 90, "")
        self.assertIn("--ei", args)
        self.assertIn("90000", args)

    def test_unknown_package_falls_back_to_package_targeting(self):
        args = AndroidIntentPlayer(package="com.inny.player")._intent_args("u", 0, "")
        self.assertIn("-p", args)
        self.assertIn("com.inny.player", args)

    def test_vlc_has_its_own_activity(self):
        args = AndroidIntentPlayer(package="org.videolan.vlc")._intent_args("u", 0, "")
        self.assertIn("org.videolan.vlc/.gui.video.VideoPlayerActivity", args)

    def test_play_runs_am_start(self):
        adb = FakeAdb()
        AndroidIntentPlayer(runner=adb).play("http://x/y.mp4")
        self.assertEqual(adb.calls[0][:2], ["am", "start"])

    def test_failed_am_start_points_at_the_overlay_permission(self):
        # Najczestsza przyczyna na Androidzie 10+ — docs.md §10.
        adb = FakeAdb(am_returncode=1)
        with self.assertRaises(PlayerError) as ctx:
            AndroidIntentPlayer(runner=adb).play("http://x/y.mp4")
        self.assertIn("Draw over other apps", str(ctx.exception))

    def test_status_reports_position_from_dumpsys(self):
        player = AndroidIntentPlayer(runner=FakeAdb())
        player.play("http://x/y.mp4")
        self.assertEqual(player.status()["position"], 124)

    def test_missing_session_right_after_start_is_not_the_end(self):
        # Odtwarzacz potrzebuje chwili na rejestracje sesji. Bez karencji dozorca
        # oglaszalby koniec odcinka w sekundzie startu.
        player = AndroidIntentPlayer(runner=FakeAdb(dumpsys=DUMPSYS_EMPTY))
        self.assertEqual(player.play("http://x/y.mp4")["state"], "starting")

    def test_missing_session_after_grace_means_player_was_closed(self):
        player = AndroidIntentPlayer(runner=FakeAdb(dumpsys=DUMPSYS_EMPTY))
        player.play("http://x/y.mp4")
        player._started_at = time.monotonic() - player.SESSION_GRACE_SECONDS - 1
        self.assertEqual(player.status()["state"], "ended")

    def test_without_adb_capabilities_degrade_instead_of_lying(self):
        player = AndroidIntentPlayer(runner=FakeAdb(adb_ok=False))
        described = player.describe()
        self.assertFalse(described["adb"])
        self.assertFalse(described["capabilities"]["pause"])
        self.assertTrue(described["capabilities"]["play"])

    def test_without_adb_state_is_unknown_not_playing(self):
        # Zeby dozorca nie zglosil falszywego konca odcinka.
        player = AndroidIntentPlayer(runner=FakeAdb(adb_ok=False))
        player.play("http://x/y.mp4")
        self.assertEqual(player.status()["state"], "unknown")

    def test_pause_without_adb_is_refused_with_a_reason(self):
        player = AndroidIntentPlayer(runner=FakeAdb(adb_ok=False))
        player.play("http://x/y.mp4")
        with self.assertRaises(PlayerError) as ctx:
            player.pause()
        self.assertIn("self-adb", str(ctx.exception))

    def test_pause_sends_the_media_keyevent(self):
        adb = FakeAdb()
        player = AndroidIntentPlayer(runner=adb)
        player.play("http://x/y.mp4")
        player.pause()
        self.assertTrue(
            any("keyevent" in c and str(player.KEYCODE_PLAY_PAUSE) in c for c in adb.calls)
        )

    def test_stop_force_stops_the_package(self):
        adb = FakeAdb()
        player = AndroidIntentPlayer(runner=adb)
        player.play("http://x/y.mp4")
        self.assertEqual(player.stop(), {"state": "stopped"})
        self.assertIn(["am", "force-stop", "is.xyz.mpv"], adb.calls)

    def test_headers_are_not_passed_through_the_intent(self):
        # mpv-android ich nie przyjmuje (#427) — Referer dokleja proxy, docs.md §7.
        adb = FakeAdb()
        AndroidIntentPlayer(runner=adb).play("http://x/y.mp4", headers={"Referer": "https://r"})
        self.assertNotIn("https://r", " ".join(adb.calls[0]))

    def test_no_seek_method_exists(self):
        # docs.md D2 — seek jest niedostepny i nie ma go udawac.
        self.assertFalse(AndroidIntentPlayer.capabilities.seek)
        self.assertFalse(hasattr(AndroidIntentPlayer, "seek"))


if __name__ == "__main__":
    unittest.main()
