from __future__ import annotations

from http.client import HTTPConnection
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from banking_agent.web_app import LocalBankServer
from banking_agent.models import Operation, RiskAssessment, RiskLevel


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = LocalBankServer(audit_dir=self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.state = self.get_state()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Cookie": self.cookie, "Content-Type": "application/json", "Origin": self.server.origin, "X-CSRF-Token": getattr(self, "state", {}).get("csrf", "")}
        request_headers.update(headers or {})
        connection.request(method, path, body=None if body is None else json.dumps(body, ensure_ascii=False).encode(), headers=request_headers)
        result = connection.getresponse()
        payload = result.read()
        if result.getheader("Set-Cookie"):
            self.cookie = result.getheader("Set-Cookie").split(";", 1)[0]
        status = result.status
        content_type = result.getheader("Content-Type")
        connection.close()
        return status, json.loads(payload) if "application/json" in content_type else payload

    def get_state(self):
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200, body)
        return body

    def body(self, message):
        return {"request_id": str(uuid.uuid4()), "message": message, "revision": self.state["revision"], "confirmation_token": self.state["confirmation_token"]}

    def send(self, message):
        status, result = self.request("POST", "/api/message", self.body(message))
        self.assertEqual(status, 200, result)
        self.state = result
        return result

    def test_real_preview_confirmation_and_retry_do_not_double_debit(self):
        self.send("现在从活期转账3000元给小明用途午餐")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")
        body = self.body("确认转账")
        status, result = self.request("POST", "/api/message", body)
        self.assertEqual(status, 200)
        self.assertEqual(result["task"]["state"], "SUCCESS")
        self.assertEqual(result["accounts"][0]["available_balance"], "47000.00")
        status, repeated = self.request("POST", "/api/message", body)
        self.assertEqual(status, 200)
        self.assertEqual(repeated["accounts"][0]["available_balance"], "47000.00")
        self.assertEqual(len(repeated["messages"]), 5)
        self.assertTrue(list(Path(self.temp.name).glob("*.jsonl")))

    def test_stale_tabs_and_mismatched_preview_are_rejected(self):
        old_body = self.body("现在从活期转账3000元给小明用途午餐")
        self.send("查余额")
        self.assertEqual(self.request("POST", "/api/message", old_body)[0], 409)
        self.send("现在从活期转账3000元给小明用途午餐")
        body = self.body("确认转账")
        body["confirmation_token"] = "outdated-preview"
        self.assertEqual(self.request("POST", "/api/message", body)[0], 409)
        body["message"] = " 确认转账 \n"
        body["confirmation_token"] = "错误的旧摘要"
        self.assertEqual(self.request("POST", "/api/message", body)[0], 409)
        self.assertEqual(self.get_state()["accounts"][0]["available_balance"], "50000.00")

    def test_cancel_choice_and_card_changes_share_the_original_agent(self):
        self.send("现在从活期转账3000元给张伟用途AA")
        self.assertEqual(len(self.state["task"]["recipient_choices"]), 2)
        self.send("1")
        self.assertEqual(self.state["task"]["preview"]["payee_account_last4"], "2211")
        self.send("取消")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")
        self.send("临时锁卡尾号9012")
        self.send("确认卡片操作")
        self.send("查看卡片")
        self.assertEqual(self.state["task"]["result"]["data"]["cards"][0]["status"], "temporarily_locked")

    def test_origin_csrf_host_and_static_path_boundaries(self):
        body = self.body("查余额")
        self.assertEqual(self.request("POST", "/api/message", body, {"Origin": "https://outside.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/message", body, {"X-CSRF-Token": "bad"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "outside.example"})[0], 403)
        self.assertEqual(self.request("GET", "/../demo.py")[0], 404)
        for path in ("/", "/app.css", "/app.js", "/favicon.svg"):
            status, payload = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertGreater(len(payload), 10)

    def test_sessions_are_isolated_and_refresh_preserves_current_state(self):
        self.send("现在从活期转账25元给小明用途午餐")
        first_cookie = self.cookie
        self.assertEqual(self.get_state()["accounts"][0]["available_balance"], "49975.00")
        self.cookie = ""
        second = self.get_state()
        self.assertEqual(second["accounts"][0]["available_balance"], "50000.00")
        self.cookie = first_cookie
        self.assertEqual(self.get_state()["accounts"][0]["available_balance"], "49975.00")

    def test_reset_and_unknown_text_do_not_show_previous_result(self):
        self.send("查余额")
        self.send("你好随便说一句")
        self.assertIsNone(self.state["messages"][-1]["task"])
        body = self.body("")
        body["action"] = "reset"
        status, result = self.request("POST", "/api/message", body)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["accounts"][0]["available_balance"], "50000.00")

    def test_malformed_input_and_reused_id_with_different_content(self):
        body = self.body("查余额")
        self.assertEqual(self.request("POST", "/api/message", body)[0], 200)
        body["message"] = "查看卡片"
        self.assertEqual(self.request("POST", "/api/message", body)[0], 409)
        self.assertEqual(self.request("POST", "/api/message", [1, 2])[0], 400)
        self.assertEqual(self.request("POST", "/api/message", self.body(""))[0], 409)

    def test_review_never_debits_and_overview_does_not_replace_pending_task(self):
        self.send("现在从活期转账25元给小明用途安全账户")
        task_id = self.state["task"]["task_id"]
        refreshed = self.get_state()
        self.assertEqual(refreshed["task"]["task_id"], task_id)
        self.send("确认转账")
        self.assertEqual(self.state["task"]["state"], "UNDER_REVIEW")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")

    def test_short_transfer_executes_once_and_audits_configured_policy(self):
        body = self.body("给小明转账25元")
        status, self.state = self.request("POST", "/api/message", body)
        self.assertEqual(status, 200)
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.state["task"]["confirmation_policy"], "virtual_transfer_below_3000")
        self.assertIsNone(self.state["confirmation_token"])
        self.assertEqual(self.state["accounts"][0]["available_balance"], "49975.00")
        status, replayed = self.request("POST", "/api/message", body)
        self.assertEqual(status, 200)
        self.assertEqual(replayed["accounts"][0]["available_balance"], "49975.00")
        self.assertEqual(len(replayed["messages"]), 3)
        events = [json.loads(line) for path in Path(self.temp.name).glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
        direct = next(event for event in events if event["action"] == "configured_transfer_authorization_used")
        self.assertFalse(direct["confirmation"]["required"])
        self.assertFalse(direct["confirmation"]["received"])
        self.assertFalse(any(event["action"] == "confirmation_received" for event in events))

    def test_direct_transfer_boundary_and_cent_precision(self):
        self.send("给小明转账2999.99元")
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "47000.01")
        self.send("给小明转账3000元")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "47000.01")
        self.send("取消")
        self.send("给小明转账3000.01元")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.send("取消")
        self.send("给小明转账0.01元")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "47000.00")

    def test_small_transfer_needs_recipient_choice_and_missing_amount(self):
        self.send("给张伟转账50元")
        self.assertEqual(self.state["task"]["state"], "AWAITING_INFORMATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")
        self.send("2")
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.state["task"]["preview"]["payee_account_last4"], "3344")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "49950.00")
        self.send("给小明转账")
        self.assertEqual(self.state["task"]["state"], "AWAITING_INFORMATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "49950.00")
        self.send("25元")
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "49925.00")

    def test_authentication_and_scheduled_transfers_keep_their_gates(self):
        self.send("切换到IM")
        self.send("给小明转账25元")
        self.assertEqual(self.state["task"]["state"], "AWAITING_AUTHENTICATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")
        # The old text command must NOT restore the enhanced level any more.
        self.send("切换到APP")
        self.assertEqual(self.state["task"]["state"], "AWAITING_AUTHENTICATION")
        self.send(f"认证 {self.state['auth']['code']}")
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "49975.00")
        self.send("定时转账2026-11-01从活期转账100元给小明用途房租")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "49975.00")

    def test_first_identity_and_daily_limit_keep_their_gates(self):
        self.send("给林女士转账25元")
        self.assertEqual(self.state["pending_recipient"]["name"], "林女士")
        self.assertIsNone(self.state["task"])
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")
        body = self.body("")
        body.update(action="cancel_recipient", recipient_token=self.state["pending_recipient"]["token"])
        status, self.state = self.request("POST", "/api/message", body)
        self.assertEqual(status, 200)
        session = next(iter(self.server.sessions.values()))
        session.bank._record_transfer("checking", Decimal(str(session.agent.config.daily_transfer_limit)) - Decimal("10"))
        self.send("给小明转账25元")
        self.assertEqual(self.state["task"]["risk_level"], "R4")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")

    def test_changed_risk_or_quote_falls_back_to_confirmation(self):
        session = next(iter(self.server.sessions.values()))
        original_assess = session.bank.assess
        transfer_calls = 0
        def changing_risk(request, prepared):
            nonlocal transfer_calls
            if request.operation is Operation.TRANSFER:
                transfer_calls += 1
                if transfer_calls >= 2:
                    return RiskAssessment(RiskLevel.R4, manual_review_required=True)
            return original_assess(request, prepared)
        session.bank.assess = changing_risk
        self.send("给小明转账25元")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["task"]["risk_level"], "R4")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")
        self.send("取消")
        session.bank.assess = original_assess
        original_preview = session.bank.preview
        preview_calls = 0
        def changing_preview(request, prepared):
            nonlocal preview_calls
            quote = original_preview(request, prepared)
            if request.operation is Operation.TRANSFER:
                preview_calls += 1
                if preview_calls >= 2:
                    return replace(quote, fee="1.00")
            return quote
        session.bank.preview = changing_preview
        self.send("给小明转账25元")
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["task"]["preview"]["fee"], "1.00")
        self.assertEqual(self.state["accounts"][0]["available_balance"], "50000.00")


if __name__ == "__main__":
    unittest.main()
