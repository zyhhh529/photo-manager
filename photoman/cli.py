"""Command line entry point:

  python -m photoman.cli import /Volumes/NIKON_Z --trip Kyoto [--dry-run]
  python -m photoman.cli cards            # list camera cards currently inserted
  python -m photoman.cli status           # show config and last import
  python -m photoman.cli set-library /Volumes/PhotoA/Photos
  python -m photoman.cli backup           # copy originals and edits that aren't on the travel backup drive yet
  python -m photoman.cli baidu-setup      # connect Baidu Netdisk (asks for AppKey / SecretKey / app name)
  python -m photoman.cli baidu-upload     # upload what isn't in Baidu Netdisk yet
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import baidu, config
from .importer import backup_library, backup_problem, is_camera_card, pending_backups, run_import


def find_cards(volumes: Path = Path("/Volumes")) -> list:
    if not volumes.is_dir():
        return []
    return [p for p in sorted(volumes.iterdir()) if p.is_dir() and is_camera_card(p)]


def record_import(res) -> None:
    """Save this import's result to ~/.photoman/state.json for the menu bar to display."""
    state = config.load_state()
    state["last_import"] = {"finished_at": res.finished_at, "trip": res.trip,
                            "summary": res.summary(), "folders": res.folders,
                            "safe_to_format": res.safe_to_format, "library_root": res.library_root,
                            "failed": res.failed, "backup_pending": res.backup_pending,
                            "edited_folder": res.edited_folder}
    if res.trip:
        state["last_trip"] = res.trip
    config.save_state(state)


def record_backup(library: Path, backup: Path) -> bool:
    """After a catch-up backup: if the last import was only waiting for its backup, it's now safe to format.
    Returns True if that changed."""
    state = config.load_state()
    last = state.get("last_import")
    policy = config.load_config().get("backup_originals", "raw")
    if (not last or last.get("safe_to_format") or last.get("failed") or not last.get("backup_pending")
            or last.get("library_root") != str(library) or pending_backups(library, backup, include_edited=False, policy=policy)):
        return False
    last["safe_to_format"], last["backup_pending"] = True, 0
    last["summary"] += " (backed up later)"
    config.save_state(state)
    return True


def format_advice(res) -> str:
    if res.safe_to_format:
        return "✅ All files verified" + (" and backed up" if res.backup_root else "") + ", safe to format the card"
    if res.failed or res.in_library < res.total:
        return "⚠️ Some files failed to import, do NOT format the card"
    if res.backup_error:
        return (f"⚠️ Imported, but the travel backup is {res.backup_error}. Keep the card until the photos are "
                f"backed up (connect the drive and run: photoman backup)")
    return f"⚠️ {res.backup_pending} files weren't backed up to the travel backup drive, do NOT format the card"


