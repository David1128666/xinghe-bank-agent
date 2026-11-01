"""Check the distributed ZIP through its actual Windows batch entry point."""

from __future__ import annotations

import argparse
from decimal import Decimal
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
import zipfile


def verify_web(project: Path, system_dir: Path, env: dict[str, str]) -> None:
    log_path = project / "web-launch-verification.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(system_dir / "cmd.exe"), "/d", "/c", str(project / "启动网页演示.cmd"),
             "--no-browser", "--port", "0"],
            cwd=project.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 15
            origin = None
            while time.monotonic() < deadline:
                output = log_path.read_text(encoding="utf-8", errors="replace")
                match = re.search(r"http://127\.0\.0\.1:(\d+)", output)
                if match:
                    origin = match.group(0)
                    port = int(match.group(1))
                    break
                if process.poll() is not None:
                    raise AssertionError(output)
                time.sleep(.1)
            if origin is None:
                raise AssertionError("Web launcher did not print its address: " + output)

            cookie = ""
            def request(method, path, body=None, csrf=""):
                nonlocal cookie
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(method, path,
                    body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                    headers={"Cookie": cookie, "Content-Type": "application/json",
                             "Origin": origin, "X-CSRF-Token": csrf})
                response = connection.getresponse()
                payload = response.read()
                if response.getheader("Set-Cookie"):
                    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                status = response.status
                connection.close()
                if status != 200:
                    raise AssertionError(f"{path}: {status} {payload!r}")
                return payload

            for path, marker in (("/", "星河银行".encode()), ("/app.css", b":root"),
                                 ("/app.js", b"transferForm")):
                if marker not in request("GET", path):
                    raise AssertionError("Missing packaged web resource: " + path)
            state = json.loads(request("GET", "/api/state"))
            def send(message):
                nonlocal state
                body = {
                    "message": message, "request_id": str(uuid.uuid4()),
                    "revision": state["revision"], "confirmation_token": state["confirmation_token"],
                }
                state = json.loads(request("POST", "/api/message", body, state["csrf"]))
                return body

            send("给小明转账300元")
            if state["task"]["state"] != "SUCCESS" or state["accounts"][0]["available_balance"] != "49700.00":
                raise AssertionError("Small transfer did not execute directly.")
            send("给小明转账3000元")
            if state["task"]["state"] != "AWAITING_CONFIRMATION" or state["accounts"][0]["available_balance"] != "49700.00":
                raise AssertionError("3000-CNY boundary must await confirmation.")
            send("确认转账")
            if state["accounts"][0]["available_balance"] != "46700.00":
                raise AssertionError("Confirmed transfer balance mismatch.")
            send("查看股票行情")
            stocks = state["task"]["result"]["data"]["stocks"]
            if not stocks or stocks[0]["stock_code"] != "XH001":
                raise AssertionError("Packaged virtual stock catalog is missing.")
            purchase = send("一键买入 XH001 100股")
            if state["task"]["state"] != "SUCCESS" or state["task"]["result"]["stock_order"]["holding_shares"] != 100:
                raise AssertionError("One-click virtual stock purchase failed.")
            balance = state["accounts"][0]["available_balance"]
            state = json.loads(request("POST", "/api/message", purchase, state["csrf"]))
            if state["accounts"][0]["available_balance"] != balance:
                raise AssertionError("Stock request replay debited twice.")
            send("股票持仓")
            if state["task"]["result"]["data"]["stock_holdings"][0]["shares"] != 100:
                raise AssertionError("Purchased virtual stock holding is missing.")
            before = Decimal(state["accounts"][0]["available_balance"])
            send("给张三转账2888元")
            pending = state["pending_recipient"]
            if not pending or pending["name"] != "张三" or Decimal(state["accounts"][0]["available_balance"]) != before:
                raise AssertionError("New recipient must await identity confirmation without paying.")
            approval = {
                "action": "confirm_recipient", "request_id": str(uuid.uuid4()),
                "revision": state["revision"], "recipient_token": pending["token"],
                "payload": {"name": "张三", "account_last4": "7788"},
            }
            state = json.loads(request("POST", "/api/message", approval, state["csrf"]))
            contacts = [row for row in state["recipients"] if row["name"] == "张三"]
            if (state["task"]["state"] != "SUCCESS" or len(contacts) != 1
                    or contacts[0]["account_last4"] != "7788"
                    or Decimal(state["accounts"][0]["available_balance"]) != before - Decimal("2888")):
                raise AssertionError("Identity approval must save the recipient and pay once.")
            state = json.loads(request("POST", "/api/message", approval, state["csrf"]))
            if Decimal(state["accounts"][0]["available_balance"]) != before - Decimal("2888"):
                raise AssertionError("Identity approval replay debited twice.")
            send("给张三转账25元")
            if (state["pending_recipient"] is not None or state["task"]["state"] != "SUCCESS"
                    or Decimal(state["accounts"][0]["available_balance"]) != before - Decimal("2913")):
                raise AssertionError("Verified recipient must support subsequent direct small transfers.")
            print("PASS: real web CMD, assets, direct transfer, 3000 boundary, stocks, identity popup API, recipient list, approval replay, repeat transfer")
        finally:
            if process.poll() is None:
                stopped = subprocess.run(
                    [str(system_dir / "taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                )
                if stopped.returncode != 0 and process.poll() is None:
                    raise RuntimeError("Cannot clean up the test process tree: " +
                                       (stopped.stdout + stopped.stderr).decode(errors="replace"))
            process.wait(timeout=10)


def verify(archive: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Run this verification on Windows.")
    env = os.environ.copy()
    system_dir = Path(os.environ["SystemRoot"]) / "System32"
    env["PATH"] = str(system_dir)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with tempfile.TemporaryDirectory(prefix="Agent 分发验收 含空格 ") as temp_dir:
        unpack_dir = Path(temp_dir)
        with zipfile.ZipFile(archive) as package:
            if package.testzip() is not None:
                raise AssertionError("ZIP CRC verification failed.")
            names = [Path(name).parts for name in package.namelist()]
            if any({".git", "data", "__pycache__"}.intersection(parts) for parts in names):
                raise AssertionError("Development data leaked into the distributed ZIP.")
            roots = {parts[0] for parts in names if parts}
            if len(roots) != 1:
                raise AssertionError("The portable ZIP must have exactly one project folder.")
            for member in package.namelist():
                if not (unpack_dir / member).resolve().is_relative_to(unpack_dir.resolve()):
                    raise AssertionError("Unexpected path in the portable ZIP.")
            package.extractall(unpack_dir)
        project = unpack_dir / next(iter(roots))
        launcher = project / "启动演示.cmd"
        result = subprocess.run(
            [str(system_dir / "cmd.exe"), "/d", "/c", str(launcher)],
            cwd=unpack_dir,
            input="查余额\n现在从活期转账25元给小明用途午餐\n确认转账\n查余额\n退出\n\n",
            capture_output=True, encoding="utf-8", errors="replace", env=env,
            timeout=30,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            raise AssertionError(output)
        for expected in ("runtime\\python.exe", "你>", "50000.00", "49975.00", "演示结束"):
            if expected not in output:
                raise AssertionError(f"Missing {expected!r}:\n{output}")
        print("PASS: extracted ZIP, Chinese/spaces path, isolated PATH, batch entry, transfer, exit")
        verify_web(project, system_dir, env)
        tests = subprocess.run(
            [str(project / "runtime" / "python.exe"), "-X", "utf8", "-m", "unittest",
             "discover", "-s", "tests", "-q"],
            cwd=project, capture_output=True, encoding="utf-8", env=env, timeout=60,
        )
        print(tests.stdout + tests.stderr, end="")
        if tests.returncode != 0:
            raise AssertionError("Bundled-Python test run failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    verify(args.archive)
