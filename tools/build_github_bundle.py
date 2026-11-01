"""Create a source-only GitHub upload bundle from an explicit public-file allowlist.

Run from the project checkout with Python 3.11+; no third-party packages are needed.
The existing checkout, Git history, runtime, and local data are never copied.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


ROOT_FILES = (
    ".gitignore", "README.md", "部署说明.md", "GitHub上传指南.md", "pyproject.toml", "launch_demo.py",
    "启动演示.cmd", "启动网页演示.cmd", "先看这里.txt", "网页演示使用说明.txt",
    "功能验收清单.md", "AI_Banking_Agent_System_Prompt.md",
    "banking_agent_config.template.json",
)
OPTIONAL_ROOT_FILES = ("SECURITY.md", "LICENSE", "LICENSE.md", "LICENSE.txt")
REQUIRED_FILES = ROOT_FILES + (
    "banking_agent/__main__.py", "banking_agent/agent.py",
    "banking_agent/web_app.py", "banking_agent/web/index.html",
    "banking_agent/web/app.js", "banking_agent/web/app.css",
    "tests/test_agent.py", "tests/test_demo.py", "tests/test_launcher.py",
    "tests/test_recipients.py", "tests/test_stocks.py", "tests/test_web.py",
    "tools/build_github_bundle.py", ".github/workflows/tests.yml",
    "docs/系统架构.md", "docs/核心算法.md", "docs/安全设计.md",
    "docs/权限分级实现.md", "docs/已知风险清单.md", "docs/测试用例.md",
    "docs/README.md", "docs/架构图.svg", "docs/架构图.png",
    "提交材料/00-评委阅读指引.md",
)
DIRECTORY_SUFFIXES = {
    "banking_agent": {".py", ".html", ".css", ".js", ".svg"},
    "tests": {".py"},
    "tools": {".py"},
    "docs": {".md", ".svg", ".png", ".html"},
    "提交材料": {".md", ".pdf", ".docx", ".pptx"},
    ".github/workflows": {".yml", ".yaml"},
}
EXCLUDED_PARTS = {
    ".git", "data", "dist", "runtime", "output", "tmp", "__pycache__",
    ".venv", "node_modules", ".pytest_cache", ".idea", ".vscode",
}


def public_files(project_dir: Path) -> list[Path]:
    """Validate required material first, then select only public source files."""
    missing = [name for name in REQUIRED_FILES if not (project_dir / name).is_file()]
    if missing:
        raise ValueError("Required delivery files are missing: " + ", ".join(missing))
    selected = {project_dir / name for name in ROOT_FILES}
    selected.update(project_dir / name for name in OPTIONAL_ROOT_FILES
                    if (project_dir / name).is_file())
    for directory, suffixes in DIRECTORY_SUFFIXES.items():
        source = project_dir / directory
        if source.is_symlink():
            raise ValueError("Public source directories must not be symlinks: " + directory)
        for path in source.rglob("*"):
            relative = path.relative_to(project_dir)
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("Symlinks are not included in the source bundle: " + relative.as_posix())
            if path.is_file() and path.suffix.lower() in suffixes:
                selected.add(path)
    for path in selected:
        if path.is_symlink() or not path.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError("Public file escapes the checkout: " + str(path))
    return sorted(selected, key=lambda path: path.relative_to(project_dir).as_posix())


def build(project_dir: Path) -> dict[str, str | int]:
    project_dir = project_dir.resolve()
    files = public_files(project_dir)
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    package_dir = project_dir / "dist" / f"github-{stamp}" / "ai-banking-agent"
    package_dir.mkdir(parents=True, exist_ok=False)
    manifest_rows = []
    for source in files:
        relative = source.relative_to(project_dir)
        target = package_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        contents = target.read_bytes()
        manifest_rows.append({"path": relative.as_posix(), "bytes": len(contents),
                              "sha256": hashlib.sha256(contents).hexdigest()})
    manifest = {
        "schema_version": 1,
        "bundle_type": "source-only",
        "created_at": now.isoformat(),
        "file_count_excluding_manifest": len(manifest_rows),
        "note": "Hashes describe bundled source and documents, not a security certification. "
                "manifest.json is excluded from its own file list.",
        "files": manifest_rows,
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    zip_path = project_dir / "dist" / f"星河银行Agent-GitHub源码包-{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(package_dir.rglob("*")):
            if source.is_file():
                archive.write(source, Path("ai-banking-agent") / source.relative_to(package_dir))
    return {"folder": str(package_dir), "zip": str(zip_path),
            "file_count": len(manifest_rows) + 1,
            "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = build(Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=False))
