"""Isolated behavioral checks; no network, live repository writes or licensed fixtures."""
import argparse
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

spec = importlib.util.spec_from_file_location("intake", Path(__file__).resolve().parents[1] / "scripts/community_memes.py")
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)
REAL_PUBLIC_REPO = intake.public_repo


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(intake, "ROOT", self.root).start()
        self.repo = "fixture/site"
        self.head = "a" * 40
        self.proof = b"This website was built using https://github.com/9federal/meme-sites ."
        self.grant = b"I created all pixels in public/memes/test.png and license this image under CC0-1.0."
        self.payload = self.png("red")
        self.blob = self.git_blob(self.payload)
        self.project = {"repo": self.repo, "evidence_commit": self.head,
                        "evidence_path": "README.md", "usage_note": "Reviewed website and explicit skill credit",
                        "image_roots": ["public/memes"]}
        self.candidate = {"id": "candidate", "repo": self.repo, "commit": self.head,
                          "path": "public/memes/test.png", "blob": self.blob,
                          "url": intake.source_url(self.repo, self.head, "public/memes/test.png"),
                          "status": "pending", "rights": None}
        intake.save("community/sources.json", {"origin": intake.ORIGIN, "projects": [self.project]})
        intake.save("community/candidates.json", [self.candidate])
        intake.save("assets/memes/catalog.json", [])
        patch.object(intake, "public_repo", return_value={"default_branch": "main"}).start()
        patch.object(intake, "file_at", side_effect=self.file_at).start()
        self.args = argparse.Namespace(id="candidate", rights_commit=self.head,
                                      rights_path="IMAGE-RIGHTS.md", rights_quote=self.grant.decode(),
                                      rights_note="Original fixture pixels; all reuse granted", credit="Fixture; CC0-1.0",
                                      license="CC0-1.0")

    @staticmethod
    def png(color, metadata=None):
        data = io.BytesIO()
        Image.new("RGB", (4, 3), color).save(data, "PNG", pnginfo=metadata)
        return data.getvalue()

    @staticmethod
    def git_blob(data):
        return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

    def file_at(self, repo, revision, path):
        if path == "README.md":
            return self.proof, self.git_blob(self.proof)
        if path == "IMAGE-RIGHTS.md":
            return self.grant, self.git_blob(self.grant)
        return self.payload, self.blob

    def test_import_records_provenance_and_strips_active_trailer(self):
        self.payload += b"<script>untrusted()</script>"
        self.blob = self.git_blob(self.payload)
        self.candidate["blob"] = self.blob
        intake.save("community/candidates.json", [self.candidate])
        result = intake.import_image(self.args)
        self.assertEqual(result["status"], "imported")
        self.assertNotIn(b"<script>", (self.root / result["file"]).read_bytes())
        entry = intake.read("assets/memes/catalog.json")[0]
        self.assertEqual(entry["source_blob"], self.blob)
        self.assertEqual(entry["rights"]["evidence_quote"], self.grant.decode())

    def test_uncertain_rights_remain_link_without_image(self):
        self.args.rights_quote = "A grant that does not exist in the public source"
        with self.assertRaises(ValueError):
            intake.import_image(self.args)
        self.assertEqual(intake.read("community/candidates.json")[0]["status"], "pending")
        self.assertEqual(list((self.root / "assets/memes").glob("*.png")), [])

    def test_reencoded_output_strips_profile_and_exif_metadata(self):
        original = Image.new("RGB", (4, 3), "red")
        exif = Image.Exif()
        exif[0x0131] = "Private fixture metadata"
        source = io.BytesIO()
        original.save(source, "PNG", icc_profile=b"Fixture color profile", exif=exif)
        encoded, _ = intake.raster(source.getvalue())
        with Image.open(io.BytesIO(encoded)) as clean:
            self.assertNotIn("icc_profile", clean.info)
            self.assertNotIn("exif", clean.info)

    def test_same_pixels_with_different_metadata_do_not_duplicate_manual_asset(self):
        from PIL.PngImagePlugin import PngInfo
        metadata = PngInfo()
        metadata.add_text("note", "Manually curated")
        manual = self.root / "assets/manual.png"
        original = self.png("red", metadata)
        manual.write_bytes(original)
        result = intake.import_image(self.args)
        self.assertEqual(result, {"status": "duplicate", "file": "assets/manual.png"})
        self.assertEqual(manual.read_bytes(), original)
        self.assertEqual(intake.read("assets/memes/catalog.json"), [])

    def test_repeated_import_is_idempotent_and_keeps_manual_catalog_fields(self):
        result = intake.import_image(self.args)
        catalog = intake.read("assets/memes/catalog.json")
        catalog[0]["manual_caption"] = "Keep this caption"
        intake.save("assets/memes/catalog.json", catalog)
        intake.import_image(self.args)
        self.assertEqual(len(list((self.root / "assets/memes").glob("*.png"))), 1)
        self.assertEqual(intake.read("assets/memes/catalog.json")[0]["manual_caption"], "Keep this caption")
        self.assertTrue((self.root / result["file"]).exists())

    def test_manual_modified_image_is_not_mistaken_for_old_pixels(self):
        result = intake.import_image(self.args)
        target = self.root / result["file"]
        edited = self.png("blue")
        target.write_bytes(edited)
        with self.assertRaises(FileExistsError):
            intake.import_image(self.args)
        self.assertEqual(target.read_bytes(), edited)

    def test_svg_disguised_as_png_is_not_imported(self):
        self.payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
        self.blob = self.git_blob(self.payload)
        self.candidate["blob"] = self.blob
        intake.save("community/candidates.json", [self.candidate])
        with self.assertRaises(Exception):
            intake.import_image(self.args)
        self.assertEqual(list((self.root / "assets/memes").glob("*.png")), [])

    def test_git_blob_mismatch_blocks_import(self):
        self.payload = self.png("blue")
        with self.assertRaises(ValueError):
            intake.import_image(self.args)

    def test_animation_is_left_for_review(self):
        data = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(data, "GIF", save_all=True,
                append_images=[Image.new("RGB", (2, 2), "blue")])
        with self.assertRaises(ValueError):
            intake.raster(data.getvalue())

    def test_removed_or_unrelated_skill_attribution_blocks_scan(self):
        self.proof = b"A different meme-sites skill, https://github.com/elsewhere/meme-sites"
        with patch.object(intake, "api", return_value={"sha": self.head}):
            result = intake.scan(None)
        self.assertEqual(len(result["blockers"]), 1)
        self.assertEqual(result["projects_checked"], 0)

    def test_scan_adds_links_only_skips_active_files_and_preserves_manual_decisions(self):
        self.candidate.update(status="rights-unclear", note="Keep reviewed decision")
        intake.save("community/candidates.json", [self.candidate])
        tree = [{"path": "public/memes/new.png", "type": "blob", "mode": "100644", "size": 80, "sha": "b" * 40},
                {"path": "public/memes/active.svg", "type": "blob", "mode": "100644", "size": 80, "sha": "c" * 40},
                {"path": "public/memes/link.png", "type": "blob", "mode": "120000", "size": 80, "sha": "d" * 40},
                {"path": "other/private.png", "type": "blob", "mode": "100644", "size": 80, "sha": "e" * 40}]
        with patch.object(intake, "api", side_effect=lambda path: {"tree": tree} if "/git/trees/" in path else {"sha": self.head}):
            self.assertEqual(intake.scan(None)["new_links"], 1)
            self.assertEqual(intake.scan(None)["new_links"], 0)
        queue = intake.read("community/candidates.json")
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["note"], "Keep reviewed decision")
        self.assertIsNone(queue[1]["rights"])
        self.assertEqual(list((self.root / "assets/memes").glob("*.png")), [])

    def test_private_repository_is_rejected_even_if_metadata_available(self):
        with patch.object(intake, "api", return_value={"private": True, "visibility": "private", "full_name": self.repo}):
            with self.assertRaises(ValueError):
                REAL_PUBLIC_REPO(self.repo)

    def test_local_escape_and_symlink_are_rejected(self):
        with self.assertRaises(ValueError):
            intake.save("../outside.json", {})
        (self.root / "community/candidates.json").unlink()
        (self.root / "community/candidates.json").symlink_to(self.root / "assets/memes/catalog.json")
        with self.assertRaises(ValueError):
            intake.save("community/candidates.json", [])


if __name__ == "__main__":
    unittest.main()
