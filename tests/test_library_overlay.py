from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import vc_gallery_library as L


def _touch(p: Path, data: bytes = b"x", mtime: float | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


class LibraryOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        L.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # Two units with different names — nothing episode-specific.
        for unit in ("Spot A", "Launch film"):
            for stage in ("00_Admin", "01_Ingest", "02_Assets", "04_Deliverables"):
                (self.root / unit / stage).mkdir(parents=True)
        _touch(self.root / "Spot A/04_Deliverables/Spot A v1.mp4", mtime=1000)
        _touch(self.root / "Spot A/04_Deliverables/Spot A v2.mp4", mtime=2000)
        _touch(self.root / "Spot A/02_Assets/frame.png")
        _touch(self.root / "Launch film/01_Ingest/Client/cut_010226.mp4", mtime=5000)
        _touch(self.root / "Launch film/01_Ingest/Client/cut_121525.mp4", mtime=9000)
        (self.root / "Loose refs").mkdir()
        _touch(self.root / "Spot A/Adobe Premiere Pro Auto-Save/junk.prproj")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_units_are_discovered_by_structure_not_name(self) -> None:
        o = L.library_overview(str(self.root))
        self.assertEqual(sorted(u["name"] for u in o["units"]), ["Launch film", "Spot A"])
        self.assertEqual([x["name"] for x in o["other_folders"]], ["Loose refs"])
        spot = next(u for u in o["units"] if u["name"] == "Spot A")
        self.assertEqual([s["prefix"] for s in spot["stages"]], [0, 1, 2, 4])

    def test_default_latest_cut_is_newest_video_in_deliverables(self) -> None:
        o = L.library_overview(str(self.root))
        spot = next(u for u in o["units"] if u["name"] == "Spot A")
        self.assertEqual(spot["latest_cut"]["name"], "Spot A v2.mp4")
        launch = next(u for u in o["units"] if u["name"] == "Launch film")
        self.assertIsNone(launch["latest_cut"])  # nothing in its deliverables

    def test_config_folders_and_name_dates_override_mtime(self) -> None:
        (self.root / "LIBRARY.json").write_text(json.dumps({
            "full_cut": {"folders": ["04_Deliverables", "01_Ingest"], "date_in_name": "MMDDYY"}}))
        # Make the older-by-name file the newer-by-mtime file: name date must win.
        os.utime(self.root / "Launch film/01_Ingest/Client/cut_121525.mp4", (1, 1))
        o = L.library_overview(str(self.root))
        launch = next(u for u in o["units"] if u["name"] == "Launch film")
        self.assertEqual(launch["latest_cut"]["name"], "cut_010226.mp4")
        self.assertEqual(launch["latest_cut"]["name_date"], "2026-01-02")

    def test_editor_clutter_is_skipped(self) -> None:
        f = L.library_folder(str(self.root), "Spot A")
        names = [x["name"] for g in f["groups"] for x in g["files"]]
        self.assertNotIn("junk.prproj", names)

    def test_paths_cannot_escape_root(self) -> None:
        with self.assertRaises(PermissionError):
            L.library_folder(str(self.root), "../")
        with self.assertRaises(PermissionError):
            L.file_for_serving(str(self.root), "../../etc/hosts")

    def test_overlay_never_writes_into_the_tree(self) -> None:
        before = sorted(str(p) for p in self.root.rglob("*"))
        L.library_overview(str(self.root))
        L.library_folder(str(self.root), "Spot A")
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_ambiguous_dates_are_not_guessed_by_default(self) -> None:
        self.assertIsNone(L.date_from_name("BTW_Episode 3_050726_LP.mp4", None))
        self.assertEqual(str(L.date_from_name("x_2026-09-24.mp4", None)), "2026-09-24")


if __name__ == "__main__":
    unittest.main()
