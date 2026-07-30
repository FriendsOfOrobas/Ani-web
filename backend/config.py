"""Wczytywanie konfiguracji.

Kolejnosc nadpisywania (od najslabszej do najsilniejszej):
    DEFAULTS  ->  config.json  ->  config.local.json  ->  zmienne ANI_WEB_*

config.local.json jest w .gitignore — to miejsce na ustawienia konkretnego
urzadzenia (sciezki, port, wybrany odtwarzacz), ktore nie maja trafic do repo.
"""

import json
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8000,
    "player": "auto",
    "quality": "1080",
    "ani_cli_command": "ani-cli",
    "mpv_command": "mpv",
    "mpv_ipc_socket": "",
    "adb_command": "adb",
    "android_player_package": "is.xyz.mpv",
    "data_dir": "data",
}

ENV_PREFIX = "ANI_WEB_"
_INT_KEYS = {"port"}


def _coerce(key, value):
    if key in _INT_KEYS:
        return int(value)
    return value


def _read_json(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _env_overrides(environ):
    overrides = {}
    for key in DEFAULTS:
        env_key = ENV_PREFIX + key.upper()
        if env_key in environ:
            overrides[key] = _coerce(key, environ[env_key])
    return overrides


def load_config(backend_dir=None, environ=None):
    backend_dir = Path(backend_dir) if backend_dir else BACKEND_DIR
    environ = environ if environ is not None else os.environ

    config = dict(DEFAULTS)
    config.update(_read_json(backend_dir / "config.json"))
    config.update(_read_json(backend_dir / "config.local.json"))
    config.update(_env_overrides(environ))

    config["port"] = int(config["port"])
    return config


def data_path(config, *parts):
    """Sciezka w katalogu danych, tworzona na zadanie."""
    base = Path(config["data_dir"])
    if not base.is_absolute():
        base = ROOT_DIR / base
    path = base.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
