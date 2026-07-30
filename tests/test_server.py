import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from server import AppState


class AppStateTests(unittest.TestCase):
    def test_initial_state_is_idle(self):
        state = AppState()
        result = state.as_dict()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["player"], "vlc")

    def test_playback_starts_in_simulated_mode_when_vlc_is_missing(self):
        state = AppState()
        result = state.start_playback("demo.mp4")
        self.assertEqual(result["player"], "vlc")
        self.assertIn(result["status"], {"started", "simulated"})

    def test_playback_launches_configured_ani_cli_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cli_path = Path(tmpdir) / "ani-cli"
            cli_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC)

            state = AppState(ani_cli_command=str(cli_path))
            result = state.start_playback("demo.mp4")

            self.assertEqual(result["status"], "playing")
            self.assertEqual(result["player"], "ani-cli")
            self.assertIsNotNone(result["pid"])

            state.stop_playback()


if __name__ == "__main__":
    unittest.main()
