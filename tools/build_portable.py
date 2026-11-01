"""Build an offline Windows x64 demo from a verified official CPython bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


PYTHON_VERSION = "3.13.17"
PYTHON_URL = (
    "https://www.python.org/ftp/python/3.13.17/"
    "python-3.13.17-embed-amd64.zip"
)
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
PYTHON_RELEASE = "https://www.python.org/downloads/release/python-31317/"


def build(archive: Path, project_dir: Path) -> tuple[Path, Path]:
    actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual_hash != PYTHON_SHA256:
        raise ValueError("Python archive SHA-256 does not match the official release.")

    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-%f")
    package_dir = project_dir / "dist" / f"ai-agent-windows-x64-{stamp}"
    package_dir.mkdir(parents=True, exist_ok=False)
    for name in ("banking_agent", "tests", "tools", "docs"):
        if not (project_dir / name).is_dir():
            if name == "docs":
                continue
            raise ValueError(f"Required project directory is missing: {name}")
        shutil.copytree(
            project_dir / name,
            package_dir / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    for name in (
        "启动演示.cmd", "启动网页演示.cmd", "launch_demo.py", "先看这里.txt", "网页演示使用说明.txt", "README.md",
        "功能验收清单.md", "AI_Banking_Agent_System_Prompt.md",
        "banking_agent_config.template.json", "pyproject.toml",
    ):
        shutil.copy2(project_dir / name, package_dir / name)
    for name in ("部署说明.md", "GitHub上传指南.md", "SECURITY.md"):
        if (project_dir / name).is_file():
            shutil.copy2(project_dir / name, package_dir / name)

    runtime_dir = package_dir / "runtime"
    runtime_dir.mkdir()
    with zipfile.ZipFile(archive) as runtime_archive:
        for member in runtime_archive.infolist():
            member_path = (runtime_dir / member.filename).resolve()
            if not member_path.is_relative_to(runtime_dir.resolve()):
                raise ValueError("Unexpected path in Python archive.")
        runtime_archive.extractall(runtime_dir)

    # Keep Python isolated from the host, while allowing this extracted project.
    (runtime_dir / "python313._pth").write_text(
        "python313.zip\n.\n..\n", encoding="utf-8"
    )
    (package_dir / "PYTHON_RUNTIME_SOURCE.txt").write_text(
        f"CPython {PYTHON_VERSION}, Windows x64 embeddable distribution\n"
        f"Source: {PYTHON_URL}\nRelease and checksum: {PYTHON_RELEASE}\n"
        f"Original archive SHA-256: {PYTHON_SHA256}\n\n"
        "The original license is retained in runtime/LICENSE.txt.\n"
        "python313._pth adds the parent project directory to the isolated path.\n"
        "No global installation, PATH changes, pip, or network access is required.\n",
        encoding="utf-8",
    )

    zip_path = package_dir.parent / f"星河银行Agent-网页交互免安装版-{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as package_archive:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                package_archive.write(
                    path, Path("ai-agent-windows-x64") / path.relative_to(package_dir)
                )
    return package_dir, zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-archive", required=True, type=Path)
    arguments = parser.parse_args()
    folder, package = build(arguments.python_archive, Path(__file__).resolve().parents[1])
    print(json.dumps({"folder": str(folder), "zip": str(package)}, ensure_ascii=False))
