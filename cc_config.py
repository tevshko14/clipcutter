"""ClipCutter configuration and paths.

Single source of truth for:
- Filesystem locations (APP_DIR, CONFIG_PATH, SESSIONS_DIR, OUTPUT_DIR)
- Supported input video formats
- User-settable config (load_config / save_config / DEFAULT_CONFIG)
"""

import json
from pathlib import Path

# ---------- Paths ----------

APP_DIR = Path.home() / ".clipcutter"
APP_DIR.mkdir(exist_ok=True)
DB_PATH = APP_DIR / "clipcutter.db"
CONFIG_PATH = APP_DIR / "config.json"
SESSIONS_DIR = APP_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path.home() / "ClipCutter_Clips"
OUTPUT_DIR.mkdir(exist_ok=True)

# Video file extensions accepted by SnipCut (input) and right-click "Open With".
# The output is always CFR MP4 regardless of input codec.
SUPPORTED_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".avi",
                        ".ts", ".mts", ".flv", ".m4v", ".wmv")

# ---------- User config ----------

DEFAULT_CONFIG = {
    "api_key": "",
    "default_clip_window": 5,
    "target_duration": 60,
    "whisper_model": "base",
    "output_dir": str(OUTPUT_DIR),
    "channel_profile": "",
    "streambuddy_url": "",
    "streambuddy_token": "",
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except (json.JSONDecodeError, IOError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
