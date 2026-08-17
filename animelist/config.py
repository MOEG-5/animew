"""Central configuration and XDG paths (PRD F4/F6)."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "animelist-widget"

MAL_API_BASE = "https://api.myanimelist.net/v2"
MAL_OAUTH_AUTHORIZE = "https://myanimelist.net/v1/oauth2/authorize"
MAL_OAUTH_TOKEN = "https://myanimelist.net/v1/oauth2/token"

DEFAULT_SVP_SOCKET = "/tmp/mpvsocket"   # SVP4's mpv integration
DEFAULT_MPV_SOCKET = "/tmp/mpv.sock"    # plain mpv (user config)
DEFAULT_CALLBACK_URL = "http://localhost:8765/callback"

DEFAULTS: dict = {
    "mpv_sockets": [DEFAULT_SVP_SOCKET, DEFAULT_MPV_SOCKET],
    "mal_client_id": "",
    "mal_client_secret": "",
    "callback_url": DEFAULT_CALLBACK_URL,
}


def xdg_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def xdg_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_NAME


def xdg_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_NAME


CONFIG_FILE = xdg_config_dir() / "config.json"
DB_PATH = xdg_data_dir() / "animelist.db"
TOKEN_FILE = xdg_data_dir() / "token.json"
IMAGE_CACHE_DIR = xdg_cache_dir() / "images"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text())
            # Legacy single-socket key -> normalize to a list.
            if "mpv_socket" in loaded and "mpv_sockets" not in loaded:
                loaded["mpv_sockets"] = [loaded.pop("mpv_socket")]
            cfg.update(loaded)
        except (json.JSONDecodeError, OSError):
            pass
    sockets = cfg.get("mpv_sockets")
    if isinstance(sockets, str):
        cfg["mpv_sockets"] = [sockets]
    elif not isinstance(sockets, list) or not sockets:
        cfg["mpv_sockets"] = list(DEFAULTS["mpv_sockets"])
    return cfg


def save_config(cfg: dict[str, str]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
