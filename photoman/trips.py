"""Trips: every import grouped by trip, with live editing and backup status.

Built from the import logs each destination keeps in <library>/.photoman/imports/, so it covers imports from
any day, whether or not the app was running since. Trips on drives that aren't connected are remembered in
~/.photoman/trips.json and shown as such.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import config
from .importer import (META_DIR, SETTLE_SECONDS, Index, backup_problem, edited_folder_for, files_in, raw_keys,
                       same_file, wanted_in_cloud, wanted_on_drive)

CACHE_PATH = config.HOME_DIR / "trips.json"


@dataclass
class Trip:
    library_root: str
    name: str                          # "" when archived by date only
    folders: List[str]                 # date folders, relative to library_root
    edited_folder: Optional[str]       # relative to library_root
    imported_at: str                   # when the last import for this trip finished
    imports: int = 1
    # live status (see fill_status)
    connected: bool = True
    photos: int = 0                    # imported originals still in the library
    edited: int = 0                    # files in the Edited folder
    last_export: Optional[float] = None
    originals_pending: Optional[int] = None   # None: no backup drive set, or it isn't usable right now
    edited_pending: Optional[int] = None
    cloud_pending: Optional[int] = None       # None: Baidu Netdisk isn't set up

    @property
    def title(self) -> str:
        return self.name or self.date_range

    @property
    def date_range(self) -> str:
        days = sorted(Path(f).name[:10] for f in self.folders)
        lo, hi = datetime.strptime(days[0], "%Y-%m-%d"), datetime.strptime(days[-1], "%Y-%m-%d")
        if lo == hi:
            return f"{lo:%b} {lo.day}, {lo.year}"
        if lo.year == hi.year:
            return f"{lo:%b} {lo.day} – {hi:%b} {hi.day}, {hi.year}"
        return f"{lo:%b} {lo.day}, {lo.year} – {hi:%b} {hi.day}, {hi.year}"

    @property
    def first_day(self) -> str:
        return min(Path(f).name[:10] for f in self.folders)

    @property
    def backup_pending(self) -> Optional[int]:
        if self.originals_pending is None:
            return None
        return self.originals_pending + (self.edited_pending or 0)

    def path(self, rel: str) -> Path:
        return Path(self.library_root) / rel


def _rel(path: str, library_root: str) -> str:
    try:
        return str(Path(path).relative_to(library_root))
    except ValueError:
        return path


def trips_from_logs(library_root: Path) -> List[Trip]:
    """Group a destination's import logs into trips (imports sharing an Edited folder are one trip)."""
    library_root = Path(library_root)
    trips = {}
    for log in sorted((library_root / META_DIR / "imports").glob("*.json")):
        try:
            d = json.loads(log.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("dry_run") or not d.get("folders"):
            continue  # nothing new was imported
        root = d.get("library_root") or str(library_root)  # paths in the log may predate a rename
        folders = [_rel(f, root) for f in d["folders"]]
        edited = d.get("edited_folder")
        edited = _rel(edited, root) if edited else str(edited_folder_for(Path(sorted(folders)[0])))
        t = trips.get(edited)
        if t:
            t.folders = sorted(set(t.folders) | set(folders))
            t.imports += 1
            t.imported_at = max(t.imported_at, d.get("finished_at", ""))
            t.name = t.name or d.get("trip", "")
        else:
            trips[edited] = Trip(library_root=str(library_root), name=d.get("trip", ""), folders=sorted(folders),
                                 edited_folder=edited, imported_at=d.get("finished_at", ""))
    return list(trips.values())


def fill_status(trips: List[Trip], library_root: Path, backup_root: Optional[Path], policy: str = "all",
                cloud_policy: Optional[str] = None) -> None:
    """Count photos, edits and pending backups for each trip of one (connected) destination."""
    library_root = Path(library_root)
    usable_backup = backup_root is not None and not backup_problem(backup_root)
    rows, backed, clouded = [], {}, {}
    if (library_root / META_DIR / "index.sqlite").exists():
        index = Index(library_root, readonly=True)
        try:
            rows = index.all_files()
            if usable_backup:
                backed = {sha: index.backup_of(sha, backup_root) for sha, _ in rows}
            if cloud_policy is not None:
                clouded = dict(((r[0], (r[1], r[2])) for r in
                                index.db.execute("SELECT rel_path, size, mtime FROM cloud")))
        finally:
            index.close()
    for t in trips:
        prefixes = tuple(f.rstrip("/") + "/" for f in t.folders)
        originals = [(sha, rel) for sha, rel in rows if rel.startswith(prefixes) and (library_root / rel).is_file()]
        t.photos = len(originals)
        edited_dir = t.path(t.edited_folder) if t.edited_folder else None
        edits = list(files_in(edited_dir)) if edited_dir and edited_dir.is_dir() else []
        t.edited = len(edits)
        t.last_export = max((st.st_mtime for _, st in edits), default=None)
        if usable_backup:
            keys = raw_keys(rel for _, rel in originals)
            t.originals_pending = sum(1 for sha, rel in originals if wanted_on_drive(rel, keys, policy)
                                      and not (backed.get(sha) and (backup_root / backed[sha]).is_file()))
            now = datetime.now().timestamp()
            t.edited_pending = sum(1 for p, st in edits if now - st.st_mtime >= SETTLE_SECONDS
                                   and not same_file(st, backup_root / p.relative_to(library_root)))
        else:
            t.originals_pending = t.edited_pending = None
        if cloud_policy is not None:
            def uploaded(rel, st):
                rec = clouded.get(rel)
                return rec and rec[0] == st.st_size and abs(rec[1] - st.st_mtime) <= 2
            now = datetime.now().timestamp()
            t.cloud_pending = sum(1 for _, rel in originals if wanted_in_cloud(rel, cloud_policy)
                                  and not uploaded(rel, (library_root / rel).stat()))
            t.cloud_pending += sum(1 for p, st in edits if now - st.st_mtime >= SETTLE_SECONDS
                                   and not uploaded(str(p.relative_to(library_root)), st))
        else:
            t.cloud_pending = None


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def load_trips(libraries: List[str], backup_root: Optional[Path], policy: str = "all",
               cloud_policy: Optional[str] = None) -> List[Trip]:
    """All trips across the given destinations, newest first. Connected destinations are read live
    (and cached); disconnected ones come from the cache with connected=False."""
    cache = _load_cache()
    out = []
    for lib in libraries:
        if Path(lib).is_dir():
            trips = trips_from_logs(Path(lib))
            fill_status(trips, Path(lib), backup_root, policy, cloud_policy)
            cache[lib] = [asdict(t) for t in trips]
        else:
            trips = [Trip(**{**d, "connected": False}) for d in cache.get(lib, [])]
        out.extend(trips)
    _save_cache(cache)
    return sorted(out, key=lambda t: (t.first_day, t.imported_at), reverse=True)
