import unittest
import os
import shutil
import tempfile
import plistlib
from rapidweaver_path_manager import (
    normalize_filename,
    get_volume_info,
    AssetResolver,
    RapidWeaverProject
)


class TestRapidWeaverPathManager(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="rw_test_")
        self.assets_dir = os.path.join(self.test_dir, "assets")
        os.makedirs(self.assets_dir, exist_ok=True)

        # Create dummy assets
        self.sample_img1 = os.path.join(self.assets_dir, "Band Photo.jpg")
        with open(self.sample_img1, "wb") as f:
            f.write(b"IMG_DATA_1")

        self.sample_img2 = os.path.join(self.assets_dir, "poster-event-2024.jpeg")
        with open(self.sample_img2, "wb") as f:
            f.write(b"IMG_DATA_2")

        # Create mock .rwc project bundle
        self.project_path = os.path.join(self.test_dir, "TestSite.rwc")
        os.makedirs(self.project_path, exist_ok=True)
        self.resources_path = os.path.join(self.project_path, "Resources")
        os.makedirs(self.resources_path, exist_ok=True)

        # Setup mock Contents.plist
        self.contents_plist = os.path.join(self.project_path, "Contents.plist")
        self.mock_data = {
            "Creator": "RapidWeaver",
            "SandwichFillings": [{
                "Dictionary": {
                    "Minimum Application Version": "8.1",
                    "RWFileReferences": {
                        "UUID-INTERNAL-1": {
                            "fileName": "favicon.ico",
                            "identifier": "UUID-INTERNAL-1",
                            "lastResolvablePath": "",
                            "storedInsideDocument": True
                        },
                        "UUID-EXTERNAL-1": {
                            "fileName": "Band Photo.jpg",
                            "identifier": "UUID-EXTERNAL-1",
                            "lastResolvablePath": "/Volumes/OldDisk/Music/Band Photo.jpg",
                            "storedInsideDocument": False,
                            "volumeName": "OldDisk",
                            "volumePath": "/Volumes/OldDisk",
                            "documentScopedBookmark": b"OLD_BOOKMARK"
                        },
                        "UUID-EXTERNAL-2": {
                            "fileName": "poster event 2024.jpg",
                            "identifier": "UUID-EXTERNAL-2",
                            "lastResolvablePath": "/Volumes/OldDisk/Music/poster event 2024.jpg",
                            "storedInsideDocument": False,
                            "volumeName": "OldDisk",
                            "volumePath": "/Volumes/OldDisk"
                        },
                        "UUID-MISSING-1": {
                            "fileName": "nonexistent.png",
                            "identifier": "UUID-MISSING-1",
                            "lastResolvablePath": "/Volumes/OldDisk/Music/nonexistent.png",
                            "storedInsideDocument": False
                        }
                    }
                }
            }]
        }
        with open(self.contents_plist, "wb") as f:
            plistlib.dump(self.mock_data, f)

        # Setup mock Resources/Contents.plist
        self.res_contents_plist = os.path.join(self.resources_path, "Contents.plist")
        self.mock_res_data = {
            "Creator": "",
            "SandwichFillings": [{
                "Dictionary": {},
                "Files": [],
                "Subsandwiches": ["UUID-INTERNAL-1"]
            }],
            "Type": "Resources"
        }
        with open(self.res_contents_plist, "wb") as f:
            plistlib.dump(self.mock_res_data, f)

        # Setup mock internal file for UUID-INTERNAL-1
        internal_folder = os.path.join(self.resources_path, "UUID-INTERNAL-1")
        os.makedirs(internal_folder, exist_ok=True)
        with open(os.path.join(internal_folder, "favicon.ico"), "wb") as f:
            f.write(b"FAVICON_DATA")

        # Setup mock Pages Stacks storedFiles
        self.page_data_dir = os.path.join(self.project_path, "Pages", "01-About", "Data")
        os.makedirs(self.page_data_dir, exist_ok=True)
        self.page_img = os.path.join(self.page_data_dir, "photo.jpg")
        with open(self.page_img, "wb") as f:
            f.write(b"STACKS_IMG")
        self.page_plist = os.path.join(self.page_data_dir, "Contents.plist")
        page_plist_data = {
            "SandwichFillings": [{
                "Dictionary": {
                    "storedFiles": {
                        "photo.jpg": "/Volumes/OldDisk/Project.rwc/Pages/01-About/Data/photo.jpg"
                    }
                }
            }]
        }
        with open(self.page_plist, "wb") as f:
            plistlib.dump(page_plist_data, f)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_normalize_filename(self):
        self.assertEqual(normalize_filename("Band - Photo_01.jpeg"), "band photo 01.jpg")
        self.assertEqual(normalize_filename("DSC_2652sml.jpg"), "dsc 2652sml.jpg")
        self.assertEqual(normalize_filename("Doc.TIFF"), "doc.tif")

    def test_get_volume_info(self):
        name, path = get_volume_info("/Volumes/MyExternalDrive/Photos/pic.jpg")
        self.assertEqual(name, "MyExternalDrive")
        self.assertEqual(path, "/Volumes/MyExternalDrive")

        name, path = get_volume_info("/Users/john/Pictures/pic.jpg")
        self.assertEqual(name, "Macintosh HD")
        self.assertEqual(path, "/")

    def test_asset_resolver(self):
        resolver = AssetResolver([self.assets_dir])
        # Exact match
        res1 = resolver.resolve("Band Photo.jpg")
        self.assertEqual(res1, os.path.abspath(self.sample_img1))

        # Normalized match (spaces in query, dashes in filename)
        res2 = resolver.resolve("poster event 2024.jpg")
        self.assertEqual(res2, os.path.abspath(self.sample_img2))

        # Non-existent
        res3 = resolver.resolve("does_not_exist.png")
        self.assertIsNone(res3)

    def test_project_inspect(self):
        project = RapidWeaverProject(self.project_path)
        report = project.inspect()

        self.assertEqual(report["total_file_references"], 4)
        self.assertEqual(report["internal_count"], 1)
        self.assertEqual(report["external_missing"], 3)  # None of the old paths exist on disk
        self.assertEqual(len(report["stacks_issues"]), 1)
        self.assertEqual(report["stacks_issues"][0]["fileName"], "photo.jpg")

    def test_project_internalize(self):
        project = RapidWeaverProject(self.project_path)
        resolver = AssetResolver([self.assets_dir])

        # Test Dry Run
        dry_res = project.internalize(resolver, dry_run=True)
        self.assertEqual(len(dry_res["internalized"]), 2)  # UUID-EXTERNAL-1 and UUID-EXTERNAL-2
        self.assertEqual(len(dry_res["failed_to_resolve"]), 1)  # UUID-MISSING-1
        self.assertFalse(os.path.exists(os.path.join(self.resources_path, "UUID-EXTERNAL-1")))

        # Test Actual Run
        res = project.internalize(resolver, dry_run=False)
        self.assertEqual(len(res["internalized"]), 2)
        self.assertEqual(len(res["failed_to_resolve"]), 1)

        # Check that files were copied to Resources/<UUID>/<file>
        target_folder_1 = os.path.join(self.resources_path, "UUID-EXTERNAL-1")
        self.assertTrue(os.path.isdir(target_folder_1))
        self.assertTrue(os.path.isfile(os.path.join(target_folder_1, "Band Photo.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(target_folder_1, "Contents.plist")))

        # Check Resources/Contents.plist updated Subsandwiches
        with open(self.res_contents_plist, "rb") as f:
            res_plist = plistlib.load(f)
        subsandwiches = res_plist["SandwichFillings"][0]["Subsandwiches"]
        self.assertIn("UUID-INTERNAL-1", subsandwiches)
        self.assertIn("UUID-EXTERNAL-1", subsandwiches)
        self.assertIn("UUID-EXTERNAL-2", subsandwiches)

        # Check Contents.plist RWFileReferences
        with open(self.contents_plist, "rb") as f:
            proj_plist = plistlib.load(f)
        refs = proj_plist["SandwichFillings"][0]["Dictionary"]["RWFileReferences"]
        ref1 = refs["UUID-EXTERNAL-1"]
        self.assertTrue(ref1["storedInsideDocument"])
        self.assertEqual(ref1["fileName"], "Band Photo.jpg")
        self.assertEqual(ref1["lastResolvablePath"], "")
        self.assertNotIn("documentScopedBookmark", ref1)
        self.assertNotIn("volumeName", ref1)

        # Check Stacks storedFiles rebased
        with open(self.page_plist, "rb") as f:
            pg_plist = plistlib.load(f)
        sf = pg_plist["SandwichFillings"][0]["Dictionary"]["storedFiles"]
        self.assertEqual(sf["photo.jpg"], os.path.abspath(self.page_img))

    def test_asset_resolver_priority(self):
        # Create secondary asset dir with duplicate filename
        sec_dir = os.path.join(self.test_dir, "secondary_assets")
        os.makedirs(sec_dir, exist_ok=True)
        dup_file = os.path.join(sec_dir, "Band Photo.jpg")
        with open(dup_file, "wb") as f:
            f.write(b"SECONDARY_DATA")

        # Prioritize primary assets_dir over secondary
        resolver = AssetResolver([self.assets_dir, sec_dir])
        res = resolver.resolve("Band Photo.jpg")
        self.assertEqual(res, os.path.abspath(self.sample_img1))

        # Prioritize secondary over primary
        resolver_sec = AssetResolver([sec_dir, self.assets_dir])
        res_sec = resolver_sec.resolve("Band Photo.jpg")
        self.assertEqual(res_sec, os.path.abspath(dup_file))

    def test_project_remap(self):
        project = RapidWeaverProject(self.project_path)
        resolver = AssetResolver([self.assets_dir])

        res = project.remap(resolver, dry_run=False)
        self.assertEqual(len(res["remapped"]), 2)
        self.assertEqual(len(res["failed_to_resolve"]), 1)

        with open(self.contents_plist, "rb") as f:
            proj_plist = plistlib.load(f)
        refs = proj_plist["SandwichFillings"][0]["Dictionary"]["RWFileReferences"]
        ref1 = refs["UUID-EXTERNAL-1"]
        self.assertEqual(ref1["lastResolvablePath"], os.path.abspath(self.sample_img1))
        self.assertEqual(ref1["fileName"], "Band Photo.jpg")


if __name__ == "__main__":
    unittest.main()
