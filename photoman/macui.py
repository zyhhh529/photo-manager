"""Small macOS UI helpers shared by the menu bar and the import window."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from AppKit import NSAlert, NSAlertFirstButtonReturn, NSApplication, NSFloatingWindowLevel, NSOpenPanel
from Foundation import NSURL

from .importer import is_writable


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def activate_app() -> None:
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def float_above(ns_alert) -> None:
    """A background menu bar app can't take focus on recent macOS, so its dialogs would open
    behind the active window. Float them above normal windows instead."""
    activate_app()
    ns_alert.layout()
    ns_alert.window().setLevel_(NSFloatingWindowLevel)


def dialog(title: str, message: str, buttons: list) -> int:
    """Floating dialog; returns the index of the clicked button (0 = first, the default)."""
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(message)
    for b in buttons:
        a.addButtonWithTitle_(b)
    float_above(a)
    return a.runModal() - NSAlertFirstButtonReturn


def alert(title: str, message: str = "", ok: str = "OK", cancel: str = None) -> int:
    """Like rumps.alert, but floats above other windows. Returns 1 for ok, 0 for cancel."""
    return int(dialog(title, message, [ok, cancel] if cancel else [ok]) == 0)


def notify(title: str, message: str, subtitle: str = "") -> None:
    """Send a system notification via osascript (more reliable than rumps' built-in notifications for unbundled Python)."""
    script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
    if subtitle:
        script += f' subtitle "{_esc(subtitle)}"'
    subprocess.run(["osascript", "-e", script], check=False)


def choose_folder(prompt: str, default: str = "/Volumes"):
    """Folder picker; starts in /Volumes so external drives are one click away. Runs in-process (a modal
    NSOpenPanel), so the app keeps handling events while it's open instead of showing the spinning cursor."""
    panel = NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setCanCreateDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setMessage_(prompt)
    panel.setPrompt_("Choose")
    if default and Path(default).is_dir():
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(default))
    activate_app()
    if panel.runModal() != 1:  # NSModalResponseOK
        return None
    path = panel.URL().path()
    return Path(path.rstrip("/") or "/")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def drive_label(path: Path) -> str:
    """Drive name ("PhotoA" for /Volumes/PhotoA/...), or "this Mac" for the internal disk."""
    parts = Path(path).parts
    return parts[2] if len(parts) > 2 and parts[1] == "Volumes" else "this Mac"


def free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def read_only_message(path: Path) -> str:
    return (f"{drive_label(path)} is read-only. If it's an NTFS (Windows) drive, macOS can only read it — "
            f"choose a folder on an APFS or exFAT drive instead.")


def read_only_alert(path: Path) -> None:
    alert("Destination is read-only", f"{path} can't be written to.\n\n{read_only_message(path)}")


def check_destination(path: Path, card: Path = None) -> str:
    """Why `path` can't be used as an import destination, or "" if it can."""
    if not path.is_dir():
        return f"{path} isn't connected."
    if card and (path == card or card in path.parents):
        return "Can't import onto the memory card itself — choose a folder on another drive."
    if not is_writable(path):
        return read_only_message(path)
    return ""
