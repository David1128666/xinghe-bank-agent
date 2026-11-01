from __future__ import annotations

from decimal import Decimal
from http.client import HTTPConnection
import json
import tempfile
import threading
import unittest
import uuid

from banking_agent.web_app import LocalBankServer
from banking_agent.audit import AuditError


class RecipientIdentityTests(unittest.TestCase):
    """Exercise first-recipient identity handling through the local HTTP API."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = LocalBankServer(audit_dir=self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.state = self.refresh()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Cookie": self.cookie, "Content-Type": "application/json",
                   "Origin": self.server.origin,
                   "X-CSRF-Token": getattr(self, "state", {}).get("csrf", "")}
        connection.request(method, "/api/state" if method == "GET" else "/api/message",
                           body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                           headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        status = response.status
        connection.close()
        return status, payload

    def refresh(self):
        status, state = self.request("GET")
        self.assertEqual(status, 200, state)
        self.state = state
        return state

    def body(self, action="message", **fields):
        return {"action": action, "request_id": str(uuid.uuid4()),
                "revision": self.state["revision"],
                "confirmation_token": self.state.get("confirmation_token"), **fields}

    def post(self, body):
        status, state = self.request("POST", body)
        self.assertEqual(status, 200, state)
        self.state = state
        return state

    def send(self, message):
        return self.post(self.body(message=message))

    def confirmation(self, name=None, last4="1230"):
        pending = self.state["pending_recipient"]
        self.assertIsNotNone(pending)
        return self.body("confirm_recipient", recipient_token=pending["token"],
                         payload={"name": name or pending["name"], "account_last4": last4})

    def cancel(self):
        return self.post(self.body("cancel_recipient",
                                   recipient_token=self.state["pending_recipient"]["token"]))

    def balance(self):
        checking = next(row for row in self.state["accounts"] if row["account_last4"] == "1234")
        return checking["available_balance"]

    def contacts(self, name):
        return [row for row in self.state["recipients"] if row["name"] == name]

    def test_first_identity_confirmation_saves_contact_and_next_transfer_is_direct(self):
        self.send("给张三转账2888元")
        pending = self.state["pending_recipient"]
        self.assertEqual(pending["name"], "张三")
        self.assertEqual(Decimal(pending["amount"]), Decimal("2888"))
        self.assertEqual(pending["currency"], "CNY")
        self.assertEqual(pending["payer_account_last4"], "1234")
        self.assertIsNone(self.state["task"])
        self.assertEqual(self.balance(), "50000.00")
        self.assertEqual(self.contacts("张三"), [])
        self.post(self.confirmation(last4="7788"))
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.state["task"]["preview"]["payee_name"], "张三")
        self.assertEqual(self.state["task"]["preview"]["payee_account_last4"], "7788")
        self.assertEqual(self.balance(), "47112.00")
        contact, = self.contacts("张三")
        self.assertEqual(contact["account_last4"], "7788")
        self.assertEqual(contact["bank_name"], "星河模拟银行")
        self.assertTrue(contact["identity_confirmed"])
        self.send("给张三转账25元")
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.balance(), "47087.00")
        self.assertEqual(len(self.contacts("张三")), 1)

    def test_identity_name_can_be_corrected_before_contact_is_saved(self):
        self.send("给张三转账2888元")
        self.post(self.confirmation(name="李四", last4="5566"))
        self.assertEqual(self.contacts("张三"), [])
        self.assertEqual(len(self.contacts("李四")), 1)
        self.assertEqual(self.state["task"]["preview"]["payee_name"], "李四")
        self.assertEqual(self.balance(), "47112.00")
        self.send("给李四转账12元")
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.balance(), "47100.00")

    def test_new_identity_at_3000_still_requires_separate_transfer_confirmation(self):
        self.send("给张三转账3000元")
        self.post(self.confirmation(last4="7788"))
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.balance(), "50000.00")
        self.assertEqual(len(self.contacts("张三")), 1)
        self.send("确认转账")
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.balance(), "47000.00")

    def test_pending_survives_refresh_and_confirm_request_replay_does_not_pay_twice(self):
        self.send("给张三转账2888元")
        pending = self.state["pending_recipient"]
        self.refresh()
        self.assertEqual(self.state["pending_recipient"], pending)
        body = self.confirmation(last4="7788")
        self.post(body)
        reference = self.state["task"]["transaction_reference"]
        self.post(body)
        self.assertEqual(self.balance(), "47112.00")
        self.assertEqual(self.state["task"]["transaction_reference"], reference)
        self.assertEqual(len(self.contacts("张三")), 1)
        altered = {**body, "payload": {"name": "李四", "account_last4": "5566"}}
        self.assertEqual(self.request("POST", altered)[0], 409)
        self.refresh()
        self.assertEqual(self.balance(), "47112.00")
        self.assertEqual(self.contacts("李四"), [])

    def test_cancelling_identity_does_not_save_or_pay(self):
        self.send("给张三转账2888元")
        self.cancel()
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.contacts("张三"), [])
        self.assertEqual(self.balance(), "50000.00")
        self.send("给张三转账2888元")
        self.assertEqual(self.state["pending_recipient"]["name"], "张三")
        self.assertEqual(self.balance(), "50000.00")

    def test_invalid_identity_token_revision_or_fields_have_no_side_effects(self):
        self.send("给张三转账2888元")
        valid = self.confirmation(last4="7788")
        variants = [
            {**valid, "recipient_token": "outdated-token"},
            {**valid, "revision": valid["revision"] - 1},
            {**valid, "payload": {"name": "", "account_last4": "7788"}},
            {**valid, "payload": {"name": "not-a-name", "account_last4": "7788"}},
            {**valid, "payload": {"name": "张三", "account_last4": "123"}},
            {**valid, "payload": {"name": "张三", "account_last4": "abcd"}},
        ]
        for body in variants:
            with self.subTest(body=body):
                body["request_id"] = str(uuid.uuid4())
                status, result = self.request("POST", body)
                self.assertIn(status, (400, 409), result)
                self.refresh()
                self.assertEqual(self.balance(), "50000.00")
                self.assertEqual(self.contacts("张三"), [])
                self.assertEqual(self.state["pending_recipient"]["token"], valid["recipient_token"])

    def test_exact_new_name_does_not_match_known_name_substring_and_duplicates_stay_explicit(self):
        self.send("给张小明转账2888元")
        self.assertEqual(self.state["pending_recipient"]["name"], "张小明")
        self.post(self.confirmation(last4="5566"))
        self.assertEqual(self.state["task"]["preview"]["payee_name"], "张小明")
        self.assertEqual(self.balance(), "47112.00")
        self.send("给张伟转账50元")
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.state["task"]["state"], "AWAITING_INFORMATION")
        self.assertEqual(len(self.state["task"]["recipient_choices"]), 2)
        self.assertEqual(self.balance(), "47112.00")
        self.send("2")
        self.assertEqual(self.state["task"]["preview"]["payee_account_last4"], "3344")
        self.assertEqual(self.balance(), "47062.00")

    def test_supplying_missing_name_restores_the_original_amount(self):
        self.send("转账2888元")
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.state["task"]["state"], "AWAITING_INFORMATION")
        self.assertEqual(self.balance(), "50000.00")
        self.send("张三")
        self.assertEqual(self.state["pending_recipient"]["name"], "张三")
        self.assertEqual(Decimal(self.state["pending_recipient"]["amount"]), Decimal("2888"))
        self.post(self.confirmation(last4="7788"))
        self.assertEqual(self.state["task"]["state"], "SUCCESS")
        self.assertEqual(self.balance(), "47112.00")

    def test_unknown_phone_pronoun_and_multiple_names_do_not_create_identity_candidates(self):
        original = self.state["recipients"]
        for message in ("给13900009999转账2888元", "给他转账2888元", "给张三和李四转账2888元", "给张三或李四转账2888元", "给张三与李四转账2888元"):
            with self.subTest(message=message):
                self.post(self.body("reset"))
                self.send(message)
                self.assertIsNone(self.state["pending_recipient"])
                self.assertEqual(self.balance(), "50000.00")
                self.assertEqual(self.state["recipients"], original)

    def test_identity_confirmation_does_not_bypass_risk_or_leak_contacts_between_sessions(self):
        self.send("从活期给张三转账2888元用途安全账户")
        self.post(self.confirmation(last4="7788"))
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["task"]["risk_level"], "R4")
        self.assertEqual(self.balance(), "50000.00")
        self.send("确认转账")
        self.assertEqual(self.state["task"]["state"], "UNDER_REVIEW")
        self.assertEqual(self.balance(), "50000.00")
        cookie = self.cookie
        self.cookie = ""
        self.refresh()
        self.assertEqual(self.contacts("张三"), [])
        self.send("给张三转账25元")
        self.assertIsNotNone(self.state["pending_recipient"])
        self.cookie = cookie
        self.refresh()
        self.assertEqual(len(self.contacts("张三")), 1)

    def test_other_recipient_prefixes_never_use_a_known_name_substring(self):
        for prefix in ("向", "为", "汇给", "转给"):
            with self.subTest(prefix=prefix):
                self.post(self.body("reset"))
                self.send(f"{prefix}张小明转账2888元")
                self.assertEqual(self.state["pending_recipient"]["name"], "张小明")
                self.assertEqual(self.balance(), "50000.00")

    def test_name_mentioned_only_in_purpose_does_not_select_a_payee(self):
        self.send("转账25元用途给小明买早餐")
        self.assertIsNone(self.state["pending_recipient"])
        self.assertEqual(self.state["task"]["state"], "AWAITING_INFORMATION")
        self.assertEqual(self.balance(), "50000.00")

    def test_recurring_transfer_keeps_the_name_separate_from_frequency(self):
        self.send("每月定时转账2026-11-10从活期转账300元给张三每月执行3次用途午餐")
        self.assertEqual(self.state["pending_recipient"]["name"], "张三")
        self.post(self.confirmation(last4="7788"))
        self.assertEqual(self.state["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(self.state["task"]["preview"]["payee_name"], "张三")
        self.assertEqual(self.balance(), "50000.00")

    def test_same_full_name_choices_exclude_longer_names(self):
        self.send("给张伟明转账25元")
        self.post(self.confirmation(last4="7788"))
        self.send("给张伟转账25元")
        choices = self.state["task"]["recipient_choices"]
        self.assertEqual(len(choices), 2)
        self.assertTrue(all(row["name"] == "张伟" for row in choices))
        self.assertEqual(self.balance(), "49975.00")

    def test_identity_audit_failure_prevents_contact_save_and_payment(self):
        self.send("给张三转账2888元")
        session = next(iter(self.server.sessions.values()))
        original_append = session.agent.audit.append
        def fail_identity_record(event):
            if event.get("action") == "recipient_identity_confirmed":
                raise AuditError("test identity audit failure")
            return original_append(event)
        session.agent.audit.append = fail_identity_record
        status, _ = self.request("POST", self.confirmation(last4="7788"))
        self.assertEqual(status, 500)
        self.refresh()
        self.assertEqual(self.balance(), "50000.00")
        self.assertEqual(self.contacts("张三"), [])
        self.assertIsNotNone(self.state["pending_recipient"])


if __name__ == "__main__":
    unittest.main()
