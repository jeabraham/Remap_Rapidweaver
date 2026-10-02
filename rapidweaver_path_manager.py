#!/usr/bin/env python3
"""
RapidWeaver Path Generalization & Conversion Tool
-------------------------------------------------
Inspects, repairs, internalizes, and remaps file references in RapidWeaver
Classic project bundles (.rwc) and RapidWeaver 8 bundles (.rw8).

Features:
  - inspect: Comprehensive diagnostic report of internal/external/missing files
  - find-assets: Intelligently locates missing assets across specified search directories
  - internalize: Converts external file links into fully self-contained internal
                 project resources (storedInsideDocument: True), making the project
                 100% portable across any Mac without path dependencies.
  - remap: Re-anchors external paths to a new local directory and optionally
           regenerates macOS security-scoped bookmarks via Swift.
  - rebase-stacks: Updates Stacks page storedFiles paths to match the current
                   bundle location on disk.
"""

import os
import sys
import shutil
import re
import json
import plistlib
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set


def normalize_filename(name: str) -> str:
    """Normalize a filename by collapsing spaces, dashes, underscores and lowercasing."""
    base, ext = os.path.splitext(name)
    clean_base = re.sub(r'[-_\s]+', ' ', base).strip().lower()
    clean_ext = ext.strip().lower()
    if clean_ext == '.jpeg':
        clean_ext = '.jpg'
    elif clean_ext == '.tiff':
        clean_ext = '.tif'
    return f"{clean_base}{clean_ext}"


def get_volume_info(path: str) -> Tuple[str, str]:
    """Determine volume path and volume name on macOS."""
    abs_path = os.path.abspath(path)
    if abs_path.startswith('/Volumes/'):
        parts = abs_path.split(os.sep)
        if len(parts) >= 3:
            vol_name = parts[2]
            vol_path = f"/Volumes/{vol_name}"
            return vol_name, vol_path
    return "Macintosh HD", "/"

def get_default_search_dirs(project_path: str) -> List[str]:
    """Calculates intelligent default search directories near a RapidWeaver project."""
    proj_dir = os.path.dirname(os.path.abspath(project_path))
    parent_dir = os.path.dirname(proj_dir)
    candidates = [
        proj_dir,
        os.path.join(proj_dir, "Pictures"),
        os.path.join(proj_dir, "Photos"),
        os.path.join(proj_dir, "Assets"),
        os.path.join(proj_dir, "images"),
        parent_dir,
        os.path.join(parent_dir, "Pictures"),
        os.path.join(parent_dir, "Photos"),
        os.path.join(parent_dir, "Assets"),
    ]
    seen = set()
    result = []
    for c in candidates:
        abs_c = os.path.abspath(c)
        if abs_c not in seen and os.path.isdir(abs_c):
            seen.add(abs_c)
            result.append(abs_c)
    return result


