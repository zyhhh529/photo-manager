"""SD card import core: scan → read capture dates → archive into YYYY/YYYY-MM-DD_trip/ → per-file SHA-256 verification.

Design principles:
- The SD card is read-only: files on it are never modified or deleted.
- Files are first written as .part temp files and renamed only after verification;
  on verification failure the temp file is deleted.
- The library holds an index (<library>/.photoman/index.sqlite) of imported file hashes,
  so re-inserting a card never imports the same photo twice.
- "Safe to format" is reported only when every file on the card is in the library
  (newly copied and verified, or previously imported with a matching hash).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

CHUNK = 4 * 1024 * 1024
META_DIR = ".photoman"
EDITED_SUFFIX = "_Edited"   # 2026/2026-09-05_Rome_Edited/: Lightroom exports for that trip
SETTLE_SECONDS = 60
DATE_BATCH = 250            # files per exiftool run when reading capture dates         # edited files changed more recently than this may still be being exported


# ---------------------------------------------------------------- scanning

def is_camera_card(mount: Path) -> bool:
    """Treat any volume with a DCIM folder as a camera card."""
    return (mount / "DCIM").is_dir()


def scan_media(source: Path, extensions: Iterable[str]) -> List[Path]:
    exts = {e.lower() for e in extensions}
    root = source / "DCIM" if (source / "DCIM").is_dir() else source
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):  # skip macOS ._ metadata files
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in exts:
                files.append(p)
    files.sort()
    return files


# ---------------------------------------------------------------- capture dates

_DATE_TAGS = ["DateTimeOriginal", "CreateDate", "MediaCreateDate"]
_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def _parse_exif_date(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()))
    except ValueError:  # e.g. 0000:00:00 00:00:00
        return None
    return dt if dt.year >= 1990 else None


def exiftool_path() -> Optional[str]:
    for cand in ("exiftool", "/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def read_capture_dates(files: List[Path]) -> Dict[Path, datetime]:
    """Batch-read capture times with exiftool; files without one fall back to the modification time
    (when a camera writes to a FAT/exFAT card, that is the camera clock's local time)."""
    result: Dict[Path, datetime] = {}
    tool = exiftool_path()
    if tool and files:
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
            for f in files:
                af.write(str(f) + "\n")
            argfile = af.name
        try:
            out = subprocess.run(
                [tool, "-json", "-charset", "filename=utf8", "-fast2",
                 *[f"-{t}" for t in _DATE_TAGS], "-@", argfile],
                # explicit UTF-8: the login item may run with an ASCII default encoding, and exiftool's
                # output contains non-ASCII text (file names, camera strings)
                capture_output=True, encoding="utf-8", errors="replace", check=False,
            ).stdout
            for rec in json.loads(out or "[]"):
                src = Path(rec.get("SourceFile", ""))
                for tag in _DATE_TAGS:
                    dt = _parse_exif_date(rec.get(tag))
                    if dt:
                        result[src] = dt
                        break
        except (json.JSONDecodeError, OSError):
            pass
        finally:
            os.unlink(argfile)
    for f in files:
        if f not in result:
            result[f] = datetime.fromtimestamp(f.stat().st_mtime)
    return result


# ---------------------------------------------------------------- destination paths

def sanitize_trip(name: Optional[str]) -> str:
    if not name:
        return ""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name.strip())
    return name.strip(". ")[:60]


def is_writable(path: Path) -> bool:
    """False for read-only folders and drives (e.g. NTFS, which macOS mounts read-only)."""
    return os.access(path, os.W_OK)


def dest_folder(library_root: Path, dt: datetime, trip: str) -> Path:
    day = f"{dt:%Y-%m-%d}"
    return library_root / f"{dt:%Y}" / (f"{day}_{trip}" if trip else day)


# ---------------------------------------------------------------- hashing and index

# ---------------------------------------------------------------- RAW + JPEG pairs

RAW_EXTS = {".nef", ".nrw", ".dng", ".cr2", ".cr3", ".arw", ".raf", ".orf", ".rw2"}
JPEG_EXTS = {".jpg", ".jpeg", ".hif", ".heic", ".heif"}   # images the camera already processed


