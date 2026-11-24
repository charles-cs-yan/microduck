"""Stdlib-only checks for the voice tooling: model registry, fetch_model, keyword files."""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

VOICE = Path(__file__).resolve().parents[1] / "voice"
sys.path.insert(0, str(VOICE))

import fetch_model  # noqa: E402
import models  # noqa: E402


def make_archive(path: Path, files: dict) -> None:
    with tarfile.open(path, "w:bz2") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(f"model-dir/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        evil = tarfile.TarInfo("../../escape.txt")  # must never be extracted
        evil.size = 4
        bundle.addfile(evil, io.BytesIO(b"evil"))


class FetchModelTest(unittest.TestCase):
    def test_verified_archive_extracts_only_listed_files(self):
        spec = models.MODELS["gigaspeech"]
        payload = {name: name.encode() for name in spec["files"].values()}
        payload["README.md"] = b"not wanted"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "m.tar.bz2"
            make_archive(archive, payload)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            original = spec["sha256"]
            spec["sha256"] = digest
            try:
                out = Path(tmp) / "out"
                self.assertEqual(fetch_model.main(["--archive", str(archive), "--dir", str(out)]), 0)
            finally:
                spec["sha256"] = original
            self.assertEqual(sorted(p.name for p in out.iterdir()), sorted([*spec["files"].values(), "manifest.json"]))
            self.assertFalse((Path(tmp).parent / "escape.txt").exists())
            self.assertEqual(fetch_model.verify_installed(out), "")
            # a truncated file (interrupted copy, full disk…) is caught by the size check
            victim = out / spec["files"]["encoder"]
            victim.write_bytes(victim.read_bytes()[:1])
            self.assertIn("大小不符", fetch_model.verify_installed(out))
            self.assertFalse(Path(str(out) + ".partial").exists())

    def test_failed_extraction_leaves_nothing_that_looks_installed(self):
        spec = models.MODELS["gigaspeech"]
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "m.tar.bz2"
            make_archive(archive, {"tokens.txt": b"only one file"})
            out = Path(tmp) / "out"
            with self.assertRaises(ValueError):
                fetch_model.extract(archive, list(spec["files"].values()), out)
            self.assertFalse(out.exists())
            self.assertFalse(Path(str(out) + ".partial").exists())
            self.assertIn("沒有 manifest.json", fetch_model.verify_installed(out))

    def test_reinstall_replaces_previous_install(self):
        spec = models.MODELS["gigaspeech"]
        names = list(spec["files"].values())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            for version in (b"v1", b"v22"):
                archive = Path(tmp) / "m.tar.bz2"
                make_archive(archive, {name: version for name in names})
                fetch_model.extract(archive, names, out)
            self.assertEqual((out / names[0]).read_bytes(), b"v22")
            self.assertEqual(fetch_model.verify_installed(out), "")

    def test_wrong_checksum_extracts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "m.tar.bz2"
            make_archive(archive, {"tokens.txt": b"x"})
            out = Path(tmp) / "out"
            self.assertEqual(fetch_model.main(["--archive", str(archive), "--dir", str(out)]), 2)
            self.assertFalse(out.exists())


class KeywordFilesTest(unittest.TestCase):
    def test_compiled_files_match_their_manifests(self):
        raw = hashlib.sha256((VOICE / "keywords_en.txt").read_bytes()).hexdigest()
        for key, spec in models.MODELS.items():
            compiled = VOICE / spec["compiled"]
            manifest = json.loads((VOICE / spec["compiled"].replace(".compiled.txt", ".manifest.json")).read_text())
            self.assertEqual(manifest["raw_sha256"], raw, f"{key}: re-run kws_prepare.py --model {key}")
            self.assertEqual(manifest["compiled_sha256"], hashlib.sha256(compiled.read_bytes()).hexdigest(), key)
            labels = sorted({line.rsplit("@", 1)[1].strip() for line in compiled.read_text().splitlines() if "@" in line})
            self.assertEqual(labels, ["crouch", "dance", "follow", "gaze", "greet", "home", "letsgo", "sit", "stand", "stop"], key)

    def test_bpe_raw_has_no_apostrophe(self):
        self.assertNotIn("'", models.raw_for("gigaspeech", "LET'S GO HOME"))
        self.assertIn("'", models.raw_for("zh-en", "LET'S GO HOME"))

    def test_default_dir_is_not_inside_a_release(self):
        self.assertNotIn("releases", str(models.MODELS["gigaspeech"]["default_dir"]))


if __name__ == "__main__":
    unittest.main()