class AssetResolver:
    """Indexes local asset directories for fast, flexible file lookup."""

    def __init__(self, search_dirs: List[str]):
        self.search_dirs = [os.path.abspath(d) for d in search_dirs if os.path.isdir(d)]
        # Map: key -> list of (priority_index, full_path)
        self.exact_map: Dict[str, List[Tuple[int, str]]] = {}
        self.norm_map: Dict[str, List[Tuple[int, str]]] = {}
        self.stem_map: Dict[str, List[Tuple[int, str]]] = {}
        self._build_index()

    def _build_index(self):
        for priority, sdir in enumerate(self.search_dirs):
            for root, _, files in os.walk(sdir):
                for f in files:
                    if f.startswith('.'):
                        continue
                    full_path = os.path.join(root, f)
                    lower_name = f.lower()
                    norm_name = normalize_filename(f)
                    stem = re.sub(r'[-_\s]+', ' ', os.path.splitext(f)[0]).strip().lower()

                    self.exact_map.setdefault(lower_name, []).append((priority, full_path))
                    self.norm_map.setdefault(norm_name, []).append((priority, full_path))
                    self.stem_map.setdefault(stem, []).append((priority, full_path))

    def resolve(self, filename: str, original_path: Optional[str] = None) -> Optional[str]:
        """Resolves a filename or path to an existing local file."""
        if not filename and original_path:
            filename = os.path.basename(original_path)
        if not filename:
            return None

        # 1. Check if original_path already exists directly
        if original_path and os.path.exists(original_path):
            return os.path.abspath(original_path)

        lower_name = filename.lower()
        # 2. Check exact match
        if lower_name in self.exact_map:
            return self._pick_best_match(self.exact_map[lower_name], original_path)

        # 3. Check normalized match (space vs dash vs underscore, jpg vs jpeg)
        norm_name = normalize_filename(filename)
        if norm_name in self.norm_map:
            return self._pick_best_match(self.norm_map[norm_name], original_path)

        # 4. Check stem match (allowing minor extension variation)
        stem = re.sub(r'[-_\s]+', ' ', os.path.splitext(filename)[0]).strip().lower()
        if stem in self.stem_map:
            return self._pick_best_match(self.stem_map[stem], original_path)

        return None

    def _pick_best_match(self, candidates: List[Tuple[int, str]], original_path: Optional[str]) -> str:
        if len(candidates) == 1:
            return candidates[0][1]

        # Sort primarily by search directory priority (lower number = higher priority)
        min_prio = min(p for p, _ in candidates)
        top_candidates = [path for p, path in candidates if p == min_prio]

        if len(top_candidates) == 1 or not original_path:
            return top_candidates[0]

        # Tie-breaker: shared path suffix components with original_path
        orig_parts = original_path.replace('\\', '/').split('/')
        best_cand = top_candidates[0]
        max_common = -1
        for cand in top_candidates:
            cand_parts = cand.replace('\\', '/').split('/')
            common = 0
            for p1, p2 in zip(reversed(orig_parts), reversed(cand_parts)):
                if p1.lower() == p2.lower():
                    common += 1
                else:
                    break
            if common > max_common:
                max_common = common
                best_cand = cand
        return best_cand


