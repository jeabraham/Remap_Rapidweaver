# RapidWeaver Path Manager (`Remap_Rapidweaver`)

A command-line tool for inspecting, repairing, internalizing, and remapping file references and image paths in **RapidWeaver Classic (`.rwc`)** and **RapidWeaver 8 (`.rw8`)** project bundles.

---

## The Problem

When you work on a RapidWeaver website project across multiple Macs (for example, between an iMac and a MacBook, or collaborating via Dropbox, Google Drive, or iCloud), external asset links often break.

This happens because RapidWeaver records:
1. **Absolute filesystem paths** (e.g. `/Volumes/ExternalDrive/Dropbox/...` or `/Users/username/...`).
2. **Machine-specific macOS security-scoped bookmarks** (`documentScopedBookmark` / `fallbackAppScopedBookmark`) tied to the specific disk volume UUID and inode of the machine where the file was added.
3. **Stacks plugin `storedFiles` entries** that record the absolute path of the project bundle on the original machine.

When opened on another computer where the username, drive name, or mount point differs, RapidWeaver marks external resources as missing, leaving you with broken image links and red warning badges.

---

## The Solution

`rapidweaver_path_manager.py` fixes this by providing two migration paths:

1. **Internalization (Recommended - 100% Portable)**:
   Converts external file links into native RapidWeaver internal project resources (`storedInsideDocument: True`). The assets are copied directly into the project bundle directory (`Resources/<UUID>/`). Bookmarks and external path dependencies are completely eliminated. **You can copy the `.rwc` or `.rw8` project to ANY Mac and it will open cleanly with zero broken links.**

2. **External Remapping**:
   If you prefer to keep asset files outside the project bundle (to minimize project file size), you can remap external references to a new directory and regenerate fresh macOS security-scoped bookmarks.

3. **Stacks Path Rebasing**:
   Automatically corrects Stacks plugin internal `storedFiles` paths in page bundles so Stacks images locate their local bundle assets properly.

---

## Features

- **inspect**: Audits all file references in a project. Shows counts of internal vs. external files, volume distribution, missing/broken paths, and Stacks issues.
- **find-assets**: Recursively searches asset directories using intelligent matching:
  - Exact filename match
  - Case-insensitive match
  - Normalized matching (treating spaces, dashes, and underscores interchangeably)
  - Extension alias matching (`.jpeg` <-> `.jpg`, `.tiff` <-> `.tif`)
- **internalize**: Automatically resolves all external assets, copies them into the bundle, registers them in `Resources/Contents.plist`, updates `RWFileReferences` to `storedInsideDocument: True`, rebases Stacks paths, and removes machine-locked bookmarks.
- **remap**: Re-anchors external paths to a new folder on the current machine and optionally generates fresh macOS security-scoped bookmarks using Swift.
- **rebase-stacks**: Updates Stacks page-level `storedFiles` paths to point to the current location of the project bundle on disk.
- **Safe by Default**:
  - Automatically creates a timestamped project backup (`<Project>_backup_YYYYMMDD_HHMMSS`) before modifying anything.
  - Supports `--dry-run` to preview changes without modifying files.
  - Supports `--export-map <file.json>` to export audit and mapping logs.

---

## Requirements

- **macOS** (compatible with macOS Monterey, Ventura, Sonoma, Sequoia, and later)
- **Python 3.7+** (uses standard library modules: `plistlib`, `shutil`, `json`, `argparse`)
- *(Optional)* **Swift** (included by default on macOS with Xcode Command Line Tools; only needed if using `--refresh-bookmarks` with `remap`).

---

## Quick Start

Make the script executable:
```bash
chmod +x rapidweaver_path_manager.py
```

### 1. Audit a Project
Inspect a RapidWeaver project to see how many references are external or broken:
```bash
./rapidweaver_path_manager.py inspect MySite.rwc
```

### 2. Search for Missing Assets
Test whether missing files can be found in your local folders or backups:
```bash
./rapidweaver_path_manager.py find-assets MySite.rwc --search-dirs ~/Pictures ~/Dropbox/Assets
```

### 3. Make Project 100% Portable (Internalize)
Bundle all external files directly into the project package:
```bash
./rapidweaver_path_manager.py internalize MySite.rwc --search-dirs ./Pictures ~/Dropbox/Assets
```
*Note: A backup of `MySite.rwc` is created automatically before any changes are written.*

To test without making changes:
```bash
./rapidweaver_path_manager.py internalize MySite.rwc --search-dirs ./Pictures ~/Dropbox/Assets --dry-run
```

### 4. Remap External Links (Alternative)
If you prefer keeping files external, point them to a new asset directory:
```bash
./rapidweaver_path_manager.py remap MySite.rwc --search-dirs ~/Pictures/SiteAssets --refresh-bookmarks
```

### 5. Fix Stacks Image Links Only
If only Stacks images inside pages are showing broken paths after moving the project:
```bash
./rapidweaver_path_manager.py rebase-stacks MySite.rwc
```

---

## How RapidWeaver Stores References

Inside a `.rwc` or `.rw8` package:

- **Contents.plist -> RWFileReferences**: A dictionary keyed by UUID (the `FileToken`).
  - When **Internal (`storedInsideDocument: True`)**:
    The asset lives at `Resources/<UUID>/filename` with its own `Contents.plist`, and `Resources/Contents.plist` includes the UUID in its `Subsandwiches` list.
  - When **External (`storedInsideDocument: False`)**:
    Contains machine-specific `lastResolvablePath`, `volumeName`, `volumePath`, `documentScopedBookmark`, and `fallbackAppScopedBookmark`.

- **Pages/*/Data/Contents.plist -> storedFiles**:
  Stacks records absolute paths for page image assets. When the `.rwc` bundle is moved or opened on another computer, these paths become stale unless rebased.

---

## Testing

Run the included unit test suite:
```bash
python3 -m unittest discover -s tests -v
```

---

## License

This project is licensed under the MIT License.
