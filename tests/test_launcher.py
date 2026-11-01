from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent 中文 空格 ")
        self.folder = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def copy_project(self):
        shutil.copy2(PROJECT_DIR / "launch_demo.py", self.folder)
        shutil.copytree(
            PROJECT_DIR / "banking_agent", self.folder / "banking_agent",
            ignore=shutil.ignore_patterns("__pycache__"),
        )

    def run_batch(self):
        launcher = self.folder / "启动演示.cmd"
        shutil.copy2(PROJECT_DIR / "启动演示.cmd", launcher)
        return subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher)],
            cwd=self.folder, input="\n", capture_output=True, encoding="utf-8",
            errors="replace", env=self.env, timeout=20,
        )

    def test_python_entry_runs_from_unrelated_working_directory(self):
        self.copy_project()
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(self.folder / "launch_demo.py")],
            cwd=PROJECT_DIR.parent, input="查余额\n退出\n", capture_output=True,
            encoding="utf-8", env=self.env, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("50000.00", result.stdout)
        self.assertIn("演示结束", result.stdout)
        self.assertTrue((self.folder / "data" / "demo-audit.jsonl").is_file())

    def test_incomplete_project_prints_error_and_saves_diagnostics(self):
        shutil.copy2(PROJECT_DIR / "launch_demo.py", self.folder)
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(self.folder / "launch_demo.py")],
            cwd=self.folder, capture_output=True, encoding="utf-8",
            env=self.env, timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("整个 ZIP 全部解压缩", result.stderr)
        log = (self.folder / "data" / "startup-error.log").read_text(encoding="utf-8")
        self.assertIn("RuntimeError", log)
        self.assertIn(str(self.folder), log)

    @unittest.skipUnless(os.name == "nt", "Windows batch entry")
    def test_script_opened_without_project_explains_extraction(self):
        result = self.run_batch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("全部解压缩", result.stdout)
        self.assertNotIn("你>", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows Python detection")
    def test_source_without_python_explains_environment_requirement(self):
        self.copy_project()
        self.env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
        result = self.run_batch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("未找到可用的 Python", result.stdout)
        self.assertNotIn("你>", result.stdout)


if __name__ == "__main__":
    unittest.main()
