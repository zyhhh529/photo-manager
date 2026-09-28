"""The Trips window: every import grouped by trip, with editing and backup status."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import objc
from AppKit import (
    NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSScrollView, NSTableColumn, NSTableView, NSTextField,
    NSViewHeightSizable, NSViewMaxXMargin, NSViewMinXMargin, NSViewWidthSizable, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled, NSWorkspace,
)
from Foundation import NSURL, NSIndexSet, NSMakeRect, NSObject

from . import baidu, config
from .macui import activate_app, alert, drive_label
from .trips import Trip, load_trips

W, H = 1120, 440
COLUMNS = [  # identifier, title, width
    ("trip", "Trip", 170),
    ("dates", "Dates", 150),
    ("dest", "Destination", 150),
    ("photos", "Photos", 60),
    ("edited", "Edited", 160),
    ("backup", "Backup drive", 160),
    ("cloud", "Baidu Netdisk", 140),
]


def _short_date(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}" + (f", {d.year}" if d.year != datetime.now().year else "")


def cell_text(t: Trip, column: str) -> str:
    if column == "trip":
        return t.title if t.name else f"{t.title} (no name)"
    if column == "dates":
        return t.date_range
    if column == "dest":
        return f"{Path(t.library_root).name} on {drive_label(Path(t.library_root))}"
    if column == "photos":
        return str(t.photos)
    if column == "edited":
        if not t.connected:
            return f"{t.edited} files" if t.edited else "—"
        if not t.edited:
            return "not started"
        return f"{t.edited} files · last {_short_date(t.last_export)}"
    if column == "backup":
        if not t.connected:
            return "drive not connected"
        if t.backup_pending is None:
            return "backup drive not connected"
        if t.backup_pending == 0:
            return "✓ all backed up"
        parts = [f"{t.originals_pending} originals"] * bool(t.originals_pending) + \
                [f"{t.edited_pending} edits"] * bool(t.edited_pending)
        return " + ".join(parts) + " waiting"
    if column == "cloud":
        if t.cloud_pending is None:
            return "—"
        if not t.connected:
            return "drive not connected"
        return "✓ uploaded" if t.cloud_pending == 0 else f"{t.cloud_pending} waiting"
    return ""


def cell_color(t: Trip, column: str):
    if not t.connected:
        return NSColor.tertiaryLabelColor()
    if column == "backup":
        if t.backup_pending is None:
            return NSColor.secondaryLabelColor()
        return NSColor.systemGreenColor() if t.backup_pending == 0 else NSColor.systemOrangeColor()
    if column == "cloud":
        if t.cloud_pending is None:
            return NSColor.secondaryLabelColor()
        return NSColor.systemGreenColor() if t.cloud_pending == 0 else NSColor.systemOrangeColor()
    if column == "edited" and not t.edited:
        return NSColor.secondaryLabelColor()
    return NSColor.labelColor()


class TripsWindow(NSObject):

    @objc.python_method
    def setup(self, app):
        self.app = app
        self.trips = []
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
            | NSWindowStyleMaskMiniaturizable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_("Photoman — Trips")
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.window.setMinSize_((720, 280))
        self.window.setFrameAutosaveName_("PhotomanTrips")
        content = self.window.contentView()

        self.table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 40, H - 80))
        for ident, title, width in COLUMNS:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.setTitle_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            self.table.addTableColumn_(col)
        self.table.setColumnAutoresizingStyle_(4)  # last column takes up spare width when resizing
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setRowHeight_(22)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setTarget_(self)
        self.table.setDoubleAction_("openEdited:")
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 60, W - 40, H - 80))
        scroll.setDocumentView_(self.table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # bezel
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(scroll)

        self.status = NSTextField.labelWithString_("")
        self.status.setFont_(NSFont.systemFontOfSize_(12))
        self.status.setTextColor_(NSColor.secondaryLabelColor())
        self.status.setFrame_(NSMakeRect(20, 22, 360, 18))
        self.status.setAutoresizingMask_(NSViewMaxXMargin)
        content.addSubview_(self.status)

        x = W - 20
        self.buttons = {}
        for key, title, action in reversed([("originals", "Show Originals", "showOriginals:"),
                                            ("edited", "Open Edited Folder", "openEdited:"),
                                            ("backup", "Back Up Now", "backUpNow:"),
                                            ("refresh", "Refresh", "refresh:")]):
            b = NSButton.buttonWithTitle_target_action_(title, self, action)
            b.sizeToFit()
            w = b.frame().size.width
            x -= w
            b.setFrameOrigin_((x, 14))
            b.setAutoresizingMask_(NSViewMinXMargin)
            content.addSubview_(b)
            self.buttons[key] = b
            x -= 8

        self.reload()
        self.window.center()
        self.show()
        return self

    # ------------------------------------------------------------ window

    @objc.python_method
    def show(self):
        activate_app()
        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()  # a background app can't take focus on recent macOS

    def windowWillClose_(self, _notification):
        self.app.trips_window_closed(self)

    # ------------------------------------------------------------ data

    @objc.python_method
    def reload(self):
        cfg = config.load_config()
        bak = cfg.get("travel_backup_root")
        selected = self.selected()
        cloud_policy = cfg.get("cloud_originals", "jpeg") if baidu.is_set_up() else None
        self.trips = load_trips(config.known_libraries(cfg), Path(bak) if bak else None, cfg["backup_originals"],
                                cloud_policy)
        self.table.reloadData()
        if selected:  # keep the selection across refreshes
            for i, t in enumerate(self.trips):
                if (t.library_root, t.edited_folder) == (selected.library_root, selected.edited_folder):
                    self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(i), False)
        pending = sum(t.backup_pending or 0 for t in self.trips if t.connected)
        if not bak:
            backup = "No travel backup drive set"
        else:
            backup = f"Travel backup {Path(bak).name}: " + (
                self.app.backup_status_word() + (f" · {pending} files waiting" if pending else ""))
        self.status.setStringValue_(f"{len(self.trips)} trips · {backup}")
        self.update_buttons()

    @objc.python_method
    def selected(self):
        row = self.table.selectedRow() if hasattr(self, "table") else -1
        return self.trips[row] if 0 <= row < len(self.trips) else None

    @objc.python_method
    def update_buttons(self):
        t = self.selected()
        usable = bool(t and t.connected)
        self.buttons["originals"].setEnabled_(usable)
        self.buttons["edited"].setEnabled_(usable)
        self.buttons["backup"].setEnabled_(self.app.can_back_up())

    # table data source / delegate
    def numberOfRowsInTableView_(self, _table):
        return len(self.trips)

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):
        return cell_text(self.trips[row], column.identifier())

    def tableView_willDisplayCell_forTableColumn_row_(self, _table, cell, column, row):
        cell.setTextColor_(cell_color(self.trips[row], column.identifier()))

    def tableViewSelectionDidChange_(self, _notification):
        self.update_buttons()

    # ------------------------------------------------------------ actions

    def showOriginals_(self, _sender):
        t = self.selected()
        folders = [t.path(f) for f in t.folders if t.path(f).is_dir()] if t else []
        if folders:
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
                [NSURL.fileURLWithPath_(str(f)) for f in folders])
        elif t:
            alert("Folders not found", "They may have been moved or deleted.")

    def openEdited_(self, _sender):
        t = self.selected()
        if not t or not t.connected:
            return
        edited = t.path(t.edited_folder)
        try:
            edited.mkdir(exist_ok=True)  # trips imported before Edited folders existed get one on demand
        except OSError as e:
            alert("Couldn't create the Edited folder", str(e))
            return
        subprocess.run(["open", str(edited)])

    def backUpNow_(self, _sender):
        self.app.on_backup_now(None)
        self.update_buttons()

    def refresh_(self, _sender):
        self.reload()