def _progress(i, n, name):
    sys.stdout.write(f"\r[{i}/{n}] {name:<40}")
    sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="photoman")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="import from a memory card")
    p_imp.add_argument("source", type=Path)
    p_imp.add_argument("--trip", default="", help="trip name, e.g. Kyoto")
    p_imp.add_argument("--library", type=Path, help="import to this folder instead of the configured destination")
    p_imp.add_argument("--dry-run", action="store_true", help="simulate only, write nothing")

    sub.add_parser("cards", help="list inserted camera cards")
    sub.add_parser("status", help="show config and last import")
    p_lib = sub.add_parser("set-library", help="set the import destination (e.g. a folder on an external drive)")
    p_lib.add_argument("path", type=Path)
    p_bak = sub.add_parser("set-backup", help="set the travel backup drive path (pass none to unset)")
    p_bak.add_argument("path")
    p_back = sub.add_parser("backup", help="copy imported originals and Edited folders that aren't on the travel backup drive yet")
    p_back.add_argument("--library", type=Path, help="back up this library instead of the configured destination")
    sub.add_parser("baidu-setup", help="connect Baidu Netdisk (device-code authorization)")
    sub.add_parser("baidu-upload", help="upload originals (per cloud_originals) and edits to Baidu Netdisk")

    args = ap.parse_args(argv)
    cfg = config.load_config()

    if args.cmd == "cards":
        cards = find_cards()
        print("\n".join(map(str, cards)) if cards else "No camera cards detected")
        return 0

    if args.cmd == "status":
        print(f"Config file: {config.CONFIG_PATH}")
        print(f"Import destination: {cfg['library_root'] or '(not set)'}")
        for lib in cfg.get("recent_libraries") or []:
            if lib != cfg["library_root"]:
                print(f"  recent: {lib}{'' if Path(lib).is_dir() else ' (not connected)'}")
        bak, lib = cfg["travel_backup_root"], cfg["library_root"]
        if not bak:
            print("Travel backup: (not set)")
        elif backup_problem(Path(bak)):
            print(f"Travel backup: {bak} ({backup_problem(Path(bak))})")
        elif lib and Path(lib).is_dir():
            print(f"Travel backup: {bak} ({pending_backups(Path(lib), Path(bak), policy=cfg['backup_originals'])} files waiting to be backed up)")
        else:
            print(f"Travel backup: {bak}")
        if baidu.is_set_up():
            libs = [Path(p) for p in config.known_libraries(cfg) if Path(p).is_dir()]
            waiting = sum(baidu.pending_cloud(p, cfg["cloud_originals"]) for p in libs)
            print(f"Baidu Netdisk: connected, uploads to /apps/{baidu.load_credentials()['app_name']} "
                  f"({cfg['cloud_originals']} originals + edits; {waiting} files waiting)")
        else:
            print("Baidu Netdisk: not set up (run: photoman baidu-setup)")
        last = config.load_state().get("last_import")
        print(f"Last import: {last['finished_at']} {last['trip']} — {last['summary']}" if last else "Last import: none")
        return 0

    if args.cmd == "set-library":
        config.remember_library(cfg, args.path.expanduser().resolve())
        config.save_config(cfg)
        print(f"Import destination set to {cfg['library_root']}")
        return 0

    if args.cmd == "set-backup":
        cfg["travel_backup_root"] = None if args.path.lower() == "none" else str(Path(args.path).expanduser().resolve())
        config.save_config(cfg)
        print(f"Travel backup: {cfg['travel_backup_root'] or '(not set)'}")
        return 0

    if args.cmd == "backup":
        library = args.library or (Path(cfg["library_root"]) if cfg["library_root"] else None)
        if not library or not cfg.get("travel_backup_root"):
            print("Set both an import destination (set-library) and a travel backup (set-backup) first", file=sys.stderr)
            return 2
        backup = Path(cfg["travel_backup_root"])
        try:
            r = backup_library(library, backup, progress=_progress, policy=cfg["backup_originals"])
        except (FileNotFoundError, PermissionError) as e:
            print(e, file=sys.stderr)
            return 2
        print()
        print(r.summary())
        if record_backup(library, backup):
            print("✅ The last import is now backed up, safe to format that card")
        return 0 if r.failed_total == 0 else 1

    if args.cmd == "baidu-setup":
        creds = baidu.load_credentials()
        for key, label in (("app_key", "AppKey"), ("secret_key", "SecretKey"), ("app_name", "App name (as registered)")):
            value = input(f"{label} [{creds.get(key, '')}]: ").strip() or creds.get(key, "")
            if not value:
                print(f"{label} is required", file=sys.stderr)
                return 2
            creds[key] = value
        creds.pop("access_token", None)
        baidu.save_credentials(creds)
        try:
            info = baidu.start_device_auth(creds["app_key"])
            print(f"Open {info['verification_url']} , sign in and enter the code: {info['user_code']}")
            deadline = time.time() + int(info.get("expires_in", 300))
            while time.time() < deadline:
                time.sleep(max(int(info.get("interval", 5)), 3))
                if baidu.poll_device_auth(creds, info["device_code"]):
                    print(f"✅ Connected. Uploads go to /apps/{creds['app_name']} in Baidu Netdisk.")
                    return 0
            print("Timed out waiting for authorization", file=sys.stderr)
        except baidu.BaiduError as e:
            print(e, file=sys.stderr)
        return 2

    if args.cmd == "baidu-upload":
        if not baidu.is_set_up():
            print("Baidu Netdisk isn't set up yet. Run: photoman baidu-setup", file=sys.stderr)
            return 2
        client, failed = baidu.Baidu(), 0
        for lib in [Path(p) for p in config.known_libraries(cfg) if Path(p).is_dir()]:
            try:
                r = baidu.upload_library(lib, client, cfg["cloud_originals"], progress=_progress)
            except baidu.BaiduError as e:
                print(f"\n{e}", file=sys.stderr)
                return 2
            print(f"\n{lib}: {r.summary()}")
            for err in r.errors:
                print(f"  ✗ {err}")
            failed += r.failed
        return 0 if failed == 0 else 1

    # import
    library = args.library or (Path(cfg["library_root"]) if cfg["library_root"] else None)
    if not library:
        print("No import destination set. Pass --library <path> or run: python -m photoman.cli set-library <path>", file=sys.stderr)
        return 2
    backup = Path(cfg["travel_backup_root"]) if cfg.get("travel_backup_root") else None
    try:
        res = run_import(args.source, library, args.trip, cfg["extensions"],
                         dry_run=args.dry_run, backup_root=backup, progress=_progress,
                         backup_policy=cfg["backup_originals"])
    except (FileNotFoundError, PermissionError) as e:
        print(e, file=sys.stderr)
        return 2
    print()
    print(res.summary())
    for f in res.files:
        if f.status == "failed":
            print(f"  ✗ {f.source}: {f.error}")
    if not args.dry_run:
        record_import(res)
        print(format_advice(res))
        if res.edited_folder:
            print(f"Export your Lightroom edits to: {res.edited_folder}")
    return 0 if res.failed == 0 and res.backup_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
