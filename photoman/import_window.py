"""The import window: card info, which photos, destination, trip name → progress → result, all in one window.

Driven by the menu bar app (menubar.py), which owns the import thread; this module is only the UI.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List

import objc
from AppKit import (
    NSBackingStoreBuffered, NSButton, NSColor, NSFloatingWindowLevel, NSFont, NSLayoutAttributeLeading,
    NSMenuItem, NSOpenPanel, NSPopUpButton, NSProgressIndicator, NSStackView, NSTextField, NSView, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSMakeRect, NSObject

from .importer import dest_folder, sanitize_trip, with_partners
from .macui import activate_app, alert, check_destination, choose_folder, drive_label, free_space, human_size

WIDTH = 460
CHOOSE = "__choose__"
LEVEL_COLORS = {"info": NSColor.secondaryLabelColor, "ok": NSColor.systemGreenColor,
                "warn": NSColor.systemOrangeColor, "error": NSColor.systemRedColor}


def date_range_label(dates: List[datetime]) -> str:
    lo, hi = min(dates), max(dates)
    if lo.date() == hi.date():
        return f"{lo:%b} {lo.day}, {lo.year}"
    if lo.year == hi.year:
        return f"{lo:%b} {lo.day} – {hi:%b} {hi.day}, {hi.year}"
    return f"{lo:%b} {lo.day}, {lo.year} – {hi:%b} {hi.day}, {hi.year}"


def destination_title(path: Path) -> str:
    if not path.is_dir():
        return f"{path.name} — {drive_label(path)} (not connected)"
    if check_destination(path):
        return f"{path.name} — {drive_label(path)} (read-only)"
    return f"{path.name} — {drive_label(path)} · {human_size(free_space(path))} free"


def _label(text="", size=13, bold=False, color=None, wrap=False):
    f = NSTextField.wrappingLabelWithString_(text) if wrap else NSTextField.labelWithString_(text)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if color is not None:
        f.setTextColor_(color)
    if wrap:
        f.setPreferredMaxLayoutWidth_(WIDTH)
        f.setSelectable_(True)
    return f


def _fixed_width(view, width=WIDTH):
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    return view


def _vstack(views, spacing=6):
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(1)  # vertical
    s.setAlignment_(NSLayoutAttributeLeading)
    s.setSpacing_(spacing)
    s.setDetachesHiddenViews_(True)
    return s


class ImportWindow(NSObject):
    """States: "ready" (choosing) → "running" (progress) → "done" (result)."""

    @objc.python_method
    def setup(self, app, card: Path, files: List[Path], dates: List[datetime], destinations: List[str],
              current: Path, trip: str):
        self.app, self.card, self.files, self.dates = app, card, files, dates
        self.sizes = {str(f): f.stat().st_size for f in files}
        self.date_of = {str(f): d for f, d in zip(files, dates)}
        self.selected = None        # None: everything on the card; otherwise the chosen card files (as str)
        self.scope_index = 0
        self.state = "ready"
        self.extra_destinations = []  # chosen in this window, not yet remembered

        # --- header
        self.title_label = _label(f"Import from “{card.name}”", size=16, bold=True)
        info = f"{len(files)} files · {human_size(sum(self.sizes.values()))} · shot {date_range_label(dates)}"
        self.info_label = _fixed_width(_label(info, color=NSColor.secondaryLabelColor(), wrap=True))

        # --- which photos
        self.scope_popup = _fixed_width(NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, WIDTH, 26), False))
        for title in (f"All {len(files)} files", "Only some days…", "Choose photos…"):
            self.scope_popup.addItemWithTitle_(title)
        self.scope_popup.setTarget_(self)
        self.scope_popup.setAction_("scopeChanged:")
        by_day = {}
        for f, d in zip(files, dates):
            by_day.setdefault(d.date(), []).append(str(f))
        self.day_checks = []
        for day, day_files in sorted(by_day.items()):
            size = sum(self.sizes[f] for f in day_files)
            cb = NSButton.checkboxWithTitle_target_action_(
                f"{day:%a}, {day:%b} {day.day} · {len(day_files)} files · {human_size(size)}", self, "dayToggled:")
            cb.setState_(1)
            self.day_checks.append((cb, day_files))
        self.days_box = _vstack([cb for cb, _ in self.day_checks], spacing=4)
        self.days_box.setHidden_(True)
        self.selection_label = _fixed_width(_label(wrap=True, size=12, color=NSColor.secondaryLabelColor()))
        self.selection_label.setHidden_(True)

        # --- form
        self.dest_popup = _fixed_width(NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, WIDTH, 26), False))
        self.dest_popup.menu().setAutoenablesItems_(False)
        self.dest_popup.setTarget_(self)
        self.dest_popup.setAction_("destinationChanged:")
        self.dest_warning = _fixed_width(_label(wrap=True, size=12))
        self.backup_label = _fixed_width(_label(wrap=True, size=12))
        self.trip_field = _fixed_width(NSTextField.textFieldWithString_(trip))
        self.trip_field.setPlaceholderString_("e.g. Rome — leave empty to archive by date only")
        self.trip_field.setDelegate_(self)
        self.preview_label = _fixed_width(_label(wrap=True, size=11, color=NSColor.secondaryLabelColor()))
        self.form = _vstack([
            _label("Photos", bold=True), self.scope_popup, self.days_box, self.selection_label,
            _label("Import to", bold=True), self.dest_popup, self.dest_warning, self.backup_label,
            _label("Trip name", bold=True), self.trip_field, self.preview_label,
        ])
        for v in (self.scope_popup, self.days_box, self.selection_label, self.dest_popup):
            self.form.setCustomSpacing_afterView_(14, v)
        self.form.setCustomSpacing_afterView_(14, self.dest_warning)
        self.form.setCustomSpacing_afterView_(14, self.backup_label)

        # --- progress
        self.progress_bar = _fixed_width(NSProgressIndicator.alloc().init())
        self.progress_bar.setIndeterminate_(False)
        self.progress_bar.setMinValue_(0)
        self.progress_bar.setMaxValue_(max(len(files), 1))
        self.progress_label = _fixed_width(_label(size=12, color=NSColor.secondaryLabelColor()))
        self.progress_box = _vstack([self.progress_bar, self.progress_label])
        self.progress_box.setHidden_(True)

        # --- result
        self.result_title = _fixed_width(_label(wrap=True, size=15, bold=True))
        self.result_detail = _fixed_width(_label(wrap=True, size=12))
        self.result_box = _vstack([self.result_title, self.result_detail], spacing=8)
        self.result_box.setHidden_(True)

        # --- buttons
        def button(title, action, key=None):
            b = NSButton.buttonWithTitle_target_action_(title, self, action)
            if key:
                b.setKeyEquivalent_(key)
            return b
        self.cancel_button = button("Cancel", "cancelClicked:", "\x1b")
        self.import_button = button("Import", "importClicked:", "\r")
        self.logs_button = button("Open Import Logs", "logsClicked:")
        self.reveal_button = button("Show in Finder", "revealClicked:")
        self.done_button = button("Done", "doneClicked:")
        self.eject_button = button("Eject Card", "ejectClicked:")
        for b in (self.logs_button, self.reveal_button, self.done_button, self.eject_button):
            b.setHidden_(True)
        spacer = NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, 0)
        self.buttons = NSStackView.stackViewWithViews_([
            self.logs_button, spacer, self.reveal_button, self.cancel_button, self.done_button,
            self.import_button, self.eject_button,
        ])
        _fixed_width(self.buttons)
        self.buttons.setDetachesHiddenViews_(True)

        root = _vstack([self.title_label, self.info_label, self.form, self.progress_box, self.result_box,
                        self.buttons], spacing=10)
        root.setCustomSpacing_afterView_(18, self.info_label)
        root.setEdgeInsets_((20, 20, 20, 20))
        root.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.root = root

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH + 40, 300), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_("Photoman")
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        content = self.window.contentView()
        content.addSubview_(root)
        for a, b in ((root.topAnchor(), content.topAnchor()), (root.bottomAnchor(), content.bottomAnchor()),
                     (root.leadingAnchor(), content.leadingAnchor()), (root.trailingAnchor(), content.trailingAnchor())):
            a.constraintEqualToAnchor_(b).setActive_(True)

        self.fill_destinations(destinations, current)
        self.update_preview()
        self.show()
        return self

    # ------------------------------------------------------------ window

    @objc.python_method
    def show(self):
        self.fit()
        self.window.center()
        self.window.setLevel_(NSFloatingWindowLevel)  # can't take focus from a background app on recent macOS
        activate_app()
        self.window.makeKeyAndOrderFront_(None)
        if self.state == "ready":
            self.window.makeFirstResponder_(self.trip_field)

    @objc.python_method
    def fit(self):
        """Shrink or grow the window to its content (sections are shown and hidden as the state changes)."""
        self.root.layoutSubtreeIfNeeded()
        self.window.setContentSize_(self.root.fittingSize())

    @objc.python_method
    def close(self):
        self.window.close()

    def windowShouldClose_(self, _sender):
        return self.state != "running"

    def windowWillClose_(self, _notification):
        self.app.import_window_closed(self)

    # ------------------------------------------------------------ which photos

    @objc.python_method
    def chosen(self) -> List[str]:
        return [str(f) for f in self.files] if self.selected is None else sorted(self.selected)

    @objc.python_method
    def chosen_size(self) -> int:
        return sum(self.sizes[f] for f in self.chosen())

    def scopeChanged_(self, _sender):
        i = self.scope_popup.indexOfSelectedItem()
        if i == 0:
            self.selected = None
        elif i == 1:
            self.selected = {f for cb, day_files in self.day_checks if cb.state() for f in day_files}
        else:
            picked = self.pick_photos()
            if picked is None:  # cancelled: keep what was selected before
                self.scope_popup.selectItemAtIndex_(self.scope_index)
                return
            self.selected = picked
        self.scope_index = i
        self.days_box.setHidden_(i != 1)
        self.update_selection()

    def dayToggled_(self, _sender):
        self.selected = {f for cb, day_files in self.day_checks if cb.state() for f in day_files}
        self.update_selection()

    @objc.python_method
    def pick_photos(self):
        """Finder-style picker on the card (⌘/Shift-click for several, Space to preview). RAW/JPEG partners of
        picked files are added, so pairs stay together."""
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(True)
        start = self.card / "DCIM" if (self.card / "DCIM").is_dir() else self.card
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(start)))
        panel.setMessage_("Select the photos to import — ⌘-click or Shift-click for several, Space to preview. "
                          "The RAW/JPEG partner of each photo is included automatically.")
        panel.setPrompt_("Select")
        activate_app()
        if panel.runModal() != 1:
            return None
        on_card = {str(f) for f in self.files}
        picked = {str(Path(u.path())) for u in panel.URLs()} & on_card  # ignore anything that isn't a card photo
        return with_partners(picked, self.files)

    @objc.python_method
    def update_selection(self):
        n, total = len(self.chosen()), len(self.files)
        if self.selected is None:
            self.selection_label.setHidden_(True)
            self.import_button.setTitle_("Import")
        else:
            if n:
                text = f"{n} of {total} files selected · {human_size(self.chosen_size())}."
                if n < total:
                    text += " The rest stay on the card — import them later before formatting it."
                if self.scope_index == 2:
                    text += " RAW/JPEG partners included."
                color = NSColor.secondaryLabelColor()
            else:
                text, color = "Nothing selected.", NSColor.systemOrangeColor()
            self.selection_label.setStringValue_(text)
            self.selection_label.setTextColor_(color)
            self.selection_label.setHidden_(False)
            self.import_button.setTitle_(f"Import {n}" if n else "Import")
        self.validate()
        self.update_preview()

    # ------------------------------------------------------------ destination

    @objc.python_method
    def fill_destinations(self, destinations: List[str], current: Path):
        menu = self.dest_popup.menu()
        menu.removeAllItems()
        paths = [*self.extra_destinations, *[p for p in destinations if p not in self.extra_destinations]]
        for p in paths:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(destination_title(Path(p)), None, "")
            item.setRepresentedObject_(p)
            item.setToolTip_(p)
            item.setEnabled_(Path(p).is_dir())
            menu.addItem_(item)
        if paths:
            menu.addItem_(NSMenuItem.separatorItem())
        choose = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Choose Other Folder…", None, "")
        choose.setRepresentedObject_(CHOOSE)
        menu.addItem_(choose)
        if current is not None and str(current) in paths:
            self.dest_popup.selectItemAtIndex_(paths.index(str(current)))
        else:
            self.dest_popup.selectItem_(choose)
        self.previous = self.selected_destination()
        self.validate()

    @objc.python_method
    def selected_destination(self):
        item = self.dest_popup.selectedItem()
        p = item.representedObject() if item else None
        return Path(p) if p and p != CHOOSE else None

    def destinationChanged_(self, _sender):
        if self.dest_popup.selectedItem().representedObject() == CHOOSE:
            chosen = choose_folder("Choose where to import the photos (e.g. a folder on your external drive)")
            self.show()
            if chosen:
                self.extra_destinations = [str(chosen), *[p for p in self.extra_destinations if p != str(chosen)]]
                self.fill_destinations(self.app.recent_libraries(), chosen)
            else:
                self.fill_destinations(self.app.recent_libraries(), self.previous)
            return
        self.previous = self.selected_destination()
        self.validate()
        self.update_preview()

    @objc.python_method
    def validate(self):
        lib = self.selected_destination()
        problem = "Choose where to import the photos." if lib is None else check_destination(lib, self.card)
        warning, color = problem, NSColor.systemRedColor()
        size = self.chosen_size()
        if not problem and size > free_space(lib):
            warning, color = (f"Only {human_size(free_space(lib))} free on {drive_label(lib)}, and the selected "
                              f"files take {human_size(size)}. Files imported before are skipped, so it may still fit."
                              ), NSColor.systemOrangeColor()
        self.dest_warning.setStringValue_(warning)
        self.dest_warning.setTextColor_(color)
        self.dest_warning.setHidden_(not warning)
        self.import_button.setEnabled_(not problem and bool(self.chosen()))
        self.update_backup_label()
        self.fit()
        return not problem

    @objc.python_method
    def update_backup_label(self):
        status = self.app.backup_status()
        if status is None:
            self.backup_label.setHidden_(True)
            return
        text, level = status
        self.backup_label.setStringValue_(text)
        self.backup_label.setTextColor_(LEVEL_COLORS[level]())
        self.backup_label.setHidden_(False)

    # ------------------------------------------------------------ trip name

    def controlTextDidChange_(self, _notification):
        self.update_preview()

    @objc.python_method
    def update_preview(self):
        lib = self.selected_destination()
        if lib is None:
            self.preview_label.setHidden_(True)
            return
        dates = [self.date_of[f] for f in self.chosen()]
        if not dates:
            self.preview_label.setHidden_(True)
            self.fit()
            return
        trip = sanitize_trip(self.trip_field.stringValue())
        days = sorted({d.date() for d in dates})
        first = dest_folder(lib, min(dates), trip)
        more = f"  (+{len(days) - 1} more date folder{'s' if len(days) > 2 else ''})" if len(days) > 1 else ""
        self.preview_label.setStringValue_(f"→ {first}{more}")
        self.preview_label.setHidden_(False)
        self.fit()

    # ------------------------------------------------------------ actions

    def importClicked_(self, _sender):
        lib = self.selected_destination()
        if not self.validate():
            return
        if self.chosen_size() > free_space(lib) and alert(
                "Destination may be too full", self.dest_warning.stringValue(),
                ok="Import Anyway", cancel="Cancel") != 1:
            return
        only = None if self.selected is None else sorted(self.selected)
        if not self.app.begin_import(self.card, lib, self.trip_field.stringValue().strip(), only):
            return
        self.state = "running"
        self.form.setHidden_(True)
        self.progress_box.setHidden_(False)
        self.cancel_button.setHidden_(True)
        self.import_button.setEnabled_(False)
        self.import_button.setTitle_("Importing…")
        what = "" if self.selected is None else f"{len(self.selected)} selected files "
        self.info_label.setStringValue_(f"Importing {what}to {lib.name} on {drive_label(lib)}")
        self.show_progress(0, len(self.files), "")
        self.fit()

    def cancelClicked_(self, _sender):
        self.close()

    def doneClicked_(self, _sender):
        self.close()

    def revealClicked_(self, _sender):
        self.app.on_reveal_last(None)

    def logsClicked_(self, _sender):
        self.app.on_open_logs(None)

    def ejectClicked_(self, _sender):
        if self.app.eject_card():
            self.close()

    # ------------------------------------------------------------ progress & result

    @objc.python_method
    def show_progress(self, i: int, n: int, name: str):
        self.progress_bar.setMaxValue_(max(n, 1))
        self.progress_bar.setDoubleValue_(i)
        self.progress_label.setStringValue_(f"{i} / {n}  {name}" if name else "Starting…")

    @objc.python_method
    def show_result(self, title: str, detail: str, level: str):
        """level: "ok" (safe to format), "warn" (imported, backup pending) or "error"."""
        self.state = "done"
        self.progress_box.setHidden_(True)
        self.form.setHidden_(True)
        self.info_label.setHidden_(True)
        self.result_title.setStringValue_(title)
        self.result_title.setTextColor_(LEVEL_COLORS[level]())
        self.result_detail.setStringValue_(detail)
        self.result_box.setHidden_(False)
        self.import_button.setHidden_(True)
        self.reveal_button.setHidden_(False)
        ejectable = level != "error"  # a card that's only waiting for its backup can still be ejected
        self.logs_button.setHidden_(level != "error")
        self.done_button.setHidden_(False)
        self.eject_button.setHidden_(not ejectable)
        (self.eject_button if level == "ok" else self.done_button).setKeyEquivalent_("\r")
        self.show()
