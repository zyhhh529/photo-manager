"""Config and state: ~/.photoman/config.json and ~/.photoman/state.json"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME_DIR = Path(os.environ.get("PHOTOMAN_HOME", Path.home() / ".photoman"))
CONFIG_PATH = HOME_DIR / "config.json"
STATE_PATH = HOME_DIR / "state.json"
MAX_RECENT_LIBRARIES = 5

# Nikon: NEF (RAW), NRW (RAW on some compacts), JPG, HEIF, video MOV/MP4
DEFAULT_EXTENSIONS = [
    ".nef", ".nrw", ".jpg", ".jpeg", ".hif", ".heic",
    ".tif", ".tiff", ".dng", ".mov", ".mp4",
]

DEFAULT_CONFIG = {
    # Import destination (usually a folder on an external drive), e.g. /Volumes/PhotoA/Photos
    "library_root": None,
    # Recently used import destinations, most recent first (shown in the destination menus)
    "recent_libraries": [],
    # Every import destination ever used (Trips window and catch-up backups look at all of them)
    "known_libraries": [],
    # Optional: travel backup drive; when connected, imports write an extra copy there
    "travel_backup_root": None,
    "extensions": DEFAULT_EXTENSIONS,
    # Which originals go where when the camera saves RAW + JPEG:
    #   travel backup: "raw" = skip JPEGs that have a RAW twin (JPEG-only shots and videos are kept), or "all"
    #   Baidu Netdisk: "jpeg" (JPEG/HEIF only), "raw", "all" or "none". Edited exports are always included.
    "backup_originals": "raw",
    "cloud_originals": "jpeg",
    # Baidu Netdisk folder to upload into, e.g. "/照片备份". Empty: the app's own folder,
    # 我的应用数据/<app name> (/apps/<app name>)
    "cloud_root": "",
    # Folders for imported photos: "trip" = one per trip (2026/2026-09-05_Rome), "day" = one per shooting day
    "group_by": "trip",
    # Within this many days of the last import, the trip name defaults to the previous one
    "trip_name_reuse_days": 3,
}


def _read_json(path: Path, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(default)
        merged.update(data)
        return merged
    except FileNotFoundError:
        return dict(default)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = _read_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not CONFIG_PATH.exists():
        _write_json(CONFIG_PATH, cfg)
    return cfg


def save_config(cfg: dict) -> None:
    _write_json(CONFIG_PATH, cfg)


def remember_library(cfg: dict, path) -> None:
    """Make `path` the current import destination and move it to the front of the recent list."""
    path = str(path).rstrip("/") or "/"
    recent = [p for p in cfg.get("recent_libraries") or [] if p != path]
    cfg["library_root"] = path
    cfg["recent_libraries"] = [path, *recent][:MAX_RECENT_LIBRARIES]
    cfg["known_libraries"] = known_libraries(cfg)


def known_libraries(cfg: dict) -> list:
    """All import destinations ever used, including ones that have dropped off the recent list."""
    known = list(cfg.get("known_libraries") or [])
    for p in [cfg.get("library_root"), *(cfg.get("recent_libraries") or [])]:
        if p and p not in known:
            known.append(p)
    return known


def load_state() -> dict:
    return _read_json(STATE_PATH, {})


def save_state(state: dict) -> None:
    _write_json(STATE_PATH, state)
