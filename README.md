# Photoman: a small photo import tool (v1)

Insert a Nikon memory card → an import window asks where to import and for the trip name → files are archived to that folder (typically on an external drive) and verified one by one → you get a "safe to format the card" notification.

## Install

```bash
git clone <this repository> photo-manager
cd photo-manager
bash install.sh
```

This builds `~/Applications/Photoman.app` (a menu bar app, no Dock icon) and starts it at login. A 📷 icon appears in the menu bar.

The app runs the code in this folder, so keep the folder where it is; after changing the code, quit and reopen Photoman.

## Import destination

Photos go wherever you choose, typically a folder on an external drive where you'll also do your editing:

- When a card is inserted, the import window shows the card (files, size, dates), a destination menu with free space, and a trip name field with a preview of the folders that will be created
- Pick a recent destination or **Choose Other Folder…**; read-only drives (e.g. NTFS) and the card itself are refused
- The last destination is remembered; if its drive isn't connected, the most recently used connected one is suggested
- **Photos**: import everything, **only some days** (tick the shooting days), or **Choose photos…** (a Finder-style picker on the card: ⌘/Shift-click, Space to preview). The RAW/JPEG partner of a chosen photo always comes along. Files left out stay on the card, so it isn't reported safe to format; insert it again later to import the rest (what's already imported is skipped)
- Progress and the result (safe to format or not, with Eject Card / Show in Finder) appear in the same window. **Stop** halts an import between files; what's done is kept and the next import carries on
- Next to the 📷 in the menu bar: `⇣ 12/160` for an import, `⇪` for a travel-drive backup and `☁ 155/1175` for Baidu Netdisk uploads (uploaded / everything that belongs in the cloud). When an import and an upload run together they're shown on two small lines, import on top
- **Import Destination ▸** in the menu lists recent destinations (✓ = current) and lets you switch or choose a new folder
- If the card is larger than the free space, you're warned before importing

Each destination keeps its own index, so duplicate detection works within a destination, not across drives.

## Archive layout

```
<destination>/2026/2026-09-20_Rome/DSC_1234.NEF      ← every photo of the trip, whatever day it was shot
<destination>/2026/2026-09-20_Rome_Edited/          ← empty; export your Lightroom edits here
```

