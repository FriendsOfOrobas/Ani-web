import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from config import DEFAULTS, data_path, load_config


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


class LoadConfigTests(unittest.TestCase):
    """Kolejnosc nadpisywania: DEFAULTS -> config.json -> config.local.json -> ANI_WEB_*."""

    def test_defaults_when_nothing_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(backend_dir=tmpdir, environ={})
        self.assertEqual(config, {**DEFAULTS, "port": DEFAULTS["port"]})

    def test_config_json_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_json(Path(tmpdir) / "config.json", {"quality": "720"})
            config = load_config(backend_dir=tmpdir, environ={})
        self.assertEqual(config["quality"], "720")
        self.assertEqual(config["host"], DEFAULTS["host"])

    def test_local_config_overrides_config_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_json(Path(tmpdir) / "config.json", {"player": "simulated"})
            write_json(Path(tmpdir) / "config.local.json", {"player": "mpv-ipc"})
            config = load_config(backend_dir=tmpdir, environ={})
        self.assertEqual(config["player"], "mpv-ipc")

    def test_env_overrides_everything(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_json(Path(tmpdir) / "config.json", {"player": "simulated"})
            write_json(Path(tmpdir) / "config.local.json", {"player": "mpv-ipc"})
            config = load_config(
                backend_dir=tmpdir,
                environ={"ANI_WEB_PLAYER": "ani-cli", "ANI_WEB_PORT": "9001"},
            )
        self.assertEqual(config["player"], "ani-cli")
        self.assertEqual(config["port"], 9001)

    def test_port_is_always_int(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_json(Path(tmpdir) / "config.json", {"port": "8080"})
            config = load_config(backend_dir=tmpdir, environ={})
        self.assertIsInstance(config["port"], int)
        self.assertEqual(config["port"], 8080)

    def test_unknown_env_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(
                backend_dir=tmpdir, environ={"ANI_WEB_NIE_ISTNIEJE": "x"}
            )
        self.assertNotIn("nie_istnieje", config)


class DataPathTests(unittest.TestCase):
    def test_absolute_data_dir_is_respected_and_parent_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = data_path({"data_dir": tmpdir}, "settings", "settings.json")
            self.assertEqual(path, Path(tmpdir) / "settings" / "settings.json")
            self.assertTrue(path.parent.is_dir())

    def test_relative_data_dir_hangs_off_repo_root(self):
        path = data_path({"data_dir": "data"}, "cache", "x.json")
        self.assertEqual(path, ROOT / "data" / "cache" / "x.json")


if __name__ == "__main__":
    unittest.main()
