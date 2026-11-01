"""Regression tests for the security hardening round (2026-11-01)."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from banking_agent.audit import AuditError, AuditLog
from banking_agent.demo import (AuthChallenge, DemoConversation, SimulatedBank,
                                TransferLedger, build_demo_agent)
from banking_agent.llm_client import APIConfig, LLMError
from banking_agent.models import Operation, OperationRequest, RiskLevel, TaskState
from banking_agent.privacy import redact_text
from banking_agent.web_app import LocalBankServer


class AuthenticationHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent, self.bank = build_demo_agent(str(Path(self.temp.name) / "auth.jsonl"))
        self.chat = DemoConversation(self.agent, self.bank)

    def tearDown(self):
        self.temp.cleanup()

    def test_chat_sentence_cannot_raise_authentication(self):
        self.chat.handle("切换到即时通讯")
        self.assertEqual(self.chat.channel, "im")
        reply = self.chat.handle("切换到APP")
        self.assertIn("不能只靠一句指令", reply)
        self.assertEqual(self.chat.channel, "im")
        reply = self.chat.handle("现在从活期转账10元给小明用途测试")
        self.assertIn("认证级别不足", reply)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_demo_code_upgrades_once_and_cannot_be_replayed(self):
        self.chat.handle("切换到即时通讯")
        code = self.chat.auth_challenge.code
        self.assertIn("增强认证", self.chat.handle(f"认证 {code}"))
        self.assertEqual(self.chat.channel, "app")
        self.chat.handle("切换到即时通讯")
        self.assertIn("错误或已过期", self.chat.handle(f"认证 {code}"))
        self.assertEqual(self.chat.channel, "im")

    def test_expired_code_is_rejected(self):
        challenge = AuthChallenge(ttl_seconds=0)
        self.assertFalse(challenge.verify(challenge.code))


class AmountAndConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent, self.bank = build_demo_agent(str(Path(self.temp.name) / "amount.jsonl"))
        self.chat = DemoConversation(self.agent, self.bank)

    def tearDown(self):
        self.temp.cleanup()

    def test_stock_purchase_requires_confirmation(self):
        self.chat.handle("买入 XH001 100股")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.chat.handle("确认投资操作")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("48718.72"))

    def test_three_decimal_amount_is_quantised_before_preview(self):
        task = self.agent.submit(OperationRequest(
            Operation.TRANSFER, "demo-user", "demo-session", "转账 1.005 元给小明",
            {"recipient_query": "小明", "amount": "1.005", "currency": "CNY",
             "execution_time": "now", "frequency": "once", "purpose": "测试",
             "source_account_ref": "checking"},
            request_id="amount-quantise-01", channel="app"))
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(task.preview.amount, "1.01")
        task = self.agent.confirm(task.task_id, "demo-user", "demo-session", "确认转账")
        self.assertEqual(task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("49998.99"))

    def test_malformed_amounts_are_rejected_without_debit(self):
        for text in ("给小明转账1e999元", "给小明转账1,000,000元", "给小明转账１００元"):
            with self.subTest(text=text):
                before = self.bank.accounts["checking"]["balance"]
                reply = self.chat.handle(text)
                self.assertIn("金额", reply)
                self.assertEqual(self.bank.accounts["checking"]["balance"], before)

    def test_shared_ledger_blocks_cross_session_overrun(self):
        ledger = TransferLedger()
        banks = []
        for _ in range(3):
            _, bank = build_demo_agent(str(Path(self.temp.name) / f"ledger-{len(banks)}.jsonl"))
            bank.ledger = ledger
            banks.append(bank)
        for bank in banks[:2]:
            self.assertIsNone(bank._transfer_limit_risk(Decimal("9000")))
            bank._record_transfer("checking", Decimal("9000"))
        risk = banks[2]._transfer_limit_risk(Decimal("9000"))
        self.assertIsNotNone(risk)
        self.assertIs(risk.level, RiskLevel.R4)


class PrivacyAndTransportTests(unittest.TestCase):
    def test_short_codes_are_masked(self):
        self.assertNotIn("123456", redact_text("验证码是123456，请勿泄露"))
        self.assertIn("••••", redact_text("验证码是123456，请勿泄露"))
        self.assertEqual(redact_text("2026-10-29"), "2026-10-29")

    def test_private_model_endpoints_are_rejected(self):
        for url in ("https://10.0.0.5/v1", "https://192.168.1.2/v1", "https://model.internal/v1",
                    "http://169.254.169.254/latest"):
            with self.subTest(url=url):
                with self.assertRaises(LLMError):
                    APIConfig("custom", url, "gpt-4o-mini", "sk-local-test")
        APIConfig("custom", "https://api.example.com/v1", "gpt-4o-mini", "sk-local-test")
        APIConfig("custom", "http://127.0.0.1:8080/v1", "local", "sk-local-test")


class AuditHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audit.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def test_events_carry_a_keyed_signature(self):
        log = AuditLog(self.path, b"unit-test-key")
        log.append({"action": "one"})
        log.append({"action": "two"})
        lines = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("event_mac", line)
        AuditLog(self.path, b"unit-test-key")  # re-opening verifies chain and signatures

    def test_signature_mismatch_is_detected(self):
        log = AuditLog(self.path, b"unit-test-key")
        log.append({"action": "one"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["event_mac"] = "0" * 64
        self.path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")) + "\n", encoding="utf-8")
        with self.assertRaises(AuditError):
            AuditLog(self.path, b"unit-test-key")


class SessionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = LocalBankServer(("127.0.0.1", 0), self.temp.name)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        if cookie:
            headers["Cookie"] = f"bank_session={cookie}"
            headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data, response

    def new_session(self):
        status, data, response = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        token = response.getheader("Set-Cookie").split("bank_session=")[1].split(";")[0]
        self.csrf = data["csrf"]
        return token, data

    def test_session_cap_evicts_instead_of_locking_out(self):
        for _ in range(34):
            self.new_session()
        self.assertLessEqual(len(self.server.sessions), self.server.max_sessions)
        status, _, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)

    def test_confirmation_token_is_single_use_and_bound(self):
        token, state = self.new_session()
        body = {"message": "给小明转账3000元", "request_id": uuid.uuid4().hex,
                "revision": state["revision"], "source": "chat"}
        status, data, _ = self.request("POST", "/api/message", body, cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["state"], "AWAITING_CONFIRMATION")
        nonce = data["confirmation_token"]
        self.assertTrue(nonce and len(nonce) >= 20)
        body = {"message": "确认转账", "request_id": uuid.uuid4().hex,
                "revision": data["revision"], "confirmation_token": nonce}
        status, data, _ = self.request("POST", "/api/message", body, cookie=token)
        self.assertEqual(status, 200)
        self.assertIsNone(data["confirmation_token"])
        balance_before = data["accounts"][0]["available_balance"]
        body = {"message": "确认转账", "request_id": uuid.uuid4().hex,
                "revision": data["revision"], "confirmation_token": nonce}
        status, data, _ = self.request("POST", "/api/message", body, cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["accounts"][0]["available_balance"], balance_before)

    def test_authentication_action_requires_the_code(self):
        token, state = self.new_session()
        code = state["auth"]["code"]
        # downgrade first so that the enhanced level must be earned again
        body = {"message": "切换到IM", "request_id": uuid.uuid4().hex, "revision": state["revision"]}
        status, data, _ = self.request("POST", "/api/message", body, cookie=token)
        self.assertEqual(status, 200)
        bad = {"action": "authenticate", "factor": "000000", "request_id": uuid.uuid4().hex,
               "revision": data["revision"]}
        status, _, _ = self.request("POST", "/api/message", bad, cookie=token)
        self.assertEqual(status, 400)
        good = {"action": "authenticate", "factor": code, "request_id": uuid.uuid4().hex,
                "revision": data["revision"]}
        status, data, _ = self.request("POST", "/api/message", good, cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["auth"]["channel"], "app")


if __name__ == "__main__":
    unittest.main()