class RapidWeaverProject:
    """Manipulates a RapidWeaver .rwc / .rw8 project bundle."""

    def __init__(self, project_path: str):
        self.project_path = os.path.abspath(project_path)
        if not os.path.isdir(self.project_path):
            raise FileNotFoundError(f"Project bundle not found: {self.project_path}")

        self.contents_path = os.path.join(self.project_path, "Contents.plist")
        if not os.path.isfile(self.contents_path):
            raise FileNotFoundError(f"Missing Contents.plist in {self.project_path}")

        self.resources_dir = os.path.join(self.project_path, "Resources")
        self.resources_plist_path = os.path.join(self.resources_dir, "Contents.plist")
        self.pages_dir = os.path.join(self.project_path, "Pages")

        self.contents_data: Dict[str, Any] = {}
        self.resources_plist_data: Dict[str, Any] = {}
        self.file_refs: Dict[str, Dict[str, Any]] = {}
        self.site_resources_obj: Optional[Dict[str, Any]] = None
        self._load()

    def _load(self):
        with open(self.contents_path, "rb") as f:
            self.contents_data = plistlib.load(f)

        fillings = self.contents_data.get("SandwichFillings", [{}])
        if fillings and "Dictionary" in fillings[0]:
            self.dictionary = fillings[0]["Dictionary"]
        else:
            self.dictionary = {}

        self.file_refs = self.dictionary.get("RWFileReferences", {})

        # Load Site Resources archive if present
        sr_raw = self.dictionary.get("Site Resources")
        if sr_raw and isinstance(sr_raw, bytes):
            try:
                self.site_resources_obj = plistlib.loads(sr_raw)
            except Exception:
                self.site_resources_obj = None

        # Load Resources/Contents.plist
        if os.path.isfile(self.resources_plist_path):
            with open(self.resources_plist_path, "rb") as f:
                self.resources_plist_data = plistlib.load(f)
        else:
            self.resources_plist_data = {
                "Creator": "",
                "SandwichFillings": [{
                    "Dictionary": {},
                    "Files": [],
                    "Subsandwiches": []
                }],
                "Type": "Resources"
            }

    def inspect(self, resolver: Optional[AssetResolver] = None) -> Dict[str, Any]:
        """Audits all file references and page-level Stacks paths, checking if missing files can be recovered."""
        total_refs = len(self.file_refs)
        internal_count = 0
        external_existing = 0
        external_missing = 0
        missing_list: List[Dict[str, Any]] = []
        existing_list: List[Dict[str, Any]] = []
        internal_list: List[Dict[str, Any]] = []
        volume_distribution: Dict[str, int] = {}

        for ref_id, ref_dict in self.file_refs.items():
            is_internal = ref_dict.get("storedInsideDocument", False)
            last_path = ref_dict.get("lastResolvablePath", "")
            file_name = ref_dict.get("fileName") or (os.path.basename(last_path) if last_path else "")

            if is_internal:
                internal_count += 1
                internal_list.append({
                    "id": ref_id,
                    "fileName": file_name,
                    "disk_file_exists": os.path.exists(os.path.join(self.resources_dir, ref_id, file_name))
                })
            else:
                vol = ref_dict.get("volumeName") or (last_path.split("/")[2] if last_path.startswith("/Volumes/") else "Root")
                volume_distribution[vol] = volume_distribution.get(vol, 0) + 1

                exists = bool(last_path and os.path.exists(last_path))
                info = {
                    "id": ref_id,
                    "fileName": file_name,
                    "lastResolvablePath": last_path,
                    "volumeName": ref_dict.get("volumeName", ""),
                    "volumePath": ref_dict.get("volumePath", ""),
                }
                if exists:
                    external_existing += 1
                    existing_list.append(info)
                else:
                    external_missing += 1
                    if resolver:
                        recovered = resolver.resolve(file_name, last_path)
                        info["recoverable_path"] = recovered
                    missing_list.append(info)

        # Inspect Stacks storedFiles in Pages
        stacks_issues = self._inspect_stacks_stored_files()

        return {
            "project_path": self.project_path,
            "total_file_references": total_refs,
            "internal_count": internal_count,
            "external_existing": external_existing,
            "external_missing": external_missing,
            "volume_distribution": volume_distribution,
            "missing_references": missing_list,
            "existing_references": existing_list,
            "internal_references": internal_list,
            "stacks_issues": stacks_issues
        }

    def _inspect_stacks_stored_files(self) -> List[Dict[str, Any]]:
        """Finds Stacks storedFiles with hardcoded paths."""
        issues = []
        if not os.path.isdir(self.pages_dir):
            return issues

        for root, _, files in os.walk(self.pages_dir):
            if "Contents.plist" in files and os.path.basename(root) == "Data":
                plist_path = os.path.join(root, "Contents.plist")
                try:
                    with open(plist_path, "rb") as fp:
                        data = plistlib.load(fp)
                    fillings = data.get("SandwichFillings", [{}])
                    sf = fillings[0].get("Dictionary", {}).get("storedFiles", {})
                    for fname, stored_path in sf.items():
                        if isinstance(stored_path, str) and stored_path.startswith("/"):
                            # Check if pointing to the bundle's actual current location
                            expected_path = os.path.abspath(os.path.join(root, fname))
                            if os.path.abspath(stored_path) != expected_path:
                                issues.append({
                                    "plist_path": plist_path,
                                    "fileName": fname,
                                    "current_stored_path": stored_path,
                                    "expected_local_path": expected_path,
                                    "exists_locally": os.path.exists(expected_path)
                                })
                except Exception:
                    pass
        return issues

    def internalize(self, resolver: AssetResolver, dry_run: bool = False) -> Dict[str, Any]:
        """
        Converts all external references to internal bundled project resources
        (storedInsideDocument: True).
        """
        results = {
            "internalized": [],
            "already_internal": [],
            "failed_to_resolve": [],
            "stacks_rebased": 0
        }

        # Subsandwiches list in Resources/Contents.plist
        r_fillings = self.resources_plist_data.get("SandwichFillings", [{}])[0]
        subsandwiches: List[str] = r_fillings.setdefault("Subsandwiches", [])

        for ref_id, ref_dict in list(self.file_refs.items()):
            if ref_dict.get("storedInsideDocument", False):
                results["already_internal"].append(ref_id)
                continue

            last_path = ref_dict.get("lastResolvablePath", "")
            fname = ref_dict.get("fileName") or (os.path.basename(last_path) if last_path else "")

            # Resolve local source file
            source_file = resolver.resolve(fname, last_path)
            if not source_file:
                results["failed_to_resolve"].append({
                    "id": ref_id,
                    "fileName": fname,
                    "lastResolvablePath": last_path
                })
                continue

            actual_filename = os.path.basename(source_file)
            target_res_folder = os.path.join(self.resources_dir, ref_id)
            target_file_path = os.path.join(target_res_folder, actual_filename)
            target_plist_path = os.path.join(target_res_folder, "Contents.plist")

            if not dry_run:
                # 1. Create Resources/<UUID> folder
                os.makedirs(target_res_folder, exist_ok=True)
                # 2. Copy asset file
                shutil.copy2(source_file, target_file_path)
                # 3. Create Resources/<UUID>/Contents.plist
                res_plist = {
                    "Creator": "",
                    "SandwichFillings": [{
                        "Dictionary": {},
                        "Files": [actual_filename],
                        "Subsandwiches": []
                    }],
                    "Type": "Resource"
                }
                with open(target_plist_path, "wb") as fp:
                    plistlib.dump(res_plist, fp)

                # 4. Add to Subsandwiches if not present
                if ref_id not in subsandwiches:
                    subsandwiches.append(ref_id)

                # 5. Update RWFileReferences entry
                ref_dict["storedInsideDocument"] = True
                ref_dict["fileName"] = actual_filename
                ref_dict["identifier"] = ref_id
                ref_dict["lastResolvablePath"] = ""
                # Remove obsolete external keys
                ref_dict.pop("documentScopedBookmark", None)
                ref_dict.pop("fallbackAppScopedBookmark", None)
                ref_dict.pop("volumeName", None)
                ref_dict.pop("volumePath", None)

            results["internalized"].append({
                "id": ref_id,
                "fileName": actual_filename,
                "source": source_file,
                "dest": target_file_path
            })

        # Rebase Stacks internal storedFiles
        rebased = self.rebase_stacks(dry_run=dry_run)
        results["stacks_rebased"] = len(rebased)

        if not dry_run:
            self._save()

        return results

    def remap(self,
              resolver: AssetResolver,
              from_prefix: Optional[str] = None,
              to_prefix: Optional[str] = None,
              dry_run: bool = False,
              refresh_bookmarks: bool = False) -> Dict[str, Any]:
        """
        Remaps external file references to existing files found by resolver
        or via prefix replacement.
        """
        results = {
            "remapped": [],
            "unchanged": [],
            "failed_to_resolve": [],
            "stacks_rebased": 0
        }

        paths_to_bookmark: Dict[str, str] = {}  # ref_id -> new_path

        for ref_id, ref_dict in self.file_refs.items():
            if ref_dict.get("storedInsideDocument", False):
                results["unchanged"].append(ref_id)
                continue

            last_path = ref_dict.get("lastResolvablePath", "")
            fname = ref_dict.get("fileName") or (os.path.basename(last_path) if last_path else "")

            new_path: Optional[str] = None
            if from_prefix and to_prefix and last_path and last_path.startswith(from_prefix):
                candidate = last_path.replace(from_prefix, to_prefix, 1)
                if os.path.exists(candidate):
                    new_path = os.path.abspath(candidate)

            if not new_path:
                new_path = resolver.resolve(fname, last_path)

            if not new_path:
                results["failed_to_resolve"].append({
                    "id": ref_id,
                    "fileName": fname,
                    "lastResolvablePath": last_path
                })
                continue

            vol_name, vol_path = get_volume_info(new_path)
            old_path = last_path

            if not dry_run:
                ref_dict["lastResolvablePath"] = new_path
                ref_dict["volumeName"] = vol_name
                ref_dict["volumePath"] = vol_path
                ref_dict["fileName"] = os.path.basename(new_path)
                paths_to_bookmark[ref_id] = new_path

            results["remapped"].append({
                "id": ref_id,
                "fileName": os.path.basename(new_path),
                "old_path": old_path,
                "new_path": new_path
            })

        if refresh_bookmarks and not dry_run and paths_to_bookmark:
            self._batch_generate_bookmarks(paths_to_bookmark)

        rebased = self.rebase_stacks(dry_run=dry_run)
        results["stacks_rebased"] = len(rebased)

        if not dry_run:
            self._save()

        return results

    def _batch_generate_bookmarks(self, ref_to_path: Dict[str, str]):
        """Generates macOS security-scoped bookmarks using Swift."""
        swift_code = f"""
import Foundation

let docURL = URL(fileURLWithPath: "{self.project_path}")
let jsonInput = try FileHandle.standardInput.readToEnd()!
let paths = try JSONSerialization.jsonObject(with: jsonInput) as! [String: String]
var results: [String: [String: String]] = [:]

for (refID, p) in paths {{
    let u = URL(fileURLWithPath: p)
    var entry: [String: String] = [:]
    if let docBM = try? u.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: docURL) {{
        entry["docBM"] = docBM.base64EncodedString()
    }}
    if let appBM = try? u.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {{
        entry["appBM"] = appBM.base64EncodedString()
    }}
    results[refID] = entry
}}
let outData = try JSONSerialization.data(withJSONObject: results)
FileHandle.standardOutput.write(outData)
"""
        try:
            proc = subprocess.run(
                ["swift", "-e", swift_code],
                input=json.dumps(ref_to_path).encode("utf-8"),
                capture_output=True,
                check=True
            )
            data = json.loads(proc.stdout.decode("utf-8"))
            for ref_id, bms in data.items():
                if ref_id in self.file_refs:
                    if "docBM" in bms:
                        import base64
                        self.file_refs[ref_id]["documentScopedBookmark"] = base64.b64decode(bms["docBM"])
                    if "appBM" in bms:
                        import base64
                        self.file_refs[ref_id]["fallbackAppScopedBookmark"] = base64.b64decode(bms["appBM"])
        except Exception as e:
            print(f"Warning: Bookmark generation failed: {e}", file=sys.stderr)

    def rebase_stacks(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        """
        Updates Stacks page storedFiles paths so they point to the actual current
        location of the project bundle on disk.
        """
        rebased = []
        if not os.path.isdir(self.pages_dir):
            return rebased

        for root, _, files in os.walk(self.pages_dir):
            if "Contents.plist" in files and os.path.basename(root) == "Data":
                plist_path = os.path.join(root, "Contents.plist")
                try:
                    with open(plist_path, "rb") as fp:
                        data = plistlib.load(fp)
                    fillings = data.get("SandwichFillings", [{}])
                    dic = fillings[0].get("Dictionary", {})
                    sf = dic.get("storedFiles", {})
                    modified = False
                    for fname, stored_path in list(sf.items()):
                        expected_local = os.path.abspath(os.path.join(root, fname))
                        if isinstance(stored_path, str) and stored_path != expected_local:
                            rebased.append({
                                "plist": plist_path,
                                "file": fname,
                                "old": stored_path,
                                "new": expected_local
                            })
                            if not dry_run:
                                sf[fname] = expected_local
                                modified = True
                    if modified and not dry_run:
                        with open(plist_path, "wb") as fp:
                            plistlib.dump(data, fp)
                except Exception as e:
                    print(f"Warning: Failed to update Stacks plist {plist_path}: {e}", file=sys.stderr)
        return rebased

    def create_backup(self) -> str:
        """Creates a timestamped backup directory of the project bundle."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{self.project_path}_backup_{timestamp}"
        shutil.copytree(self.project_path, backup_path, symlinks=True)
        return backup_path

    def _save(self):
        """Saves Contents.plist and Resources/Contents.plist."""
        with open(self.contents_path, "wb") as f:
            plistlib.dump(self.contents_data, f)
        os.makedirs(self.resources_dir, exist_ok=True)
        with open(self.resources_plist_path, "wb") as f:
            plistlib.dump(self.resources_plist_data, f)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RapidWeaver Path Generalization & Conversion Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Inspect project references:
  python3 rapidweaver_path_manager.py inspect wnbb.rwc

  # Search for missing assets:
  python3 rapidweaver_path_manager.py find-assets wnbb.rwc --search-dirs ./Pictures "/Volumes/McFly"

  # Internalize all references (make project 100% self-contained and portable):
  python3 rapidweaver_path_manager.py internalize wnbb.rwc --search-dirs ./Pictures "/Volumes/McFly"

  # Remap external paths to local directory:
  python3 rapidweaver_path_manager.py remap wnbb.rwc --search-dirs ./Pictures --refresh-bookmarks

  # Rebase internal Stacks paths to the current bundle location:
  python3 rapidweaver_path_manager.py rebase-stacks wnbb.rwc
"""
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect and audit project file references")
    p_inspect.add_argument("project", help="Path to .rwc / .rw8 project bundle")
    p_inspect.add_argument("--search-dirs", nargs="+", default=None, help="Directories to search for missing asset files (defaults to smart scan of project folder and parents)")
    p_inspect.add_argument("--no-auto-search", action="store_true", help="Disable automatic scanning of project and parent directories for missing files")
    p_inspect.add_argument("--json", action="store_true", help="Output report as JSON")

    # find-assets
    p_find = subparsers.add_parser("find-assets", help="Find matching local asset files for project references")
    p_find.add_argument("project", help="Path to .rwc / .rw8 project bundle")
    p_find.add_argument("--search-dirs", nargs="+", default=None, help="Directories to search for asset files (defaults to smart scan of project folder and parents)")
    p_find.add_argument("--export-map", help="Export path mapping as JSON to specified file path")

    # internalize
    p_internal = subparsers.add_parser("internalize", help="Bundle external files into project resources (100% portable)")
    p_internal.add_argument("project", help="Path to .rwc / .rw8 project bundle")
    p_internal.add_argument("--search-dirs", nargs="+", default=None, help="Directories to search for assets (defaults to smart scan of project folder and parents)")
    p_internal.add_argument("--dry-run", action="store_true", help="Simulate without modifying files")
    p_internal.add_argument("--no-backup", action="store_true", help="Skip creating backup")
    p_internal.add_argument("--export-map", help="Export path mapping as JSON to specified file path")

    # remap
    p_remap = subparsers.add_parser("remap", help="Remap external file reference paths")
    p_remap.add_argument("project", help="Path to .rwc / .rw8 project bundle")
    p_remap.add_argument("--search-dirs", nargs="+", default=[], help="Directories to search for assets")
    p_remap.add_argument("--from-prefix", help="Path prefix to replace")
    p_remap.add_argument("--to-prefix", help="Replacement path prefix")
    p_remap.add_argument("--refresh-bookmarks", action="store_true", help="Regenerate macOS security bookmarks via Swift")
    p_remap.add_argument("--dry-run", action="store_true", help="Simulate without modifying files")
    p_remap.add_argument("--no-backup", action="store_true", help="Skip creating backup")
    p_remap.add_argument("--export-map", help="Export path mapping as JSON to specified file path")

    # rebase-stacks
    p_stacks = subparsers.add_parser("rebase-stacks", help="Rebase Stacks page storedFiles to current bundle path")
    p_stacks.add_argument("project", help="Path to .rwc / .rw8 project bundle")
    p_stacks.add_argument("--dry-run", action="store_true", help="Simulate without modifying files")
    p_stacks.add_argument("--no-backup", action="store_true", help="Skip creating backup")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    project = RapidWeaverProject(args.project)

    if args.command == "inspect":
        search_dirs = args.search_dirs
        if search_dirs is None and not args.no_auto_search:
            search_dirs = get_default_search_dirs(args.project)
        resolver = AssetResolver(search_dirs) if search_dirs else None
        report = project.inspect(resolver=resolver)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print("==================================================")
            print(f" RapidWeaver Project Audit: {os.path.basename(project.project_path)}")
            print("==================================================")
            print(f"Total file references: {report['total_file_references']}")
            print(f"  • Internally stored (portable): {report['internal_count']}")
            print(f"  • External & found on disk:     {report['external_existing']}")
            print(f"  • External & MISSING/BROKEN:    {report['external_missing']}")
            print("\nVolume Distribution:")
            for vol, cnt in sorted(report["volume_distribution"].items(), key=lambda x: -x[1]):
                print(f"  • {vol}: {cnt} files")

            if report["external_missing"]:
                recoverable_count = sum(1 for m in report["missing_references"] if m.get("recoverable_path"))
                print(f"\nMissing Files ({report['external_missing']}):")
                for item in report["missing_references"][:25]:
                    rec_path = item.get("recoverable_path")
                    if rec_path:
                        print(f"  [!] {item['fileName']}")
                        print(f"      Old path:    {item['lastResolvablePath']}")
                        print(f"      ✓ FOUND AT:  {rec_path}")
                    else:
                        print(f"  [!] {item['fileName']}")
                        print(f"      Old path:    {item['lastResolvablePath']}")
                        print(f"      ✗ NOT FOUND in search directories")
                if len(report["missing_references"]) > 25:
                    print(f"  ... and {len(report['missing_references']) - 25} more.")

                print(f"\nAsset Recovery Status: {recoverable_count} of {report['external_missing']} missing files can be recovered.")
                if recoverable_count > 0:
                    print("\nSuggested Action:")
                    print(f"  To automatically bundle all assets into the project (100% portable across Macs), run:")
                    print(f"    ./rapidweaver_path_manager.py internalize \"{args.project}\"")
                    print(f"  Or to keep them external and remap the paths, run:")
                    print(f"    ./rapidweaver_path_manager.py remap \"{args.project}\"")
            stacks_issues = report.get("stacks_issues", [])
            print(f"\nStacks storedFiles Issues: {len(stacks_issues)}")
            for issue in stacks_issues[:5]:
                print(f"  [!] In {os.path.relpath(issue['plist_path'], project.project_path)}:")
                print(f"      Stored:   {issue['current_stored_path']}")
                print(f"      Expected: {issue['expected_local_path']}")
            if len(stacks_issues) > 5:
                print(f"  ... and {len(stacks_issues) - 5} more.")

    elif args.command == "find-assets":
        sdirs = args.search_dirs if args.search_dirs else get_default_search_dirs(args.project)
        resolver = AssetResolver(sdirs)
        report = project.inspect()
        print(f"Indexing complete. Searching for {len(report['missing_references'])} missing references...")
        found_count = 0
        missing_count = 0
        mapping_records = []
        for item in report["missing_references"]:
            match = resolver.resolve(item["fileName"], item["lastResolvablePath"])
            record = {
                "id": item["id"],
                "fileName": item["fileName"],
                "original_path": item["lastResolvablePath"],
                "found_path": match,
                "status": "found" if match else "missing"
            }
            mapping_records.append(record)
            if match:
                found_count += 1
                print(f"  [FOUND] {item['fileName']}")
                print(f"          → {match}")
            else:
                missing_count += 1
                print(f"  [NOT FOUND] {item['fileName']}")
                print(f"              last path: {item['lastResolvablePath']}")
        print("--------------------------------------------------")
        print(f"Found: {found_count} / {len(report['missing_references'])} | Still missing: {missing_count}")
        if args.export_map:
            with open(args.export_map, "w") as fp:
                json.dump(mapping_records, fp, indent=2)
            print(f"Mapping exported to: {args.export_map}")

    elif args.command == "internalize":
        sdirs = args.search_dirs if args.search_dirs else get_default_search_dirs(args.project)
        resolver = AssetResolver(sdirs)
        if not args.dry_run and not args.no_backup:
            backup_dir = project.create_backup()
            print(f"Created project backup at: {backup_dir}")

        print(f"Internalizing external references in {os.path.basename(project.project_path)}...")
        res = project.internalize(resolver, dry_run=args.dry_run)
        print("--------------------------------------------------")
        print(f"Internalized new files: {len(res['internalized'])}")
        print(f"Already internal:       {len(res['already_internal'])}")
        print(f"Stacks files rebased:   {res['stacks_rebased']}")
        print(f"Failed to resolve:      {len(res['failed_to_resolve'])}")
        if res["failed_to_resolve"]:
            print("\nCould not resolve the following files:")
            for f in res["failed_to_resolve"]:
                print(f"  [!] {f['fileName']} (last path: {f['lastResolvablePath']})")
        if args.export_map:
            with open(args.export_map, "w") as fp:
                json.dump(res, fp, indent=2)
            print(f"Mapping exported to: {args.export_map}")
        if args.dry_run:
            print("\n[DRY RUN] No files were modified.")
        else:
            print("\nSUCCESS: Project is now internalized and portable!")

    elif args.command == "remap":
        sdirs = args.search_dirs if args.search_dirs else get_default_search_dirs(args.project)
        resolver = AssetResolver(sdirs)
        if not args.dry_run and not args.no_backup:
            backup_dir = project.create_backup()
            print(f"Created project backup at: {backup_dir}")

        print(f"Remapping external references in {os.path.basename(project.project_path)}...")
        res = project.remap(
            resolver=resolver,
            from_prefix=args.from_prefix,
            to_prefix=args.to_prefix,
            dry_run=args.dry_run,
            refresh_bookmarks=args.refresh_bookmarks
        )
        print("--------------------------------------------------")
        print(f"Remapped files:       {len(res['remapped'])}")
        print(f"Unchanged/Internal:   {len(res['unchanged'])}")
        print(f"Stacks files rebased: {res['stacks_rebased']}")
        print(f"Failed to resolve:    {len(res['failed_to_resolve'])}")
        if res["failed_to_resolve"]:
            print("\nCould not resolve the following files:")
            for f in res["failed_to_resolve"]:
                print(f"  [!] {f['fileName']} (last path: {f['lastResolvablePath']})")
        if args.export_map:
            with open(args.export_map, "w") as fp:
                json.dump(res, fp, indent=2)
            print(f"Mapping exported to: {args.export_map}")
        if args.dry_run:
            print("\n[DRY RUN] No files were modified.")
        else:
            print("\nSUCCESS: External paths remapped!")

    elif args.command == "rebase-stacks":
        if not args.dry_run and not args.no_backup:
            backup_dir = project.create_backup()
            print(f"Created project backup at: {backup_dir}")

        rebased = project.rebase_stacks(dry_run=args.dry_run)
        print(f"Rebased {len(rebased)} Stacks storedFiles entries.")
        if rebased:
            for item in rebased:
                print(f"  • {item['file']} -> {item['new']}")
        if args.dry_run:
            print("\n[DRY RUN] No files were modified.")
        else:
            print("\nSUCCESS: Stacks paths rebased!")


if __name__ == "__main__":
    main()
