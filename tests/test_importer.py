import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from photoman import importer
from photoman.importer import run_import, scan_media, sanitize_trip

EXTS = [".nef", ".jpg", ".mov"]
HAS_EXIFTOOL = importer.exiftool_path() is not None


def _jpg(path: Path, when: str = None, color=(200, 50, 50)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "JPEG")
    if when and HAS_EXIFTOOL:
        subprocess.run([importer.exiftool_path(), "-q", "-overwrite_original",
                        f"-DateTimeOriginal={when}", str(path)], check=True)


def _raw(path: Path, content: bytes, mtime: datetime):
    """Fake NEF: exiftool finds no date, so the file modification time is used."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))


@pytest.fixture
def card(tmp_path):
    c = tmp_path / "NIKON_Z6"
    d = c / "DCIM" / "100NZ6_2"
    _jpg(d / "DSC_0001.JPG", "2026:09:05 10:00:00")
    _jpg(d / "DSC_0002.JPG", "2026:09:06 23:59:00", color=(10, 200, 10))
    _raw(d / "DSC_0001.NEF", os.urandom(50_000), datetime(2026, 9, 5, 10, 0, 0))
    _raw(d / "DSC_0003.MOV", os.urandom(80_000), datetime(2026, 9, 7, 8, 30, 0))
    (d / "._DSC_0001.JPG").write_bytes(b"appledouble")   # macOS metadata, should be ignored
    (c / "DCIM" / "NIKON001.DSC").write_bytes(b"x")       # non-media file, should be ignored
    return c


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "PhotoA" / "Photos"
    lib.mkdir(parents=True)
    return lib


def _snapshot(p: Path):
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


def test_scan_ignores_hidden_and_non_media(card):
    names = [p.name for p in scan_media(card, EXTS)]
    assert names == ["DSC_0001.JPG", "DSC_0001.NEF", "DSC_0002.JPG", "DSC_0003.MOV"]


def test_sanitize_trip():
    assert sanitize_trip(" Kyoto/Osaka ") == "Kyoto_Osaka"
    assert sanitize_trip(None) == ""


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="requires exiftool")
def test_import_groups_by_capture_date_and_verifies(card, library):
    before = _snapshot(card)
    res = run_import(card, library, "Europe", EXTS)
    assert res.total == 4 and res.copied == 4 and res.failed == 0
    assert res.safe_to_format
    got = sorted(str(p.relative_to(library)) for p in library.rglob("*")
                 if p.is_file() and ".photoman" not in p.parts)
    assert got == [
        "2026/2026-09-05_Europe/DSC_0001.JPG",
        "2026/2026-09-05_Europe/DSC_0001.NEF",
        "2026/2026-09-06_Europe/DSC_0002.JPG",
        "2026/2026-09-07_Europe/DSC_0003.MOV",
    ]
    # content matches
    src = card / "DCIM" / "100NZ6_2" / "DSC_0001.NEF"
    assert (library / "2026/2026-09-05_Europe/DSC_0001.NEF").read_bytes() == src.read_bytes()
    # SD card was not modified
    assert _snapshot(card) == before
    # no leftover temp files, and a log was written
    assert not list(library.rglob("*.part"))
    assert len(list((library / ".photoman" / "imports").glob("*.json"))) == 1


def test_reimport_skips_already_imported(card, library):
    run_import(card, library, "Europe", EXTS)
    res = run_import(card, library, "OtherName", EXTS)
    assert res.copied == 0 and res.already_imported == 4
    assert res.safe_to_format
    assert not list(library.rglob("*OtherName*"))


def test_new_files_on_same_card_are_added(card, library):
    run_import(card, library, "Europe", EXTS)
    _raw(card / "DCIM" / "100NZ6_2" / "DSC_0004.NEF", os.urandom(1000), datetime(2026, 9, 8, 9, 0))
    res = run_import(card, library, "Europe", EXTS)
    assert res.copied == 1 and res.already_imported == 4


def test_same_name_different_content_does_not_overwrite(tmp_path, library):
    # Both cards have a DSC_0001.NEF shot on the same day, with different content (camera file numbering wrapped)
    c1, c2 = tmp_path / "c1", tmp_path / "c2"
    when = datetime(2026, 9, 10, 12, 0)
    _raw(c1 / "DCIM/100/DSC_0001.NEF", b"A" * 100, when)
    _raw(c2 / "DCIM/100/DSC_0001.NEF", b"B" * 200, when)
    run_import(c1, library, "Rome", EXTS)
    res = run_import(c2, library, "Rome", EXTS)
    assert res.copied == 1
    folder = library / "2026/2026-09-10_Rome"
    assert (folder / "DSC_0001.NEF").read_bytes() == b"A" * 100
    assert (folder / "DSC_0001_1.NEF").read_bytes() == b"B" * 200


def test_identical_content_under_other_name_is_duplicate(tmp_path, library):
    c1, c2 = tmp_path / "c1", tmp_path / "c2"
    when = datetime(2026, 9, 10, 12, 0)
    _raw(c1 / "DCIM/100/DSC_0001.NEF", b"same", when)
    _raw(c2 / "DCIM/100/COPY_0001.NEF", b"same", when)
    run_import(c1, library, "", EXTS)
    res = run_import(c2, library, "", EXTS)
    assert res.duplicate == 1 and res.copied == 0 and res.safe_to_format
    assert not (library / "2026/2026-09-10/COPY_0001.NEF").exists()


def test_dry_run_writes_nothing(card, library):
    res = run_import(card, library, "Europe", EXTS, dry_run=True)
    assert res.would_copy == 4 and not res.safe_to_format
    assert list(library.iterdir()) == []


def test_verification_failure_is_reported_and_cleaned(card, library, monkeypatch):
    real = importer.sha256_file

    def corrupt(path):
        if str(path).endswith("DSC_0002.JPG.part"):
            return "0" * 64
        return real(path)

    monkeypatch.setattr(importer, "sha256_file", corrupt)
    res = run_import(card, library, "Europe", EXTS)
    assert res.failed == 1 and res.copied == 3
    assert not res.safe_to_format
    assert not list(library.rglob("*.part"))
    assert not list(library.rglob("DSC_0002.JPG"))


def test_travel_backup_copy(card, library, tmp_path):
    backup = tmp_path / "TravelSSD" / "Photos"
    backup.mkdir(parents=True)
    res = run_import(card, library, "Europe", EXTS, backup_root=backup)
    assert res.backup_failed == 0
    lib_files = {p.relative_to(library) for p in library.rglob("*") if p.is_file() and ".photoman" not in p.parts}
    bak_files = {p.relative_to(backup) for p in backup.rglob("*") if p.is_file()}
    assert lib_files == bak_files


def test_missing_backup_drive_means_not_safe_to_format_yet(card, library, tmp_path):
    res = run_import(card, library, "Europe", EXTS, backup_root=tmp_path / "not-plugged-in")
    assert res.copied == 4 and res.failed == 0
    assert res.backup_error == "not connected" and res.backup_pending == 4
    assert not res.safe_to_format


def test_no_backup_configured_is_safe_to_format(card, library):
    res = run_import(card, library, "Europe", EXTS)
    assert res.backup_root is None and res.backup_pending == 0 and res.safe_to_format


def test_catch_up_backup_after_drive_is_connected(card, library, tmp_path):
    backup = tmp_path / "TravelSSD" / "Photos"
    run_import(card, library, "Europe", EXTS, backup_root=backup)   # drive not connected yet
    backup.mkdir(parents=True)
    assert importer.pending_backups(library, backup) == 4
    r = importer.backup_library(library, backup)
    assert (r.copied, r.already, r.failed) == (4, 0, 0)
    assert importer.pending_backups(library, backup) == 0
    lib_files = {p.relative_to(library): p.read_bytes() for p in library.rglob("*")
                 if p.is_file() and ".photoman" not in p.parts}
    assert {p.relative_to(backup): p.read_bytes() for p in backup.rglob("*") if p.is_file()} == lib_files
    # running it again copies nothing
    assert importer.backup_library(library, backup).already == 4
    # and re-inserting the card now reports it safe to format
    res = run_import(card, library, "Europe", EXTS, backup_root=backup)
    assert res.already_imported == 4 and res.backed_up == 4 and res.safe_to_format


def test_reimport_backs_up_files_imported_without_backup(card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    run_import(card, library, "Europe", EXTS)          # no backup configured then
    backup.mkdir()
    res = run_import(card, library, "Europe", EXTS, backup_root=backup)
    assert res.copied == 0 and res.backed_up == 4 and res.safe_to_format
    assert len([p for p in backup.rglob("*") if p.is_file()]) == 4


def test_backup_never_deletes(card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    run_import(card, library, "Europe", EXTS, backup_root=backup)
    gone = next(library.glob("2026/*/*.NEF"))
    gone.unlink()
    r = importer.backup_library(library, backup)
    assert r.missing == 1 and r.failed == 0
    assert (backup / gone.relative_to(library)).is_file()


def test_backup_failure_means_not_safe_to_format(card, library, tmp_path, monkeypatch):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    real = importer.sha256_file
    monkeypatch.setattr(importer, "sha256_file",
                        lambda p: "0" * 64 if str(p).startswith(str(backup)) else real(p))
    res = run_import(card, library, "Europe", EXTS, backup_root=backup)
    assert res.copied == 4 and res.backup_failed == 4 and not res.safe_to_format
    assert not list(backup.rglob("*.part"))


def test_read_only_backup_is_reported(card, library, tmp_path):
    backup = tmp_path / "NTFS"
    backup.mkdir()
    backup.chmod(0o555)
    try:
        res = run_import(card, library, "Europe", EXTS, backup_root=backup)
        assert res.backup_error == "read-only" and not res.safe_to_format
        with pytest.raises(PermissionError):
            importer.backup_library(library, backup)
    finally:
        backup.chmod(0o755)


def test_missing_library_raises(card, tmp_path):
    with pytest.raises(FileNotFoundError):
        run_import(card, tmp_path / "PhotoA-unplugged", "", EXTS)


def test_works_without_exiftool(card, library, monkeypatch):
    monkeypatch.setattr(importer, "exiftool_path", lambda: None)
    res = run_import(card, library, "", EXTS)
    assert res.copied == 4 and res.failed == 0


def test_read_only_destination_is_refused(card, tmp_path):
    lib = tmp_path / "ReadOnly"
    lib.mkdir()
    lib.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            run_import(card, lib, "", EXTS)
        assert list(lib.iterdir()) == []
        assert run_import(card, lib, "", EXTS, dry_run=True).would_copy == 4
    finally:
        lib.chmod(0o755)


def test_deleted_files_are_imported_again(card, library):
    run_import(card, library, "Europe", EXTS)
    for f in list(library.glob("2026/*/*")):
        f.unlink()
    res = run_import(card, library, "Europe", EXTS)
    assert res.copied == 4 and res.already_imported == 0 and res.safe_to_format
    # and the index now points at the new copies
    res = run_import(card, library, "Europe", EXTS)
    assert res.already_imported == 4 and res.copied == 0


def test_partly_deleted_import_restores_only_missing(card, library):
    run_import(card, library, "Europe", EXTS)
    next(library.glob("2026/*/*.NEF")).unlink()
    res = run_import(card, library, "Europe", EXTS)
    assert res.copied == 1 and res.already_imported == 3


# ---------------------------------------------------------------- Edited folders

def _export(path: Path, content: bytes, age: float = 3600):
    """A file as Lightroom would export it, last modified `age` seconds ago."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    ts = datetime.now().timestamp() - age
    os.utime(path, (ts, ts))