def raw_keys(paths: Iterable) -> set:
    """(folder, stem) of every RAW file, to find the JPEGs shot alongside them."""
    return {(str(Path(p).parent), Path(p).stem.lower()) for p in paths if Path(p).suffix.lower() in RAW_EXTS}


def has_raw_twin(path, keys: set) -> bool:
    p = Path(path)
    return p.suffix.lower() in JPEG_EXTS and (str(p.parent), p.stem.lower()) in keys


def wanted_on_drive(path, keys: set, policy: str = "all") -> bool:
    """Travel backup policy for originals. "raw": skip JPEGs that have a RAW twin (everything else, including
    JPEG-only shots and videos, is still backed up). "all": back up everything."""
    return policy == "all" or not has_raw_twin(path, keys)


def wanted_in_cloud(path, policy: str = "jpeg") -> bool:
    """Cloud policy for originals: "jpeg" (JPEG/HEIF only), "raw", "all" or "none"."""
    ext = Path(path).suffix.lower()
    return policy == "all" or (policy == "jpeg" and ext in JPEG_EXTS) or (policy == "raw" and ext in RAW_EXTS)


TRIP_GAP_DAYS = 30  # imports with the same trip name this close together are the same trip


def edited_folder_for(date_folder: Path) -> Path:
    """The Edited folder for a photos folder: right next to it, e.g. 2026/2026-09-05_Rome_Edited."""
    date_folder = Path(date_folder)
    return date_folder.with_name(date_folder.name + EDITED_SUFFIX)


def trip_folder(library_root: Path, first_day: datetime, trip: str) -> Path:
    """The one folder a trip's photos go in: <year>/<first day>_<trip>, e.g. 2026/2026-09-05_Rome (just the date
    when there's no trip name). A later card from the same trip (same name, within TRIP_GAP_DAYS of an existing
    folder or its Edited folder) goes into that existing folder."""
    new = dest_folder(library_root, first_day, trip)
    if not trip:
        return new
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_" + re.escape(trip) + "(" + re.escape(EDITED_SUFFIX) + ")?$")
    candidates = []
    for p in Path(library_root).glob("*/*"):
        m = pattern.match(p.name)
        if m and p.is_dir():
            gap = abs((datetime.strptime(m.group(1), "%Y-%m-%d") - first_day.replace(hour=0, minute=0, second=0,
                                                                                  microsecond=0)).days)
            if gap <= TRIP_GAP_DAYS:
                candidates.append((gap, str(p.parent / f"{m.group(1)}_{trip}")))
    return Path(min(candidates)[1]) if candidates else new


def planned_folders(library_root: Path, dates: Iterable[datetime], trip: str, group_by: str = "trip") -> List[Path]:
    """Folders an import of photos taken at `dates` will use: one per trip, or one per day."""
    dates = list(dates)
    if not dates:
        return []
    trip = sanitize_trip(trip)
    if group_by == "trip":
        return [trip_folder(library_root, min(dates), trip)]
    return sorted({dest_folder(library_root, d, trip) for d in dates})


def trip_edited_folder(library_root: Path, first_date_folder: Path, trip: str) -> Path:
    """Edited folder for an import. A later card from the same trip (same name, within TRIP_GAP_DAYS)
    reuses the trip's existing Edited folder, so each trip has exactly one."""
    new = edited_folder_for(first_date_folder)
    if not trip:
        return new
    day = datetime.strptime(Path(first_date_folder).name[:10], "%Y-%m-%d")
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_" + re.escape(trip) + re.escape(EDITED_SUFFIX) + "$")
    candidates = []
    for p in Path(library_root).glob("*/*" + EDITED_SUFFIX):
        m = pattern.match(p.name)
        if m and p.is_dir():
            gap = abs((datetime.strptime(m.group(1), "%Y-%m-%d") - day).days)
            if gap <= TRIP_GAP_DAYS:
                candidates.append((gap, p))
    return min(candidates)[1] if candidates else new


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def copy_and_hash(src: Path, dst_tmp: Path) -> str:
    """Hash the source while copying, so the SD card is read only once."""
    h = hashlib.sha256()
    dst_tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fi, open(dst_tmp, "wb") as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            h.update(b)
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, dst_tmp)
    return h.hexdigest()


