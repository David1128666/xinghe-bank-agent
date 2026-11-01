"""Launch an extracted demo folder and leave useful diagnostics on failure."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    error_log = project_dir / "data" / "startup-error.log"
    try:
        if sys.version_info < (3, 11):
            raise RuntimeError("需要 Python 3.11 或更新版本，请使用完整免安装版。")
        if not (project_dir / "banking_agent" / "__main__.py").is_file():
            raise RuntimeError("项目文件不完整，请先将整个 ZIP 全部解压缩。")
        os.chdir(project_dir)
        # Embeddable Python isolates sys.path; make this project's imports explicit.
        sys.path.insert(0, str(project_dir))
        if "--web" in sys.argv[1:]:
            sys.argv.remove("--web")
            from banking_agent.web_app import main as run_web

            run_web()
        else:
            from banking_agent.demo import run_demo

            run_demo("data/demo-audit.jsonl")
        return 0
    except KeyboardInterrupt:
        print("\n演示结束。")
        return 0
    except Exception:
        details = traceback.format_exc()
        print("\n[启动或运行失败]", file=sys.stderr)
        print(details, file=sys.stderr, end="")
        print("请先确认已经全部解压缩，并把完整报错窗口发给项目提供者。", file=sys.stderr)
        try:
            error_log.parent.mkdir(parents=True, exist_ok=True)
            error_log.write_text(
                f"Python: {sys.version}\nExecutable: {sys.executable}\n"
                f"Project: {project_dir}\n\n{details}",
                encoding="utf-8",
            )
            print(f"错误详情已保存：{error_log}", file=sys.stderr)
        except OSError:
            print("无法写入错误日志，请把项目移到可写目录后再运行。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
