"""Create a clean, credential-free Windows portable API edition."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from build_portable import build


def build_api(archive: Path, project: Path):
    prepared, _original_zip = build(archive, project)
    name = "星河银行Agent-API智能版-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = prepared.parent / name
    prepared.rename(destination)
    shutil.copytree(project / ".github", destination / ".github")
    shutil.copy2(project / ".gitignore", destination / ".gitignore")
    runtime_source = destination / "PYTHON_RUNTIME_SOURCE.txt"
    content = runtime_source.read_text(encoding="utf-8")
    runtime_source.write_text(content.replace(
        "No global installation, PATH changes, pip, or network access is required.",
        "No global Python installation, PATH changes, pip, or extra packages are required.\n"
        "The local demo runs offline. API conversation requires a configured provider and network access.\n"
        "API credentials are entered in the local page and are never bundled."), encoding="utf-8")
    manifest = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            manifest[path.relative_to(destination).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (destination / "DELIVERY_MANIFEST.json").write_text(json.dumps({
        "edition": "api-agent", "created": datetime.now().isoformat(timespec="seconds"),
        "providers": ["deepseek", "qwen", "openai", "custom"],
        "credentials_included": False, "files": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    package = destination.with_suffix(".zip")
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(name) / path.relative_to(destination))
    return {"folder": str(destination), "zip": str(package), "files": len(manifest) + 1,
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-archive", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(build_api(arguments.python_archive, Path(__file__).resolve().parents[1]), ensure_ascii=False))