class Index:
    """<library>/.photoman/index.sqlite: records imported files, and which of them have a verified copy
    on which travel backup drive. It lives inside the library, so it moves along with it."""

    def __init__(self, library_root: Path, readonly: bool = False):
        self.path = library_root / META_DIR / "index.sqlite"
        if readonly and not self.path.exists():
            self.db = sqlite3.connect(":memory:")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=30)  # the cloud uploader may be using it too
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS files (
                   sha256 TEXT PRIMARY KEY,
                   rel_path TEXT NOT NULL,
                   size INTEGER NOT NULL,
                   orig_name TEXT NOT NULL,
                   captured_at TEXT NOT NULL,
                   imported_at TEXT NOT NULL)"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS sig ON files(orig_name, size, captured_at)")
        # gone = 1: deleted from the library (not just moved; see reconcile). Added after the first release.
        if "gone" not in {r[1] for r in self.db.execute("PRAGMA table_info(files)")}:
            self.db.execute("ALTER TABLE files ADD COLUMN gone INTEGER NOT NULL DEFAULT 0")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS backups (
                   sha256 TEXT NOT NULL,
                   backup_root TEXT NOT NULL,
                   rel_path TEXT NOT NULL,
                   backed_up_at TEXT NOT NULL,
                   PRIMARY KEY (sha256, backup_root))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS cloud_uploads (
                   rel_path TEXT NOT NULL,
                   remote_root TEXT NOT NULL,
                   size INTEGER NOT NULL,
                   mtime REAL NOT NULL,
                   fs_id TEXT,
                   uploaded_at TEXT NOT NULL,
                   PRIMARY KEY (rel_path, remote_root))"""
        )

    def by_hash(self, sha: str) -> Optional[str]:
        row = self.db.execute("SELECT rel_path FROM files WHERE sha256=?", (sha,)).fetchone()
        return row[0] if row else None

    def by_name_and_size(self, name: str, size: int) -> Optional[tuple]:
        """Looser match for card files left out of a partial import (their capture time isn't read)."""
        return self.db.execute("SELECT sha256, rel_path FROM files WHERE orig_name=? AND size=? AND gone=0",
                               (name, size)).fetchone()

    def by_signature(self, name: str, size: int, captured: datetime) -> Optional[tuple]:
        row = self.db.execute(
            "SELECT sha256, rel_path FROM files WHERE orig_name=? AND size=? AND captured_at=?",
            (name, size, captured.isoformat()),
        ).fetchone()
        return row

    def add(self, sha: str, rel_path: str, size: int, name: str, captured: datetime) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files (sha256, rel_path, size, orig_name, captured_at, imported_at) "
            "VALUES (?,?,?,?,?,?)",  # replaces rows whose file was deleted
            (sha, rel_path, size, name, captured.isoformat(), datetime.now().isoformat(timespec="seconds")),
        )

    def all_files(self) -> List[tuple]:
        """(sha, rel_path) of imported files, except ones deleted from the library."""
        return self.db.execute("SELECT sha256, rel_path FROM files WHERE gone=0 ORDER BY rel_path").fetchall()

    def relink(self, sha: str, old_rel: str, new_rel: str) -> None:
        """The file was moved or renamed inside the library. Cloud upload records move along, so it isn't
        uploaded again (the cloud copy stays where it was uploaded)."""
        self.db.execute("UPDATE files SET rel_path=?, gone=0 WHERE sha256=?", (new_rel, sha))
        self.db.execute("UPDATE OR REPLACE cloud_uploads SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))

    def mark_gone(self, sha: str) -> None:
        self.db.execute("UPDATE files SET gone=1 WHERE sha256=?", (sha,))

    def backup_of(self, sha: str, backup_root: Path) -> Optional[str]:
        row = self.db.execute("SELECT rel_path FROM backups WHERE sha256=? AND backup_root=?",
                              (sha, str(backup_root))).fetchone()
        return row[0] if row else None

    def mark_backed_up(self, sha: str, backup_root: Path, rel_path: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO backups VALUES (?,?,?,?)",
                        (sha, str(backup_root), rel_path, datetime.now().isoformat(timespec="seconds")))

    def cloud_of(self, rel_path: str, remote_root: str) -> Optional[tuple]:
        """(size, mtime) of the version of `rel_path` last uploaded under `remote_root` in Baidu Netdisk."""
        return self.db.execute("SELECT size, mtime FROM cloud_uploads WHERE rel_path=? AND remote_root=?",
                               (rel_path, remote_root)).fetchone()

    def cloud_uploads(self, remote_root: str) -> dict:
        return {r[0]: (r[1], r[2]) for r in self.db.execute(
            "SELECT rel_path, size, mtime FROM cloud_uploads WHERE remote_root=?", (remote_root,))}

    def mark_cloud(self, rel_path: str, remote_root: str, size: int, mtime: float, fs_id: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO cloud_uploads VALUES (?,?,?,?,?,?)",
                        (rel_path, remote_root, size, mtime, fs_id, datetime.now().isoformat(timespec="seconds")))

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()


# ---------------------------------------------------------------- import

@dataclass
class FileResult:
    source: str
    status: str  # copied / already_imported / duplicate / failed / would_copy / not_selected
    dest: Optional[str] = None
    sha: Optional[str] = None
    captured: Optional[str] = None   # capture time (ISO), so trips know their dates whatever the folders
    error: Optional[str] = None
    backup_ok: Optional[bool] = None


@dataclass
class ImportResult:
    source: str
    library_root: str
    trip: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    total: int = 0
    copied: int = 0
    already_imported: int = 0
    duplicate: int = 0
    failed: int = 0
    would_copy: int = 0
    not_selected: int = 0               # left on the card by a partial import
    stopped: bool = False               # stopped by the user before every file was handled
    bytes_copied: int = 0
    backup_root: Optional[str] = None    # configured travel backup, whether or not it's connected
    backup_error: Optional[str] = None   # why the backup couldn't run ("not connected", "read-only")
    backed_up: int = 0                   # card files with a verified copy on the backup drive
    backup_skipped: int = 0              # not needed on the backup drive (JPEGs with a RAW twin, "raw" policy)
    backup_failed: int = 0
    folders: List[str] = field(default_factory=list)
    edited_folder: Optional[str] = None  # empty folder created for this trip's Lightroom exports
    files: List[FileResult] = field(default_factory=list)

    @property
    def in_library(self) -> int:
        return self.copied + self.already_imported + self.duplicate

    @property
    def backup_pending(self) -> int:
        """Card files that still need a copy on the travel backup drive."""
        if not self.backup_root or self.dry_run:
            return 0
        return self.in_library - self.backed_up - self.backup_skipped

    @property
    def safe_to_format(self) -> bool:
        """Every file on the card is safely in the photo library, and on the travel backup drive if one is set."""
        return (not self.dry_run and not self.stopped and self.total > 0 and self.failed == 0
                and self.in_library == self.total and self.backup_pending == 0)

    def summary(self) -> str:
        if self.dry_run:
            return (f"[dry run] {self.total} files: {self.would_copy} to import, "
                    f"{self.already_imported} already imported")
        s = (f"{self.total} files: {self.copied} imported, {self.already_imported} already imported, "
             f"{self.duplicate} duplicates, {self.failed} failed")
        if self.not_selected:
            s += f", {self.not_selected} not selected"
        if self.stopped:
            s += f", stopped with {self.total - len(self.files)} files not looked at"
        if self.backup_root and self.backup_error:
            s += f"; travel backup {self.backup_error}"
        elif self.backup_root:
            s += f"; {self.backed_up} backed up"
            if self.backup_skipped:
                s += f" ({self.backup_skipped} JPEGs with RAW skipped)"
            if self.backup_failed:
                s += f", {self.backup_failed} backup failures"
        return s

    def to_dict(self) -> dict:
        d = asdict(self)
        d["safe_to_format"] = self.safe_to_format
        d["backup_pending"] = self.backup_pending
        return d


ProgressCB = Callable[[int, int, str], None]


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def backup_problem(backup_root: Path) -> Optional[str]:
    if not Path(backup_root).is_dir():
        return "not connected"
    if not is_writable(Path(backup_root)):
        return "read-only"
    return None


def ensure_backed_up(index: Index, library_root: Path, backup_root: Path, sha: str, rel: str) -> bool:
    """Make sure the library file `rel` (content `sha`) has a verified copy under the same relative path on the
    travel backup drive. Copies are only ever added there, never deleted."""
    done = index.backup_of(sha, backup_root)
    if done and (backup_root / done).is_file():
        return True
    tmp = None
    try:
        dst = backup_root / rel
        if not (dst.is_file() and sha256_file(dst) == sha):
            dst = _unique_dest(dst)
            tmp = dst.with_name(dst.name + ".part")
            # the library copy must still match what was imported, and the backup must match it
            if copy_and_hash(library_root / rel, tmp) != sha or sha256_file(tmp) != sha:
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, dst)
        index.mark_backed_up(sha, backup_root, str(dst.relative_to(backup_root)))
        index.commit()
        return True
    except OSError:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return False


