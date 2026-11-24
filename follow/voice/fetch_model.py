"""Download and verify a KWS model listed in voice/models.py. Standard library only.

    python3 follow/voice/fetch_model.py [--model gigaspeech] [--dir ~/microduck-follow/models/kws-gigaspeech]
    python3 follow/voice/fetch_model.py --archive ./that.tar.bz2      # offline: use a copied archive

Checks the archive's SHA256 against the value recorded in models.py, then extracts only the
listed files (basenames, size-capped; never extractall), so a system without the `bzip2`
command (WSL, some images) works too. Moves nothing and touches nothing outside --dir.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import MODELS  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


MANIFEST = "manifest.json"


def extract(archive: Path, wanted, destination: Path, model_id: str = "") -> None:
    """Extract into a sibling `.partial` dir, write the manifest last, then swap it into place,
    so an interrupted run never leaves a destination that looks complete but holds truncated files."""
    import json
    partial = destination.with_name(destination.name + ".partial")
    old = destination.with_name(destination.name + ".old")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    with tarfile.open(archive, "r:bz2") as bundle:
        members = {Path(m.name).name: m for m in bundle.getmembers() if m.isfile()}
        for name in wanted:
            member = members.get(name)
            if member is None or member.size > 100_000_000:
                shutil.rmtree(partial, ignore_errors=True)
                raise ValueError(f"壓縮檔裡沒有 {name}（或大小異常）")
            with bundle.extractfile(member) as source, (partial / name).open("wb") as target:
                shutil.copyfileobj(source, target)
    files = {name: {"size": (partial / name).stat().st_size, "sha256": sha256(partial / name)} for name in wanted}
    (partial / MANIFEST).write_text(json.dumps({"model_id": model_id, "files": files}, indent=2), encoding="utf-8")
    shutil.rmtree(old, ignore_errors=True)
    if destination.exists():
        destination.rename(old)
    partial.rename(destination)
    shutil.rmtree(old, ignore_errors=True)


def verify_installed(directory: Path) -> str:
    """"" if `directory` holds a complete extraction (manifest present, sizes match), else why not."""
    import json
    manifest = directory / MANIFEST
    if not manifest.is_file():
        return f"{directory} 沒有 {MANIFEST}（沒解壓完或不是 fetch_model.py 裝的）"
    for name, info in json.loads(manifest.read_text(encoding="utf-8"))["files"].items():
        path = directory / name
        if not path.is_file() or path.stat().st_size != info["size"]:
            return f"{path} 缺少或大小不符"
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gigaspeech", choices=[k for k, v in MODELS.items() if "url" in v])
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--archive", type=Path, help="use an already downloaded archive")
    args = parser.parse_args(argv)
    spec = MODELS[args.model]
    destination = args.dir or spec["default_dir"]
    with tempfile.TemporaryDirectory() as tmp:
        archive = args.archive
        if archive is None:
            archive = Path(tmp) / (spec["id"] + ".tar.bz2")
            print(f"下載 {spec['url']}", flush=True)
            with urllib.request.urlopen(spec["url"], timeout=60) as source, archive.open("wb") as target:
                shutil.copyfileobj(source, target)
        actual = sha256(archive)
        if actual != spec["sha256"]:
            print(f"SHA256 不符：{actual}\n預期：{spec['sha256']}\n不解壓。", file=sys.stderr)
            return 2
        extract(archive, list(spec["files"].values()), destination, spec["id"])
    print(f"已驗證並解壓到 {destination}：{', '.join(spec['files'].values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