- **One folder per trip**, named after the trip's first day. A later card from the same trip (same name, within 30 days) goes into the same folder. Without a trip name, each import gets one folder named after its first day. To get one folder per shooting day instead, set `"group_by": "day"` in `~/.photoman/config.json`
- Re-inserting a card never imports the same photo twice (hashes are recorded in `.photoman/index.sqlite` inside the destination)
- Files with the same name but different content (camera file numbering wrapped around) are renamed to `DSC_1234_1.NEF`, never overwritten
- Each file is first written as a `.part` temp file, re-read and checked against its SHA-256, and only then renamed to its final name
- **The card is read-only**: files on it are never modified or deleted; format the card manually in the camera
- If a travel backup drive is set, see [Travel backup](#travel-backup) below

## Reorganizing after import

You can move, rename and sort imported photos into subfolders (or other folders in the destination) whenever you like, even while a backup is running:

- Photoman tracks originals by content (SHA-256), not by path. When a photo isn't where it was imported, it's found again by size and hash and its record is updated
- Re-inserting the card doesn't import moved or renamed photos again
- A photo moved before it was backed up / uploaded is still backed up (at its new place); one moved afterwards isn't copied again: the backup drive and Baidu Netdisk keep the layout from when each file was first copied, and nothing there is moved or deleted
- Photos you delete are noted once and not searched for again; their backup copies stay
- Trips keep counting their photos wherever they've been moved (for imports made with this version or later)
- `_Edited` folders can be moved up to three levels deep inside a year folder and are still backed up

## Edited folder (Lightroom exports)

Each trip gets one empty `<first date>_<trip>_Edited` folder next to its date folders, e.g. `2026/2026-09-20_Rome_Edited/`. A later card from the same trip (same name, within 30 days) reuses it. Export your finished edits from Lightroom Classic into it (subfolders are fine). **Open Edited Folder** in the menu opens the last one, and **Show in Finder** selects it along with the date folders.

## Trips

Importing, editing and backing up often happen days or weeks apart. **Trips…** in the menu lists every import, grouped by trip, in the order they were first imported (it updates by itself while open):

| Column | Shows |
|---|---|
| Photos | Imported originals still in the destination, and how many are JPEG |
| Edited | Files in the trip's Edited folder and when the last one was exported, or "not started" |
| Backup | ✓ all backed up, or how many originals / edits are still waiting |
| Baidu Netdisk | uploaded / total for the trip (adds up to the menu bar's ☁ count), or ✓ uploaded |

Select a trip to **Show Originals** or **Open Edited Folder** (double-click works too). The list is built from the import logs kept in each destination (`.photoman/imports/`), so it includes imports from any day. Trips on drives that aren't connected are still listed, greyed out. Photoman remembers every destination you've used, and catch-up backups cover all of the connected ones.

## Travel backup

Set a folder on a second drive with **Set Travel Backup Drive…** (or `photoman set-backup <path>`). Then:

- Each imported original also gets a verified copy there, under the same `YYYY/YYYY-MM-DD_trip/` path
- The import window shows whether the backup drive is connected
- **"Safe to format" means two verified copies**: until every file on the card is on both the destination and the backup drive, Photoman tells you to keep the card
- If the backup drive wasn't connected, plugging it in later backs up the missing files automatically (or use **Back Up Now** / `photoman backup`); the last card then becomes safe to format
- The menu shows how many files are waiting to be backed up
- Edited folders are backed up too: Photoman checks for new exports every 5 minutes (and when the drive is connected). Files changed in the last minute are left for the next round, since Lightroom may still be writing them. A re-exported file replaces its older copy on the backup
- **RAW + JPEG**: by default the backup drive keeps the RAW and skips JPEGs that have a RAW twin (same name, same folder). JPEG-only shots and videos are still backed up. Set `"backup_originals": "all"` in `~/.photoman/config.json` to back up the JPEGs too
- Nothing is ever deleted from the backup drive: originals and exports you delete from the library stay there
- Edits don't affect "safe to format", which is only about the card's originals

## Baidu Netdisk (百度网盘)

Photoman can also upload to Baidu Netdisk through Baidu's official open platform API, straight from the library drive (nothing is copied to the Mac's own disk).

**What's uploaded**: JPEG/HEIF originals (not RAW or video) plus everything in the Edited folders. Change `"cloud_originals"` in `~/.photoman/config.json` to `"all"`, `"raw"` or `"none"`. The cloud doesn't count toward "safe to format" (that's the destination + travel backup drive).

**Setup (once)**:
1. At [pan.baidu.com/union](https://pan.baidu.com/union) sign in, complete the developer verification and create an app. Note its **AppKey**, **SecretKey** and **name**
2. Photoman menu → **Set Up Baidu Netdisk…** (or `photoman baidu-setup`), enter the three values
3. A Baidu page opens; sign in and enter the code Photoman shows. Done

Uploads then run in the background whenever the library drive is connected, and resume where they left off on any day. Files land in `我的应用数据/<app name>/` (`/apps/<app name>/`) with the same `YYYY/…` folders. To upload somewhere else, set `"cloud_root"` in `~/.photoman/config.json`, e.g. `"/照片备份"` (the whole netdisk, `/`, isn't allowed); changing it uploads everything again to the new folder. Each chunk's MD5 is checked against what Baidu received. A re-exported edit replaces its older upload; nothing is deleted in the cloud. The menu and the Trips window show what's still waiting. Credentials are kept in `~/.photoman/baidu.json` (readable only by you).

## Lightroom Classic

After importing, in LrC choose File → Import, select the matching date folders in the destination, and choose **Add** (not Copy or Move).

## Command line

```bash
~/.photoman/photoman status
~/.photoman/photoman cards
~/.photoman/photoman import /Volumes/NIKON_Z --trip Rome --dry-run   # simulate first
~/.photoman/photoman import /Volumes/NIKON_Z --trip Rome
~/.photoman/photoman import /Volumes/NIKON_Z --trip Rome --only 'DSC_01*'                  # only some files
~/.photoman/photoman import /Volumes/NIKON_Z --trip Rome --library /Volumes/PhotoB/Photos   # one-off destination
~/.photoman/photoman set-library /Volumes/PhotoA/Photos                                    # change the default
~/.photoman/photoman set-backup /Volumes/TravelSSD/Photos                                  # travel backup (none = off)
~/.photoman/photoman backup                                                                # catch up the travel backup
~/.photoman/photoman baidu-setup                                                           # connect Baidu Netdisk
~/.photoman/photoman baidu-upload                                                          # upload what's waiting
```

## Tests

```bash
pip install pytest pillow && python -m pytest -q
```

## Uninstall

`bash uninstall.sh` (the photo library and config are kept)