def with_partners(selected: Iterable, card_files: Iterable) -> set:
    """Selected card files plus their RAW/JPEG partners (same folder and name), so pairs stay together."""
    keys = {(str(Path(p).parent), Path(p).stem.lower()) for p in selected}
    return {str(p) for p in card_files if (str(Path(p).parent), Path(p).stem.lower()) in keys} | {str(p) for p in selected}


def reconcile(index: Index, library_root: Path) -> Tuple[int, int]:
    """Find imported files that are no longer at their recorded path: moved or renamed ones are found again by
    size and SHA-256 and relinked; the rest are marked gone (deleted from the library). Returns (moved, gone)."""
    library_root = Path(library_root)
    rows = index.db.execute("SELECT sha256, rel_path, size, gone FROM files").fetchall()
    missing = {sha: rel for sha, rel, size, gone in rows if not (library_root / rel).is_file()}
    for sha, rel, size, gone in rows:  # a "gone" file that is back where it was
        if gone and sha not in missing:
            index.db.execute("UPDATE files SET gone=0 WHERE sha256=?", (sha,))
    if not missing:
        index.commit()
        return 0, 0
    sizes = {size for sha, rel, size, gone in rows if sha in missing}
    known = {rel for sha, rel, size, gone in rows if sha not in missing}
    moved = 0
    for dirpath, dirnames, filenames in os.walk(library_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.endswith(".part"):
                continue
            path = Path(dirpath) / name
            rel = str(path.relative_to(library_root))
            try:
                if rel in known or path.stat().st_size not in sizes:
                    continue
                sha = sha256_file(path)
            except OSError:
                continue
            if sha in missing:
                index.relink(sha, missing.pop(sha), rel)
                known.add(rel)
                moved += 1
        if not missing:
            break
    for sha in missing:
        index.mark_gone(sha)
    index.commit()
    return moved, len(missing)


def reconcile_library(library_root: Path) -> Tuple[int, int]:
    """reconcile() if any imported file isn't where the index says (cheap check first)."""
    library_root = Path(library_root)
    if not (library_root / META_DIR / "index.sqlite").exists():
        return 0, 0
    index = Index(library_root)
    try:
        if all((library_root / rel).is_file() for _, rel in index.all_files()):
            return 0, 0
        return reconcile(index, library_root)
    finally:
        index.close()


def run_import(
    source: Path,
    library_root: Path,
    trip: Optional[str] = None,
    extensions: Iterable[str] = (),
    dry_run: bool = False,
    backup_root: Optional[Path] = None,
    progress: Optional[ProgressCB] = None,
    backup_policy: str = "all",
    only: Optional[Iterable] = None,
    group_by: str = "day",
    should_stop: Optional[Callable[[], bool]] = None,
) -> ImportResult:
    """Import `source` into `library_root`. With `only`, just those card files are copied; the rest are
    checked (already imported?) but left on the card, so the card isn't reported safe to format.
    group_by: "trip" puts everything in one folder per trip (2026/2026-09-05_Rome), "day" one per shooting day.
    should_stop: checked between files; when it returns True the import stops (finished files are kept)."""
    source, library_root = Path(source), Path(library_root)
    trip = sanitize_trip(trip)
    res = ImportResult(
        source=str(source), library_root=str(library_root), trip=trip,
        dry_run=dry_run, started_at=datetime.now().isoformat(timespec="seconds"),
    )
    if not library_root.is_dir():
        raise FileNotFoundError(f"Photo library missing or drive not connected: {library_root}")
    if not dry_run and not is_writable(library_root):
        raise PermissionError(f"Destination is read-only: {library_root}")
    if backup_root is not None:
        backup_root = Path(backup_root)
        res.backup_root = str(backup_root)
        res.backup_error = backup_problem(backup_root)  # not connected → back up later, card isn't safe to format yet
    can_back_up = backup_root is not None and not res.backup_error and not dry_run

    files = scan_media(source, extensions)
    res.total = len(files)
    selected = None if only is None else {str(Path(p)) for p in only}
    # Capture times only for the files being imported (exiftool reads ~50 files/s from a card), in batches so
    # the progress shows something instead of sitting at "Starting…" on a big card.
    need = [f for f in files if selected is None or str(f) in selected]
    dates: Dict[Path, datetime] = {}
    for start in range(0, len(need), DATE_BATCH):
        if progress:
            progress(start, len(need), "Reading capture dates…")
        dates.update(read_capture_dates(need[start:start + DATE_BATCH]))
    index = Index(library_root, readonly=dry_run)
    folders = set()
    card_raws = raw_keys(files)
    reconciled = False
    import_dates = [dates[f] for f in files if selected is None or str(f) in selected]
    the_trip_folder = (trip_folder(library_root, min(import_dates), trip)
                       if group_by == "trip" and import_dates else None)

    def in_library(rel: Optional[str]) -> bool:
        """Is the indexed file still there? If not, it may have been moved or renamed: look for it by content
        once per import, so reorganizing the library doesn't make a card import everything again."""
        nonlocal reconciled
        if rel and (library_root / rel).is_file():
            return True
        if rel and not reconciled and not dry_run:
            reconciled = True
            reconcile(index, library_root)
        return False

    def back_up(fr: FileResult, sha: str, rel: str) -> None:
        if not dry_run and backup_root is not None and not wanted_on_drive(fr.source, card_raws, backup_policy):
            res.backup_skipped += 1  # the RAW twin is what the backup drive keeps
            return
        if can_back_up:
            fr.backup_ok = ensure_backed_up(index, library_root, backup_root, sha, rel)
            if fr.backup_ok:
                res.backed_up += 1
            else:
                res.backup_failed += 1

    try:
        todo = [f for f in files if selected is None or str(f) in selected]
        done = 0
        for src in files:
            chosen = selected is None or str(src) in selected
            if chosen:
                if should_stop and should_stop():
                    res.stopped = True
                    break
                done += 1
                if progress:
                    progress(done, len(todo), src.name)
            captured = dates.get(src)   # None for files left out of a partial import
            size = src.stat().st_size
            fr = FileResult(source=str(src), status="", captured=captured.isoformat() if captured else None)
            res.files.append(fr)
            try:
                # 1) Fast check: same name, size and capture time → confirm with the hash.
                #    Only counts if the file is still in the library (it may have been deleted since).
                lookup = ((lambda: index.by_signature(src.name, size, captured)) if chosen
                          else (lambda: index.by_name_and_size(src.name, size)))
                sig = lookup()
                if sig and not in_library(sig[1]):
                    sig = lookup()  # found where it was moved to?
                if sig and (library_root / sig[1]).is_file():
                    # Files left out of a partial import are only matched by name and size: re-reading
                    # (hashing) them would mean reading the whole card just to skip them.
                    if dry_run or not chosen or sha256_file(src) == sig[0]:
                        fr.status, fr.dest, fr.sha = "already_imported", sig[1], sig[0]
                        res.already_imported += 1
                        back_up(fr, sig[0], sig[1])  # e.g. imported while the backup drive wasn't connected
                        continue

                if not chosen:
                    fr.status = "not_selected"  # stays on the card; a later import can pick it up
                    res.not_selected += 1
                    continue

                folder = the_trip_folder or dest_folder(library_root, captured, trip)
                if dry_run:
                    fr.status, fr.dest = "would_copy", str(folder / src.name)
                    res.would_copy += 1
                    folders.add(str(folder))
                    continue

                # 2) Copy to a temp file, hashing the source along the way
                dest = _unique_dest(folder / src.name)
                tmp = dest.with_name(dest.name + ".part")
                sha = copy_and_hash(src, tmp)

                # 3) Content identical to a file already in the library (e.g. renamed) → don't store a second copy
                existing = index.by_hash(sha)
                if existing and not in_library(existing):
                    existing = index.by_hash(sha)
                if existing and (library_root / existing).is_file():
                    tmp.unlink()
                    fr.status, fr.dest, fr.sha = "duplicate", existing, sha
                    res.duplicate += 1
                    back_up(fr, sha, existing)
                    continue

                # 4) Re-read the destination file to verify
                if sha256_file(tmp) != sha:
                    tmp.unlink(missing_ok=True)
                    raise IOError("Verification failed: copied file does not match the original")
                os.replace(tmp, dest)

                rel = str(dest.relative_to(library_root))
                index.add(sha, rel, size, src.name, captured)
                index.commit()
                fr.status, fr.dest, fr.sha = "copied", rel, sha
                res.copied += 1
                res.bytes_copied += size
                folders.add(str(folder))

                back_up(fr, sha, rel)
            except Exception as e:  # one file failing doesn't affect the others
                fr.status, fr.error = "failed", str(e)
                res.failed += 1
    finally:
        index.close()

    res.folders = sorted(folders)
    if res.folders and not dry_run:
        edited = (edited_folder_for(the_trip_folder) if the_trip_folder
                  else trip_edited_folder(library_root, Path(res.folders[0]), trip))
        try:
            edited.mkdir(exist_ok=True)
            res.edited_folder = str(edited)
        except OSError:
            pass  # not worth failing an otherwise good import over
    res.finished_at = datetime.now().isoformat(timespec="seconds")
    if not dry_run:
        _write_log(library_root, res)
    return res


def _write_log(library_root: Path, res: ImportResult) -> Path:
    log_dir = library_root / META_DIR / "imports"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_dest(log_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- catch-up backup

@dataclass
class BackupResult:
    library_root: str
    backup_root: str
    total: int = 0
    copied: int = 0            # newly backed up
    already: int = 0           # had a verified copy already
    failed: int = 0
    missing: int = 0           # found deleted from the library in this run (their backups are kept)
    moved: int = 0             # found moved or renamed inside the library (their backups stay put)
    skipped: int = 0           # not wanted on the backup drive (JPEGs with a RAW twin)
    edited_copied: int = 0     # new or re-exported files from *_Edited folders
    edited_already: int = 0
    edited_failed: int = 0

    def summary(self) -> str:
        s = f"{self.copied} originals backed up, {self.already} already backed up, {self.failed} failed"
        if self.moved:
            s += f", {self.moved} moved in the library"
        if self.missing:
            s += f", {self.missing} no longer in the library"
        s += f"; edited: {self.edited_copied} backed up, {self.edited_already} already backed up"
        if self.edited_failed:
            s += f", {self.edited_failed} failed"
        return s

    @property
    def copied_total(self) -> int:
        return self.copied + self.edited_copied

    @property
    def failed_total(self) -> int:
        return self.failed + self.edited_failed


def edited_folders(library_root: Path) -> List[Path]:
    """*_Edited folders up to three levels below the year folders (so they can be moved into subfolders);
    an Edited folder nested inside another counts once, as part of the outer one."""
    found = set()
    for depth in range(1, 4):
        found |= {p for p in Path(library_root).glob("*/" * depth + f"*{EDITED_SUFFIX}")
                  if p.is_dir() and not any(part.startswith(".") for part in p.relative_to(library_root).parts)}
    return sorted(p for p in found if not any(a in found for a in p.parents))


def edited_files(library_root: Path, settle: float = SETTLE_SECONDS):
    """(path, stat) of every file in the library's Edited folders, subfolders included, skipping hidden
    and temp files and ones modified in the last `settle` seconds (probably still being exported)."""
    for folder in edited_folders(library_root):
        yield from files_in(folder, settle)


def files_in(folder: Path, settle: float = 0):
    """(path, stat) of the files under `folder`, skipping hidden and temp files and ones modified in the
    last `settle` seconds."""
    now = time.time()
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.endswith(".part"):
                continue
            path = Path(dirpath) / name
            st = path.stat()
            if now - st.st_mtime >= settle:
                yield path, st


def same_file(st, dst: Path) -> bool:
    """Backup copy matches by size and modification time (copies keep the mtime; FAT-style drives
    store it with 2-second precision)."""
    try:
        d = dst.stat()
    except FileNotFoundError:
        return False
    return d.st_size == st.st_size and abs(d.st_mtime - st.st_mtime) <= 2


def _copy_verified(src: Path, dst: Path) -> bool:
    """Copy src over dst via a .part file, verifying the copy's SHA-256 before it replaces dst."""
    tmp = dst.with_name(dst.name + ".part")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        sha = copy_and_hash(src, tmp)
        if sha256_file(tmp) != sha:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, dst)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def _library_files(index: Index, library_root: Path):
    for sha, rel in index.all_files():
        yield sha, rel, (library_root / rel).is_file()


def pending_backups(library_root: Path, backup_root: Path, include_edited: bool = True, policy: str = "all") -> int:
    """Files in the library with no up-to-date copy on the backup drive: imported originals, plus
    (unless include_edited is False) files in the Edited folders."""
    library_root, backup_root = Path(library_root), Path(backup_root)
    n = 0
    if (library_root / META_DIR / "index.sqlite").exists():
        index = Index(library_root, readonly=True)
        try:
            rows = list(_library_files(index, library_root))
            keys = raw_keys(rel for _, rel, exists in rows)
            for sha, rel, exists in rows:
                if not wanted_on_drive(rel, keys, policy):
                    continue
                done = index.backup_of(sha, backup_root)
                # not at its recorded path and not backed up: probably moved; a backup run will find it
                if not (done and (backup_root / done).is_file()):
                    n += 1
        finally:
            index.close()
    if include_edited:
        n += sum(1 for path, st in edited_files(library_root)
                 if not same_file(st, backup_root / path.relative_to(library_root)))
    return n


def backup_library(library_root: Path, backup_root: Path, progress: Optional[ProgressCB] = None,
                   policy: str = "all") -> BackupResult:
    """Copy every imported file that isn't on the travel backup drive yet (e.g. imported while it wasn't
    connected). Never deletes anything on the backup drive."""
    library_root, backup_root = Path(library_root), Path(backup_root)
    if not library_root.is_dir():
        raise FileNotFoundError(f"Photo library missing or drive not connected: {library_root}")
    problem = backup_problem(backup_root)
    if problem:
        raise (FileNotFoundError if problem == "not connected" else PermissionError)(
            f"Travel backup {problem}: {backup_root}")
    res = BackupResult(library_root=str(library_root), backup_root=str(backup_root))
    index = Index(library_root)
    try:
        if not all(exists for _, _, exists in _library_files(index, library_root)):
            # files moved or renamed since import are found again by content; deleted ones are counted here
            res.moved, res.missing = reconcile(index, library_root)
        rows = list(_library_files(index, library_root))
        keys = raw_keys(rel for _, rel, exists in rows if exists)
        edited = list(edited_files(library_root))
        n = len(rows) + len(edited)
        res.total = n
        for i, (sha, rel, exists) in enumerate(rows, 1):
            if progress:
                progress(i, n, Path(rel).name)
            if not exists:
                res.missing += 1
                continue
            if not wanted_on_drive(rel, keys, policy):
                res.skipped += 1
                continue
            done = index.backup_of(sha, backup_root)
            if done and (backup_root / done).is_file():
                res.already += 1
            elif ensure_backed_up(index, library_root, backup_root, sha, rel):
                res.copied += 1
            else:
                res.failed += 1

        # Edited folders: new and re-exported files are copied (replacing the older export on the backup);
        # files deleted from the library stay on the backup.
        for i, (path, st) in enumerate(edited, len(rows) + 1):
            if progress:
                progress(i, n, path.name)
            dst = backup_root / path.relative_to(library_root)
            if same_file(st, dst):
                res.edited_already += 1
            elif _copy_verified(path, dst):
                res.edited_copied += 1
            else:
                res.edited_failed += 1
    finally:
        index.close()
    return res
