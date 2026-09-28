import os
from datetime import datetime
from pathlib import Path

import pytest

from photoman import trips
from photoman.importer import run_import

EXTS = [".nef", ".jpg"]


def _raw(path: Path, content: bytes, when: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (when.timestamp(), when.timestamp()))


def _card(root: Path, name: str, shots):
    """shots: [(file name, content, datetime)]"""
    for fname, content, when in shots:
        _raw(root / name / "DCIM" / "100NZ6_2" / fname, content, when)
    return root / name


def _export(path: Path, content=b"edit", age=3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    t = datetime.now().timestamp() - age
    os.utime(path, (t, t))


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "PhotoA"
    lib.mkdir()
    return lib


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(trips, "CACHE_PATH", tmp_path / "home" / "trips.json")


def test_two_cards_of_one_trip_share_the_edited_folder(tmp_path, library):
    c1 = _card(tmp_path, "c1", [("DSC_0001.NEF", b"a", datetime(2026, 9, 5, 10)),
                                ("DSC_0002.NEF", b"b", datetime(2026, 9, 6, 10))])
    c2 = _card(tmp_path, "c2", [("DSC_0100.NEF", b"c", datetime(2026, 9, 8, 10))])
    r1 = run_import(c1, library, "Rome", EXTS)
    r2 = run_import(c2, library, "Rome", EXTS)
    assert r1.edited_folder == r2.edited_folder == str(library / "2026" / "2026-09-05_Rome_Edited")
    assert not (library / "2026" / "2026-09-08_Rome_Edited").exists()

    [t] = trips.trips_from_logs(library)
    assert t.name == "Rome" and t.imports == 2
    assert t.folders == ["2026/2026-09-05_Rome", "2026/2026-09-06_Rome", "2026/2026-09-08_Rome"]
    assert t.date_range == "Sep 5 – Sep 8, 2026"


def test_same_trip_name_months_apart_is_a_new_trip(tmp_path, library):
    run_import(_card(tmp_path, "c1", [("A.NEF", b"a", datetime(2026, 3, 1, 10))]), library, "Rome", EXTS)
    r = run_import(_card(tmp_path, "c2", [("B.NEF", b"b", datetime(2026, 9, 1, 10))]), library, "Rome", EXTS)
    assert r.edited_folder == str(library / "2026" / "2026-09-01_Rome_Edited")
    assert len(trips.trips_from_logs(library)) == 2


def test_status_counts_photos_edits_and_pending_backups(tmp_path, library):
    backup = tmp_path / "TravelSSD"
    card = _card(tmp_path, "c1", [("DSC_0001.NEF", b"a", datetime(2026, 9, 5, 10)),
                                  ("DSC_0002.NEF", b"b", datetime(2026, 9, 6, 10))])
    run_import(card, library, "Rome", EXTS, backup_root=backup)          # backup drive not connected
    run_import(_card(tmp_path, "c2", [("X.NEF", b"x", datetime(2026, 10, 1, 10))]), library, "Paris", EXTS)
    _export(library / "2026" / "2026-09-05_Rome_Edited" / "DSC_0001.jpg")
    _export(library / "2026" / "2026-09-05_Rome_Edited" / "Web" / "DSC_0001.jpg")

    found = trips.load_trips([str(library)], None)
    assert [t.title for t in found] == ["Paris", "Rome"]          # newest first
    rome = found[1]
    assert (rome.photos, rome.edited, rome.backup_pending) == (2, 2, None)
    assert rome.last_export is not None

    backup.mkdir()
    rome = trips.load_trips([str(library)], backup)[1]
    assert (rome.originals_pending, rome.edited_pending) == (2, 2)


def test_disconnected_destination_comes_from_cache(tmp_path, library):
    run_import(_card(tmp_path, "c1", [("A.NEF", b"a", datetime(2026, 9, 5, 10))]), library, "Rome", EXTS)
    assert trips.load_trips([str(library)], None)[0].connected
    moved = tmp_path / "unplugged"
    library.rename(moved)
    [t] = trips.load_trips([str(library)], None)
    assert t.title == "Rome" and not t.connected and t.photos == 1


def test_imports_without_new_files_or_dry_runs_are_not_trips(tmp_path, library):
    card = _card(tmp_path, "c1", [("A.NEF", b"a", datetime(2026, 9, 5, 10))])
    run_import(card, library, "Rome", EXTS, dry_run=True)
    run_import(card, library, "Rome", EXTS)
    run_import(card, library, "Rome", EXTS)                     # nothing new
    [t] = trips.trips_from_logs(library)
    assert t.imports == 1


def test_trip_counts_follow_moved_photos(tmp_path, library):
    card = _card(tmp_path, "c1", [("A.NEF", b"a", datetime(2026, 9, 5, 10)), ("B.NEF", b"b", datetime(2026, 9, 5, 11))])
    run_import(card, library, "Rome", EXTS)
    (library / "Best").mkdir()
    (library / "2026" / "2026-09-05_Rome" / "A.NEF").rename(library / "Best" / "A.NEF")
    [t] = trips.load_trips([str(library)], None)
    assert t.photos == 2
