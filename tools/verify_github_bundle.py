"""Verify a source ZIP, its manifest, Markdown links, and tests in a clean extraction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from build_github_bundle import EXCLUDED_PARTS, REQUIRED_FILES


def verify(archive_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="GitHub 源码验收 ") as temp:
        root = Path(temp).resolve()
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP integrity check failed")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("ZIP contains duplicate entries")
            for name in names:
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts or "\\" in name
                        or not path.parts or path.parts[0] != "ai-banking-agent"
                        or EXCLUDED_PARTS.intersection(path.parts)):
                    raise ValueError("Unexpected package member: " + name)
                if not (root / name).resolve().is_relative_to(root):
                    raise ValueError("Package member escapes extraction directory")
            archive.extractall(root)
        project = root / "ai-banking-agent"
        missing = [name for name in REQUIRED_FILES if not (project/name).is_file()]
        if missing:
            raise ValueError("Missing required source or documentation: " + repr(missing))
        manifest = json.loads((project/"manifest.json").read_text(encoding="utf-8"))
        rows = manifest["files"]
        actual = {path.relative_to(project).as_posix() for path in project.rglob("*") if path.is_file()}
        expected = {row["path"] for row in rows} | {"manifest.json"}
        if actual != expected or len(rows) != len(expected)-1 or manifest["file_count_excluding_manifest"] != len(rows):
            raise ValueError("Manifest and package members disagree")
        for row in rows:
            content = (project/row["path"]).read_bytes()
            if len(content) != row["bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError("Source digest mismatch: " + row["path"])
        print(f"PASS: ZIP CRC, {len(actual)} files, required materials, exclusions and SHA-256 manifest", flush=True)
        checked = 0
        for markdown in project.rglob("*.md"):
            source = markdown.read_text(encoding="utf-8")
            source = re.sub(r"```[\s\S]*?```", "", source)
            for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", source):
                target = raw.strip().strip("<>")
                link = urlsplit(target)
                if link.scheme or link.netloc or not link.path:
                    continue
                path = (markdown.parent / unquote(link.path)).resolve()
                if not path.is_relative_to(project) or not path.exists():
                    raise ValueError(f"Broken local link: {markdown.relative_to(project)} -> {target}")
                checked += 1
        print(f"PASS: {checked} local Markdown document/image/source links", flush=True)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        tests = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-q"],
                               cwd=project, env=env, capture_output=True, encoding="utf-8", timeout=120)
        print(tests.stdout + tests.stderr, end="", flush=True)
        if tests.returncode:
            raise RuntimeError("Tests failed in clean source extraction")
        return {"archive": archive_path.name, "files": len(actual), "local_links": checked,
                "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "python": sys.version.split()[0], "platform": sys.platform,
                "clean_extraction_tests": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.archive.resolve()), ensure_ascii=False))
