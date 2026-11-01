"""Arrange the reviewed project and standalone documents in the requested delivery layout."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile


FLAT_DOCS = {
    "核心算法.md": "1-核心算法说明.md",
    "安全设计.md": "2-安全设计文档.md",
    "权限分级实现.md": "3-权限分级实现说明.md",
    "已知风险清单.md": "4-18项已知风险清单.md",
}


def validate_links(root: Path) -> int:
    count = 0
    for document in root.rglob("*.md"):
        source = re.sub(r"```[\s\S]*?```", "", document.read_text(encoding="utf-8"))
        for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", source):
            link = urlsplit(raw.strip().strip("<>"))
            if link.scheme or link.netloc or not link.path:
                continue
            target = (document.parent / unquote(link.path)).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                raise ValueError(f"Broken grouped link: {document.relative_to(root)} -> {raw}")
            count += 1
    return count


def external_document(source: str, in_diagram_folder: bool = False) -> str:
    prefix = "../" if in_diagram_folder else ""

    def replace_link(match):
        raw = match.group(2)
        link = urlsplit(raw)
        if link.scheme or not link.path:
            return match.group(0)
        path = unquote(link.path)
        if path.startswith("../"):
            destination = prefix + "GitHub上传文件/" + path[3:]
        elif path in FLAT_DOCS:
            destination = prefix + FLAT_DOCS[path]
        elif path == "系统架构.md":
            destination = "系统架构说明.md" if in_diagram_folder else "系统架构图/系统架构说明.md"
        else:
            destination = prefix + "GitHub上传文件/docs/" + path
        if link.fragment:
            destination += "#" + link.fragment
        return f"{match.group(1)}({destination})"

    return re.sub(r"(!?\[[^\]]*\])\(([^)]+)\)", replace_link, source)


def build(archive_path: Path, project: Path) -> dict:
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    delivery = project / "dist" / f"分组交付-{stamp}" / "星河银行Agent-分组交付资料"
    delivery.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="分组源文件验收 ") as temp:
        base = Path(temp)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip():
                raise ValueError("Source ZIP integrity failed")
            for name in archive.namelist():
                if not (base/name).resolve().is_relative_to(base.resolve()):
                    raise ValueError("Source ZIP contains an unsafe path")
            archive.extractall(base)
        source = base / "ai-banking-agent"
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        for row in manifest["files"]:
            if hashlib.sha256((source / row["path"]).read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("Source manifest mismatch: " + row["path"])
        repository = delivery / "GitHub上传文件"
        shutil.copytree(source, repository)
        for old_name, new_name in FLAT_DOCS.items():
            document = (source / "docs" / old_name).read_text(encoding="utf-8")
            (delivery / new_name).write_text(external_document(document), encoding="utf-8")
        diagrams = delivery / "系统架构图"
        diagrams.mkdir()
        for suffix in ("svg", "png"):
            shutil.copy2(source / "docs" / f"架构图.{suffix}", diagrams / f"系统架构图.{suffix}")
        architecture = external_document((source/"docs/系统架构.md").read_text(encoding="utf-8"), True)
        architecture = architecture.replace("# 系统架构说明", "# 系统架构说明\n\n静态总览：[SVG](系统架构图.svg) · [PNG](系统架构图.png)。另见 [请求时序图](请求时序图.md) 和 [任务状态图](任务状态图.md)。", 1)
        (diagrams/"系统架构说明.md").write_text(architecture, encoding="utf-8")
        assets = project / "output/分组交付素材/系统架构图"
        for stem in ("请求时序图", "任务状态图"):
            for suffix in ("md", "mmd", "svg", "png"):
                shutil.copy2(assets / f"{stem}.{suffix}", diagrams / f"{stem}.{suffix}")
        guide = (source/"GitHub上传指南.md").read_text(encoding="utf-8")
        guide = guide.replace("全部解压缩源码 ZIP，进入 `ai-banking-agent` 文件夹。", "全部解压缩本次分组交付 ZIP，进入 `GitHub上传文件` 文件夹。")
        guide = guide.replace("**解压后的 `ai-banking-agent` 目录**", "**本次交付中的 `GitHub上传文件` 目录**")
        guide = guide.replace("## 一、最省事的上传顺序", "上传时只选择 `GitHub上传文件` 里面的内容。外层的四份独立说明与 `系统架构图` 文件夹是便于阅读的交付材料；完整仓库内已保留对应文档，使 README 链接可独立使用。\n\n## 一、最省事的上传顺序")
        (delivery/"GitHub上传指南.md").write_text(guide, encoding="utf-8")
        test_doc = (repository/"docs/测试用例.md").read_text(encoding="utf-8")
        def root_test_link(match):
            raw = match.group(2)
            link = urlsplit(raw)
            if link.scheme or not link.path:
                return match.group(0)
            destination = raw[3:] if raw.startswith("../") else "docs/"+raw
            return f"{match.group(1)}({destination})"
        test_doc = re.sub(r"(!?\[[^\]]*\])\(([^)]+)\)", root_test_link, test_doc)
        (repository/"84项测试用例.md").write_text(test_doc, encoding="utf-8")
        # Regenerate the repository manifest after adding the easy-to-find test document.
        rows = []
        for path in sorted(repository.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                content = path.read_bytes()
                rows.append({"path": path.relative_to(repository).as_posix(), "bytes": len(content),
                             "sha256": hashlib.sha256(content).hexdigest()})
        manifest.update(files=rows, file_count_excluding_manifest=len(rows), created_at=now.isoformat())
        (repository/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        # Business code and test code are identical to the previously tested source bundle.
        for directory in ("banking_agent", "tests"):
            for path in (source/directory).rglob("*"):
                if path.is_file() and path.read_bytes() != (repository/path.relative_to(source)).read_bytes():
                    raise ValueError("Business/test source changed during organization")
    checked = validate_links(delivery)
    zip_path = delivery.parent / f"星河银行Agent-分组交付资料-{stamp}.zip"
    with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(delivery.rglob("*")):
            if path.is_file():
                archive.write(path, Path(delivery.name)/path.relative_to(delivery))
    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip():
            raise ValueError("Grouped ZIP integrity failed")
    return {"folder": str(delivery), "zip": str(zip_path), "local_links": checked,
            "files": sum(1 for path in delivery.rglob("*") if path.is_file()),
            "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_zip",type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source_zip.resolve(),Path(__file__).resolve().parents[1]),ensure_ascii=False))