def test_import_creates_empty_edited_folder(card, library):
    res = run_import(card, library, "Europe", EXTS)
    edited = library / "2026" / "2026-09-05_Europe_Edited"
    assert res.edited_folder == str(edited)
    assert edited.is_dir() and list(edited.iterdir()) == []
    # importing the same card again neither fails nor treats it as a photo folder
    again = run_import(card, library, "Europe", EXTS)
    assert again.already_imported == 4 and again.safe_to_format


def test_dry_run_creates_no_edited_folder(card, library):
    res = run_import(card, library, "Europe", EXTS, dry_run=True)
    assert res.edited_folder is None and list(library.iterdir()) == []


def test_edited_files_are_backed_up_with_subfolders(card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    run_import(card, library, "Europe", EXTS, backup_root=backup)
    edited = library / "2026" / "2026-09-05_Europe_Edited"
    _export(edited / "DSC_0001.jpg", b"edit-1")
    _export(edited / "Instagram" / "DSC_0001-ig.jpg", b"edit-1-ig")
    _export(edited / ".DS_Store", b"junk")
    assert importer.pending_backups(library, backup) == 2
    assert importer.pending_backups(library, backup, include_edited=False) == 0

    r = importer.backup_library(library, backup)
    assert (r.edited_copied, r.edited_already, r.edited_failed) == (2, 0, 0)
    bak = backup / "2026" / "2026-09-05_Europe_Edited"
    assert (bak / "DSC_0001.jpg").read_bytes() == b"edit-1"
    assert (bak / "Instagram" / "DSC_0001-ig.jpg").read_bytes() == b"edit-1-ig"
    assert not (bak / ".DS_Store").exists()
    assert importer.pending_backups(library, backup) == 0
    assert importer.backup_library(library, backup).edited_already == 2


def test_reexported_file_replaces_backup_and_deleted_one_is_kept(card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    run_import(card, library, "Europe", EXTS, backup_root=backup)
    edited = library / "2026" / "2026-09-05_Europe_Edited"
    _export(edited / "a.jpg", b"v1", age=7200)
    _export(edited / "b.jpg", b"keep me", age=7200)
    importer.backup_library(library, backup)

    _export(edited / "a.jpg", b"version 2", age=3600)   # re-exported
    (edited / "b.jpg").unlink()                         # deleted in the library
    assert importer.pending_backups(library, backup) == 1
    r = importer.backup_library(library, backup)
    assert r.edited_copied == 1
    bak = backup / "2026" / "2026-09-05_Europe_Edited"
    assert (bak / "a.jpg").read_bytes() == b"version 2"
    assert (bak / "b.jpg").read_bytes() == b"keep me"


def test_files_still_being_exported_wait(card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    run_import(card, library, "Europe", EXTS, backup_root=backup)
    _export(library / "2026" / "2026-09-05_Europe_Edited" / "new.jpg", b"x", age=5)
    assert importer.pending_backups(library, backup) == 0
    assert importer.backup_library(library, backup).edited_copied == 0


def test_edited_folder_does_not_block_safe_to_format(card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    run_import(card, library, "Europe", EXTS, backup_root=backup)   # backup not connected
    backup.mkdir()
    importer.backup_library(library, backup)
    _export(library / "2026" / "2026-09-05_Europe_Edited" / "later.jpg", b"x")  # new export, not backed up
    res = run_import(card, library, "Europe", EXTS, backup_root=backup)
    assert res.safe_to_format


# ---------------------------------------------------------------- RAW + JPEG policy

def test_policy_helpers():
    keys = importer.raw_keys(["2026/a/DSC_1.NEF", "2026/a/DSC_2.nef"])
    assert importer.has_raw_twin("2026/a/DSC_1.JPG", keys)
    assert not importer.has_raw_twin("2026/a/DSC_3.JPG", keys)          # JPEG-only shot
    assert not importer.has_raw_twin("2026/b/DSC_1.JPG", keys)          # other folder
    assert not importer.wanted_on_drive("2026/a/DSC_1.JPG", keys, "raw")
    assert importer.wanted_on_drive("2026/a/DSC_1.NEF", keys, "raw")
    assert importer.wanted_on_drive("2026/a/DSC_3.JPG", keys, "raw")
    assert importer.wanted_on_drive("2026/a/MOV_1.MOV", keys, "raw")
    assert importer.wanted_on_drive("2026/a/DSC_1.JPG", keys, "all")
    assert importer.wanted_in_cloud("x/DSC_1.JPG", "jpeg") and importer.wanted_in_cloud("x/a.HIF", "jpeg")
    assert not importer.wanted_in_cloud("x/DSC_1.NEF", "jpeg") and not importer.wanted_in_cloud("x/a.MOV", "jpeg")
    assert importer.wanted_in_cloud("x/DSC_1.NEF", "raw") and not importer.wanted_in_cloud("x/DSC_1.JPG", "none")


@pytest.fixture
def pair_card(tmp_path):
    """DSC_0001.JPG + DSC_0001.NEF (a RAW + JPEG pair), DSC_0002.JPG (JPEG only), DSC_0003.MOV (video)."""
    d = tmp_path / "NIKON_Z6" / "DCIM" / "100NZ6_2"
    when = datetime(2026, 9, 5, 10, 0, 0)
    _raw(d / "DSC_0001.JPG", b"jpeg-1", when)
    _raw(d / "DSC_0001.NEF", b"raw-1", when)
    _raw(d / "DSC_0002.JPG", b"jpeg-2", when)
    _raw(d / "DSC_0003.MOV", b"video", when)
    return tmp_path / "NIKON_Z6"


def test_raw_policy_keeps_raw_and_unpaired_files_on_the_drive(pair_card, library, tmp_path):
    card = pair_card
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    res = run_import(card, library, "Europe", EXTS, backup_root=backup, backup_policy="raw")
    assert (res.backed_up, res.backup_skipped, res.backup_pending) == (3, 1, 0)
    assert res.safe_to_format
    on_backup = sorted(p.name for p in backup.rglob("*") if p.is_file())
    assert on_backup == ["DSC_0001.NEF", "DSC_0002.JPG", "DSC_0003.MOV"]
    assert importer.pending_backups(library, backup, policy="raw") == 0
    assert importer.pending_backups(library, backup, policy="all") == 1   # the paired JPEG


def test_raw_policy_catch_up_skips_paired_jpegs(pair_card, library, tmp_path):
    card = pair_card
    backup = tmp_path / "TravelSSD"
    res = run_import(card, library, "Europe", EXTS, backup_root=backup, backup_policy="raw")  # not connected
    assert res.backup_pending == 3 and not res.safe_to_format
    backup.mkdir()
    assert importer.pending_backups(library, backup, policy="raw") == 3
    r = importer.backup_library(library, backup, policy="raw")
    assert (r.copied, r.skipped) == (3, 1)
    res = run_import(card, library, "Europe", EXTS, backup_root=backup, backup_policy="raw")
    assert res.safe_to_format


# ---------------------------------------------------------------- partial imports

def test_partial_import_leaves_the_rest_on_the_card(pair_card, library):
    files = scan_media(pair_card, EXTS)
    first = [f for f in files if f.name == "DSC_0001.JPG"]
    chosen = importer.with_partners(first, files)            # the NEF partner comes along
    assert sorted(Path(p).name for p in chosen) == ["DSC_0001.JPG", "DSC_0001.NEF"]
    res = run_import(pair_card, library, "Rome", EXTS, only=chosen)
    assert (res.copied, res.not_selected) == (2, 2)
    assert not res.safe_to_format                             # two files are only on the card
    assert "2 not selected" in res.summary()
    assert sorted(p.name for p in library.rglob("DSC_*")) == ["DSC_0001.JPG", "DSC_0001.NEF"]

    # next time: import the rest; the first two are recognized as already imported
    rest = run_import(pair_card, library, "Rome", EXTS)
    assert (rest.copied, rest.already_imported, rest.not_selected) == (2, 2, 0)
    assert rest.safe_to_format
    assert rest.edited_folder == res.edited_folder            # still one trip


def test_selecting_nothing_new_imports_nothing(pair_card, library):
    res = run_import(pair_card, library, "Rome", EXTS, only=[])
    assert res.copied == 0 and res.not_selected == 4 and list(library.rglob("DSC_*")) == []


# ---------------------------------------------------------------- reorganizing the library after import

def _move(library, name, new_rel):
    src = next(library.rglob(name))
    dst = library / new_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return dst


def test_moved_and_renamed_files_are_not_imported_again(pair_card, library):
    run_import(pair_card, library, "Rome", EXTS)
    _move(library, "DSC_0001.NEF", "2026/2026-09-05_Rome/Colosseum/DSC_0001.NEF")
    _move(library, "DSC_0002.JPG", "Favorites/Rome sunset.jpg")
    res = run_import(pair_card, library, "Rome", EXTS)
    assert (res.copied, res.already_imported) == (0, 4) and res.safe_to_format
    assert not (library / "2026/2026-09-05_Rome/DSC_0001.NEF").exists()   # no duplicate re-imported
    index = importer.Index(library)
    rels = sorted(rel for _, rel in index.all_files())
    index.close()
    assert "2026/2026-09-05_Rome/Colosseum/DSC_0001.NEF" in rels and "Favorites/Rome sunset.jpg" in rels


def test_moved_before_backup_is_still_backed_up_once(pair_card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    run_import(pair_card, library, "Rome", EXTS, backup_root=backup)   # backup drive not connected
    _move(library, "DSC_0001.NEF", "2026/2026-09-05_Rome/Colosseum/DSC_0001.NEF")
    backup.mkdir()
    assert importer.pending_backups(library, backup) == 4             # the moved one is not forgotten
    r = importer.backup_library(library, backup)
    assert (r.copied, r.moved, r.failed) == (4, 1, 0)
    assert (backup / "2026/2026-09-05_Rome/Colosseum/DSC_0001.NEF").is_file()
    assert importer.pending_backups(library, backup) == 0


def test_moved_after_backup_is_not_copied_again(pair_card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    run_import(pair_card, library, "Rome", EXTS, backup_root=backup)
    _move(library, "DSC_0001.NEF", "2026/2026-09-05_Rome/Colosseum/DSC_0001.NEF")
    r = importer.backup_library(library, backup)
    assert (r.copied, r.already, r.moved) == (0, 4, 1)
    assert (backup / "2026/2026-09-05_Rome/DSC_0001.NEF").is_file()   # backup keeps the import-time layout
    assert not (backup / "2026/2026-09-05_Rome/Colosseum").exists()


def test_deleted_files_are_marked_gone_once(pair_card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    run_import(pair_card, library, "Rome", EXTS, backup_root=backup)
    next(library.rglob("DSC_0002.JPG")).unlink()
    r = importer.backup_library(library, backup)
    assert r.missing == 1 and importer.pending_backups(library, backup) == 0
    assert importer.backup_library(library, backup).missing == 0      # not searched for again


def test_edited_folder_moved_into_a_subfolder_is_still_backed_up(pair_card, library, tmp_path):
    backup = tmp_path / "TravelSSD"
    backup.mkdir()
    res = run_import(pair_card, library, "Rome", EXTS, backup_root=backup)
    moved = library / "2026" / "Italy" / Path(res.edited_folder).name
    moved.parent.mkdir()
    Path(res.edited_folder).rename(moved)
    _export(moved / "best.jpg", b"edit")
    assert importer.backup_library(library, backup).edited_copied == 1
    assert (backup / "2026/Italy/2026-09-05_Rome_Edited/best.jpg").is_file()


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="requires exiftool")
def test_capture_dates_with_ascii_default_encoding(tmp_path):
    """The login item can run with an ASCII default encoding; exiftool output with non-ASCII text
    (here a Chinese file name) must still be read. Runs in a fresh Python whose locale is really ASCII."""
    import sys
    photo = tmp_path / "照片_é.jpg"
    _jpg(photo, "2026:09:05 10:00:00")
    code = ("import sys; from pathlib import Path; from photoman.importer import read_capture_dates; "
            "print([str(d) for d in read_capture_dates([Path(sys.argv[1])]).values()])")
    env = {"HOME": os.environ.get("HOME", ""), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
           "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    r = subprocess.run([sys.executable, "-c", code, str(photo)], capture_output=True, env=env)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    assert r.stdout.decode().strip() == "['2026-09-05 10:00:00']"


def test_partial_import_only_reads_the_selected_files(pair_card, library, monkeypatch):
    run_import(pair_card, library, "Rome", EXTS)                       # everything imported once
    (pair_card / "DCIM" / "100NZ6_2" / "DSC_0009.JPG").write_bytes(b"new shot")
    files = scan_media(pair_card, EXTS)
    new = [f for f in files if f.name == "DSC_0009.JPG"]
    read = []
    real = importer.sha256_file
    monkeypatch.setattr(importer, "sha256_file", lambda p: (read.append(Path(p).name), real(p))[1])
    seen = []
    res = run_import(pair_card, library, "Rome", EXTS, only=new, progress=lambda i, n, name: seen.append((i, n, name)))
    assert seen == [(0, 1, "Reading capture dates…"), (1, 1, "DSC_0009.JPG")]   # progress covers the selection only
    assert res.copied == 1 and res.already_imported == 4 and res.not_selected == 0
    assert read == ["DSC_0009.JPG.part"]   # only the new copy is verified; already-imported files aren't re-read


def test_stopping_an_import_keeps_what_was_done(pair_card, library):
    calls = []
    res = run_import(pair_card, library, "Rome", EXTS, should_stop=lambda: len(calls) >= 2,
                     progress=lambda i, n, name: name.endswith("…") or calls.append(name))
    assert res.stopped and res.copied == 2 and not res.safe_to_format
    assert "stopped with 2 files not looked at" in res.summary()
    assert not list(library.rglob("*.part"))
    assert len(list((library / ".photoman" / "imports").glob("*.json"))) == 1   # the stop is logged
    rest = run_import(pair_card, library, "Rome", EXTS)                          # carry on later
    assert (rest.copied, rest.already_imported) == (2, 2) and rest.safe_to_format


def test_partial_import_reads_capture_dates_of_the_selection_only(pair_card, library, monkeypatch):
    asked = []
    real = importer.read_capture_dates
    monkeypatch.setattr(importer, "read_capture_dates", lambda fs: (asked.extend(Path(f).name for f in fs), real(fs))[1])
    first = [f for f in scan_media(pair_card, EXTS) if f.name.startswith("DSC_0001")]
    res = run_import(pair_card, library, "Rome", EXTS, only=first)
    assert sorted(asked) == ["DSC_0001.JPG", "DSC_0001.NEF"] and res.copied == 2 and res.not_selected == 2
