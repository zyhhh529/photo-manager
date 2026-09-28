"""Menu bar app: insert a Nikon card → import window (destination, trip name, progress, result).

Run: python -m photoman.menubar, or Photoman.app built by install.sh (which also starts it at login)
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import rumps
from AppKit import NSWorkspace
from Foundation import NSURL
from PyObjCTools.AppHelper import callAfter

from . import baidu, config
from .cli import find_cards, record_backup, record_import
from .import_window import ImportWindow
from .trips_window import TripsWindow
from .importer import backup_library, backup_problem, is_writable, pending_backups, run_import, scan_media
from .macui import alert, choose_folder, drive_label, float_above, free_space, human_size, notify, read_only_alert

POLL_SECONDS = 2
RECHECK_BACKUP_EVERY = 150  # ticks (5 minutes): look for new Lightroom exports to back up


class PhotomanApp(rumps.App):
    def __init__(self):
        super().__init__("Photoman", title="📷", quit_button="Quit")
        self.cfg = config.load_config()
        self.seen = {str(p) for p in find_cards()}  # cards inserted before launch don't trigger a prompt
        self.busy = False        # an import is running
        self.backing_up = False  # a catch-up backup is running
        self.progress = (0, 0)
        self.backup_pending = 0  # imported files not yet on the travel backup drive
        self._drives_sig = None
        self._ticks = 0
        self.window = None       # the open ImportWindow, if any
        self.trips_window = None
        self.last_card = None
        self._dest_sig = None

        self.status_item = rumps.MenuItem("Last import: none")
        self.library_item = rumps.MenuItem("Destination: not set")
        self.backup_item = rumps.MenuItem("Travel backup: not set")
        self.backup_now_item = rumps.MenuItem("Back Up Now")
        self.cloud_item = rumps.MenuItem("Baidu Netdisk: not set up")
        self.cloud_setup_item = rumps.MenuItem("Set Up Baidu Netdisk…", callback=self.on_setup_baidu)
        self.uploading = False   # Baidu Netdisk uploads run on their own thread, alongside imports and backups
        self.cloud_pending = 0
        self.cloud_progress = (0, 0)
        self.cloud_problem = None
        self.eject_item = rumps.MenuItem("Eject Card")
        self.dest_menu = rumps.MenuItem("Import Destination")
        self.menu = [
            self.status_item,
            self.library_item,
            self.backup_item,
            None,
            rumps.MenuItem("Import from Card…", callback=self.on_manual_import),
            rumps.MenuItem("Trips…", callback=self.on_trips),
            self.eject_item,
            rumps.MenuItem("Show Last Import in Finder", callback=self.on_reveal_last),
            rumps.MenuItem("Open Edited Folder", callback=self.on_open_edited),
            rumps.MenuItem("Open Library", callback=self.on_open_library),
            None,
            self.dest_menu,
            self.backup_now_item,
            rumps.MenuItem("Set Travel Backup Drive…", callback=self.on_set_backup),
            None,
            self.cloud_item,
            self.cloud_setup_item,
            rumps.MenuItem("Open Import Logs", callback=self.on_open_logs),
        ]
        self.refresh_menu()
        rumps.Timer(self.tick, POLL_SECONDS).start()

    # ------------------------------------------------------------ status display

    def library_root(self):
        lr = self.cfg.get("library_root")
        return Path(lr) if lr else None

    def refresh_menu(self):
        lib = self.library_root()
        if not lib:
            self.library_item.title = "Destination: not set"
        elif lib.is_dir():
            free = shutil.disk_usage(lib).free
            self.library_item.title = f"Destination: {lib.name} on {drive_label(lib)} ({human_size(free)} free)"
        else:
            self.library_item.title = f"Destination: {lib} (drive not connected)"
        self.refresh_dest_menu()

        bak = self.cfg.get("travel_backup_root")
        if not bak:
            self.backup_item.title = "Travel backup: not set"
        elif self.backing_up:
            i, n = self.progress
            self.backup_item.title = f"Travel backup: {Path(bak).name} — backing up {i}/{n}…"
        else:
            problem = backup_problem(Path(bak))
            pending = f", {self.backup_pending} files waiting" if self.backup_pending else ""
            self.backup_item.title = f"Travel backup: {Path(bak).name} ({problem or 'connected'}{pending})"
        self.backup_now_item.set_callback(self.on_backup_now if self.can_back_up() else None)
        self.refresh_cloud_item()

        last = config.load_state().get("last_import")
        if last:
            mark = "✅" if last.get("safe_to_format") else "⚠️"
            self.status_item.title = f"Last import: {last['finished_at'][:16].replace('T', ' ')} {last.get('trip') or ''} {mark}"

        can_eject = self.last_card and Path(self.last_card).exists() and not self.busy
        self.eject_item.set_callback(self.on_eject if can_eject else None)

    def recent_libraries(self) -> list:
        recent = list(self.cfg.get("recent_libraries") or [])
        current = self.cfg.get("library_root")
        if current and current not in recent:
            recent.insert(0, current)
        return recent

    def refresh_dest_menu(self):
        """Submenu of recent destinations (✓ = current). Rebuilt only when something changed,
        so it doesn't flicker while open."""
        current = self.cfg.get("library_root")
        recent = self.recent_libraries()
        sig = (current, tuple((p, Path(p).is_dir()) for p in recent))
        if sig == self._dest_sig:
            return
        self._dest_sig = sig
        if len(self.dest_menu):
            self.dest_menu.clear()
        for p in recent:
            title = p if Path(p).is_dir() else f"{p} (not connected)"
            item = rumps.MenuItem(title, callback=lambda _, p=p: self.set_library(p))
            item.state = int(p == current)
            self.dest_menu.add(item)
        if recent:
            self.dest_menu.add(rumps.separator)
        self.dest_menu.add(rumps.MenuItem("Choose Folder…", callback=self.on_choose_library))

    def pick_library(self):
        """Current destination if connected, otherwise the most recent one that is."""
        for p in self.recent_libraries():
            if Path(p).is_dir():
                return Path(p)
        return None

    def backup_root(self):
        bak = self.cfg.get("travel_backup_root")
        return Path(bak) if bak else None

    def backup_status(self):
        """(text, level) describing the travel backup for the import window, or None if none is set."""
        bak = self.backup_root()
        if not bak:
            return None
        problem = backup_problem(bak)
        if problem == "not connected":
            return (f"Travel backup {bak.name} isn't connected. The photos will be backed up when you connect it "
                    f"— keep the card until then.", "warn")
        if problem:
            return (f"Travel backup {bak.name} is {problem}, so nothing can be backed up there. Keep the card, "
                    f"or choose another backup drive from the menu.", "error")
        return f"Also backs up to {bak.name} on {drive_label(bak)} · {human_size(free_space(bak))} free", "info"

    def backup_status_word(self) -> str:
        if self.backing_up:
            return "backing up…"
        return backup_problem(self.backup_root()) or "connected"

    def can_back_up(self) -> bool:
        bak = self.backup_root()
        return bool(bak and not backup_problem(bak) and self.backup_pending and not self.backing_up and not self.busy)

    def connected_libraries(self) -> list:
        """Every destination ever used that's connected now (catch-up backups cover all of them)."""
        return [Path(p) for p in config.known_libraries(self.cfg) if Path(p).is_dir()]

    def update_backup_pending(self):
        bak = self.backup_root()
        if not bak or backup_problem(bak):
            self.backup_pending = 0
            return
        policy = self.cfg["backup_originals"]
        self.backup_pending = sum(pending_backups(p, bak, policy=policy) for p in self.connected_libraries())

    # ------------------------------------------------------------ polling

    def tick(self, _):
        if self.busy:
            return
        self.check_drives()
        current = {str(p) for p in find_cards()}
        new = sorted(current - self.seen)
        self.seen = current
        if self.window and self.window.state == "ready" and str(self.window.card) not in current:
            self.window.close()  # card pulled out before importing
        if new:
            self.start_import_flow(Path(new[0]))
        if self.window and self.window.state == "ready":
            self.window.validate()  # e.g. the backup drive was just connected
        self.refresh_menu()

    def check_drives(self):
        """When the backup drive or a destination is connected, recount pending backups and catch up."""
        bak = self.backup_root()
        sig = (bak and not backup_problem(bak), tuple(self.connected_libraries()))
        self._ticks += 1
        changed = sig != self._drives_sig
        if not changed and self._ticks % RECHECK_BACKUP_EVERY:
            return
        quiet = self._drives_sig is None or not changed  # only announce when a drive was just connected
        self._drives_sig = sig
        self.update_backup_pending()
        if self.backup_pending and not self.backing_up:
            self.start_backup(manual=False, quiet_start=quiet)
        self.start_cloud_upload()

    # ------------------------------------------------------------ import flow

    def default_trip(self) -> str:
        state = config.load_state()
        last = state.get("last_import")
        if not last or not state.get("last_trip"):
            return ""
        try:
            when = datetime.fromisoformat(last["finished_at"])
        except ValueError:
            return ""
        days = self.cfg.get("trip_name_reuse_days", 3)
        return state["last_trip"] if datetime.now() - when <= timedelta(days=days) else ""

    def start_import_flow(self, card: Path):
        if self.window is not None:
            self.window.show()
            return
        self.cfg = config.load_config()
        files = scan_media(card, self.cfg["extensions"])
        if not files:
            notify("Photoman", f"No photos or videos on {card.name}")
            return
        # Cameras set the file time to the capture time, which is good enough for a preview
        dates = [datetime.fromtimestamp(f.stat().st_mtime) for f in files]
        self.window = ImportWindow.alloc().init().setup(
            self, card, files, dates, self.recent_libraries(), self.pick_library(), self.default_trip())

    def import_window_closed(self, window):
        if self.window is window:
            self.window = None

    def begin_import(self, card: Path, lib: Path, trip: str) -> bool:
        """Called by the import window; runs the import on a background thread."""
        if self.backing_up:
            alert("A backup is running", "Photoman is copying earlier imports to the travel backup drive. "
                                         "Try again when it finishes (see the menu bar for progress).")
            return False
        config.remember_library(self.cfg, lib)
        config.save_config(self.cfg)
        backup = self.cfg.get("travel_backup_root")
        self.busy = True
        self.progress = (0, 0)
        self.last_card = str(card)
        self.refresh_menu()

        def work():
            try:
                res = run_import(card, lib, trip, self.cfg["extensions"],
                                 backup_root=Path(backup) if backup else None,
                                 progress=lambda i, n, name: callAfter(self.on_progress, i, n, name),
                                 backup_policy=self.cfg["backup_originals"])
                callAfter(self.on_import_done, card, res)
            except Exception as e:
                traceback.print_exc()
                callAfter(self.on_import_done, card, e)

        threading.Thread(target=work, daemon=True).start()
        return True

    def on_progress(self, i: int, n: int, name: str):
        self.progress = (i, n)
        self.title = f"📷 {i}/{n}"
        if self.window:
            self.window.show_progress(i, n, name)

    def on_import_done(self, card: Path, outcome):
        self.busy = False
        self.title = "📷"
        if isinstance(outcome, Exception):
            title, detail, level = "Import failed — do NOT format the card", str(outcome), "error"
        else:
            res = outcome
            record_import(res)
            title, detail, level = self.describe_result(res)
        self.update_backup_pending()
        self.refresh_menu()
        self.refresh_trips()
        self.start_cloud_upload()
        if self.window:
            self.window.show_result(title, detail, level)
        else:
            alert(title, detail)

    @staticmethod
    def describe_result(res):
        lines = [res.summary() + "."]
        if res.bytes_copied:
            lines.append(f"Copied {human_size(res.bytes_copied)} to {drive_label(Path(res.library_root))}.")
        if res.folders:
            shown = [Path(f).name for f in res.folders[:5]]
            more = f" (+{len(res.folders) - 5} more)" if len(res.folders) > 5 else ""
            lines.append("Folders: " + ", ".join(shown) + more)
        if res.edited_folder:
            lines.append(f"Export your Lightroom edits to {Path(res.edited_folder).name} (next to the date folders)"
                         + (" — it's backed up too." if res.backup_root else "."))
        bak = Path(res.backup_root) if res.backup_root else None

        if res.failed or res.in_library < res.total:
            failed = [f for f in res.files if f.status == "failed"]
            lines.append("Failed:\n" + "\n".join(f"• {Path(f.source).name}: {f.error}" for f in failed[:5])
                         + (f"\n… and {len(failed) - 5} more" if len(failed) > 5 else ""))
            return "⚠️ Some files failed to import — do NOT format the card", "\n\n".join(lines), "error"
        if res.backup_pending:
            if res.backup_error == "not connected":
                lines.append(f"The travel backup drive ({bak.name}) isn't connected. Connect it and Photoman "
                             f"will back up these photos automatically — then it's safe to format the card.")
            elif res.backup_error:
                lines.append(f"The travel backup drive ({bak.name}) is {res.backup_error}, so nothing was backed up.")
            else:
                lines.append(f"{res.backup_failed} files couldn't be copied to {bak.name}. "
                             f"Try “Back Up Now” from the menu.")
            return "⚠️ Imported, but not backed up yet — keep the card", "\n\n".join(lines), "warn"
        if bak:
            lines.insert(1, f"Every file also has a verified copy on {bak.name}.")
            return "✅ Imported and backed up — safe to format the card", "\n\n".join(lines), "ok"
        return "✅ Import complete — safe to format the card", "\n\n".join(lines), "ok"

    # ------------------------------------------------------------ catch-up backup

    def start_backup(self, manual: bool, quiet_start: bool = False):
        bak = self.backup_root()
        libs = self.connected_libraries()
        if not bak or backup_problem(bak) or not libs or self.busy or self.backing_up:
            return
        self.backing_up = True
        self.progress = (0, 0)
        if not manual and not quiet_start:
            notify("Photoman", f"Backing up {self.backup_pending} files to {bak.name}…")

        def work():
            results, error = [], None
            try:
                for lib in libs:
                    results.append(backup_library(
                        lib, bak, progress=lambda i, n, _name: callAfter(self.on_backup_progress, i, n),
                        policy=self.cfg["backup_originals"]))
            except Exception as e:
                traceback.print_exc()
                error = e
            callAfter(self.on_backup_done, bak, results, error, manual)

        threading.Thread(target=work, daemon=True).start()
        self.refresh_menu()

    def on_backup_progress(self, i: int, n: int):
        self.progress = (i, n)
        self.title = f"📷 ⇪ {i}/{n}"
        self.refresh_menu()

    def on_backup_done(self, bak: Path, results, error, manual: bool):
        self.backing_up = False
        self.title = "📷"
        copied = sum(r.copied for r in results)
        edited = sum(r.edited_copied for r in results)
        failed = sum(r.failed_total for r in results)
        now_safe = any(record_backup(Path(r.library_root), bak) for r in results)
        self.update_backup_pending()
        self.refresh_menu()
        self.refresh_trips()
        if error:
            title, msg = "Backup failed", str(error)
        elif failed:
            title, msg = f"⚠️ {failed} files couldn't be backed up", f"{copied + edited} files were copied to {bak.name}. Try “Back Up Now” again."
        else:
            title = f"✅ Backed up to {bak.name}"
            parts = [f"{copied} originals"] * bool(copied) + [f"{edited} edited files"] * bool(edited)
            msg = (" and ".join(parts) or "Nothing new") + " copied and verified." + (
                " The last card is now safe to format." if now_safe else "")
        if manual or error or failed:
            alert(title, msg)
        elif copied or edited:
            notify(title, msg)

    # ------------------------------------------------------------ Baidu Netdisk

    def refresh_cloud_item(self):
        if not baidu.is_set_up():
            self.cloud_item.title = "Baidu Netdisk: not set up"
            self.cloud_setup_item.title = "Set Up Baidu Netdisk…"
            return
        self.cloud_setup_item.title = "Reconnect Baidu Netdisk…"
        if self.cloud_problem:
            self.cloud_item.title = f"Baidu Netdisk: {self.cloud_problem}"
        elif self.uploading:
            i, n = self.cloud_progress
            self.cloud_item.title = f"Baidu Netdisk: uploading {i}/{n}…"
        elif self.cloud_pending:
            self.cloud_item.title = f"Baidu Netdisk: {self.cloud_pending} files waiting"
        else:
            self.cloud_item.title = "Baidu Netdisk: ✓ all uploaded"

    def start_cloud_upload(self):
        """Upload whatever isn't in Baidu Netdisk yet, from every connected destination, in the background."""
        if self.uploading or self.cloud_problem or not baidu.is_set_up():
            return
        policy = self.cfg.get("cloud_originals", "jpeg")
        libs = self.connected_libraries()
        self.cloud_pending = sum(baidu.pending_cloud(p, policy) for p in libs)
        if not self.cloud_pending:
            self.refresh_cloud_item()
            return
        self.uploading = True
        self.cloud_progress = (0, self.cloud_pending)

        def work():
            results, error = [], None
            try:
                client = baidu.Baidu()
                for lib in libs:
                    results.append(baidu.upload_library(
                        lib, client, policy,
                        progress=lambda i, n, _name: callAfter(self.on_cloud_progress, i, n)))
            except Exception as e:
                traceback.print_exc()
                error = e
            callAfter(self.on_cloud_done, results, error)

        threading.Thread(target=work, daemon=True).start()
        self.refresh_cloud_item()

    def on_cloud_progress(self, i: int, n: int):
        self.cloud_progress = (i, n)
        self.refresh_cloud_item()

    def on_cloud_done(self, results, error):
        self.uploading = False
        uploaded = sum(r.uploaded for r in results)
        failed = sum(r.failed for r in results)
        if isinstance(error, baidu.BaiduAuthError):
            self.cloud_problem = "needs reconnecting"
            notify("Baidu Netdisk needs reconnecting", "Choose “Reconnect Baidu Netdisk…” in the Photoman menu.")
        elif error:
            notify("Baidu Netdisk upload stopped", str(error)[:200])
        elif failed:
            errors = "; ".join(e for r in results for e in r.errors)[:200]
            notify(f"⚠️ {failed} files couldn't be uploaded to Baidu Netdisk", errors + " — will retry later.")
        elif uploaded:
            notify("✅ Uploaded to Baidu Netdisk", f"{uploaded} files uploaded and verified.")
        policy = self.cfg.get("cloud_originals", "jpeg")
        self.cloud_pending = sum(baidu.pending_cloud(p, policy) for p in self.connected_libraries())
        self.refresh_cloud_item()
        self.refresh_trips()

    def on_setup_baidu(self, _):
        creds = baidu.load_credentials()
        fields = [("app_key", "AppKey", "The AppKey of the app you registered at pan.baidu.com/union"),
                  ("secret_key", "SecretKey", "The SecretKey of the same app"),
                  ("app_name", "App name", "The app's name exactly as registered. Uploads go to "
                                            "“我的应用数据/<app name>” (/apps/<app name>) in Baidu Netdisk.")]
        for key, label, help_text in fields:
            w = rumps.Window(title=f"Baidu Netdisk — {label}", message=help_text,
                             default_text=creds.get(key, ""), ok="Next", cancel="Cancel", dimensions=(320, 24))
            float_above(w._alert)
            resp = w.run()
            if resp.clicked != 1 or not resp.text.strip():
                return
            creds[key] = resp.text.strip()
        creds.pop("access_token", None)
        baidu.save_credentials(creds)
        try:
            info = baidu.start_device_auth(creds["app_key"])
        except baidu.BaiduError as e:
            alert("Couldn't start Baidu authorization", f"{e}\n\nCheck the AppKey and try again.")
            return
        subprocess.run(["open", info.get("verification_url", "https://openapi.baidu.com/device")])
        alert("Authorize Photoman in Baidu",
              f"A Baidu page has opened in your browser. Sign in and enter this code:\n\n{info['user_code']}\n\n"
              f"Photoman will notice when you're done (the code expires in {int(info.get('expires_in', 300)) // 60} "
              f"minutes).")

        def poll():
            deadline = time.time() + int(info.get("expires_in", 300))
            while time.time() < deadline:
                time.sleep(max(int(info.get("interval", 5)), 3))
                try:
                    if baidu.poll_device_auth(creds, info["device_code"]):
                        callAfter(self.on_baidu_connected)
                        return
                except baidu.BaiduError as e:
                    callAfter(notify, "Baidu authorization failed", str(e))
                    return
            callAfter(notify, "Baidu authorization timed out", "Choose “Set Up Baidu Netdisk…” to try again.")

        threading.Thread(target=poll, daemon=True).start()

    def on_baidu_connected(self):
        self.cloud_problem = None
        notify("✅ Baidu Netdisk connected", "Photoman will upload your photos in the background.")
        self.start_cloud_upload()
        self.refresh_menu()

    # ------------------------------------------------------------ trips window

    def on_trips(self, _):
        if self.trips_window is None:
            self.trips_window = TripsWindow.alloc().init().setup(self)
        else:
            self.trips_window.reload()
            self.trips_window.show()

    def trips_window_closed(self, window):
        if self.trips_window is window:
            self.trips_window = None

    def refresh_trips(self):
        if self.trips_window is not None:
            self.trips_window.reload()

    def on_backup_now(self, _):
        self.start_backup(manual=True)

    # ------------------------------------------------------------ menu commands

    def on_manual_import(self, _):
        cards = find_cards()
        if not cards:
            alert("No card detected", "Insert a camera memory card (it must contain a DCIM folder).")
            return
        self.start_import_flow(cards[0])

    def on_eject(self, _):
        self.eject_card()

    def eject_card(self) -> bool:
        if not self.last_card:
            return False
        r = subprocess.run(["diskutil", "eject", self.last_card], capture_output=True, text=True)
        if r.returncode == 0:
            notify("Photoman", "Card ejected — you can remove it now")
        else:
            alert("Couldn't eject the card", r.stderr.strip() or r.stdout.strip())
        self.refresh_menu()
        return r.returncode == 0

    def on_reveal_last(self, _):
        last = config.load_state().get("last_import")
        folders = [f for f in [*((last or {}).get("folders") or []), (last or {}).get("edited_folder")]
                   if f and Path(f).is_dir()]
        if folders:
            # One Finder window with this import's date folders selected, not a window per folder
            urls = [NSURL.fileURLWithPath_(f) for f in folders]
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(urls)
        elif last:
            alert("Last import's folders not found", "They may have been moved or deleted, or the drive isn't connected.")
        else:
            alert("No imports yet")

    def on_open_edited(self, _):
        edited = (config.load_state().get("last_import") or {}).get("edited_folder")
        if edited and Path(edited).is_dir():
            subprocess.run(["open", edited])
        else:
            alert("No Edited folder yet", "Photoman creates one next to the date folders when you import.")

    def on_open_library(self, _):
        lib = self.library_root()
        if lib and lib.is_dir():
            subprocess.run(["open", str(lib)])
        else:
            alert("Destination not set or drive not connected")

    def on_open_logs(self, _):
        lib = self.library_root()
        logs = lib / ".photoman" / "imports" if lib else None
        if logs and logs.is_dir():
            subprocess.run(["open", str(logs)])
        else:
            alert("No import logs yet")

    def on_choose_library(self, _):
        p = choose_folder("Choose the import destination (e.g. a folder on your external drive)")
        if p and not is_writable(p):
            read_only_alert(p)
        elif p:
            self.set_library(p)

    def set_library(self, path):
        self.cfg = config.load_config()
        config.remember_library(self.cfg, path)
        config.save_config(self.cfg)
        self.refresh_menu()

    def on_set_backup(self, _):
        p = choose_folder("Choose a folder on the travel backup drive (imports will write an extra copy here)")
        if p and not is_writable(p):
            read_only_alert(p)
        elif p:
            self.cfg["travel_backup_root"] = str(p).rstrip("/")
            config.save_config(self.cfg)
            self._drives_sig = None  # recount pending backups (and catch up) on the next tick
            self.refresh_menu()


def main():
    PhotomanApp().run()


if __name__ == "__main__":
    main()
