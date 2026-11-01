from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from banking_agent.agent import BankingAgent
from banking_agent.audit import AuditError, AuditLog
from banking_agent.config import AgentConfig
from banking_agent.models import (
    AuthLevel,
    AuthResult,
    ExecutionResult,
    ExecutionState,
    Operation,
    OperationPreview,
    OperationRequest,
    PreparedOperation,
    RecipientChoice,
    ResolutionState,
    RiskAssessment,
    RiskLevel,
    TaskState,
    TransferResolution,
)
from banking_agent.tools import ToolRegistry, ToolTimeout
from banking_agent.privacy import redact_tool_output


def complete_config(**overrides):
    values = {
        "bank_name": "Example Bank",
        "jurisdiction": "Example jurisdiction",
        "default_currency": "XXX",
        "timezone": "UTC",
        "low_risk_limit": "100",
        "high_risk_limit": "10000",
        "daily_transfer_limit": "20000",
        "investment_confirm_limit": "500",
        "confirmation_ttl": 120,
        "auth_methods": ["bank_app"],
        "manual_review_channel": "configured-review-service",
        "support_channel": "configured-support",
        "available_tools": [
            "authentication", "risk_assessment", "preview", "execution",
            "status_query", "manual_review", "transfer_resolver", "account_query",
            "public_info",
        ],
        "policy_version": "test-policy-v1",
    }
    values.update(overrides)
    return AgentConfig.from_mapping(values)


class FakeAuth:
    def __init__(self):
        self.allowed = True

    def authenticate(self, user_id, session_id, required_level):
        return AuthResult(self.allowed, AuthLevel.ENHANCED if self.allowed else AuthLevel.NONE)


class FakeRisk:
    def __init__(self, level=RiskLevel.R1):
        self.level = level

    def assess(self, request, prepared):
        return RiskAssessment(self.level, rule_ids=("test-rule",))


class FakeResolver:
    def __init__(self, ambiguous=False):
        self.ambiguous = ambiguous

    def resolve(self, request, selected_recipient_ref=None):
        if self.ambiguous and selected_recipient_ref is None:
            return TransferResolution(
                ResolutionState.AMBIGUOUS,
                choices=(
                    RecipientChoice("Recipient A", "1111", "bank-recipient-a"),
                    RecipientChoice("Recipient B", "2222", "bank-recipient-b"),
                ),
            )
        return TransferResolution(
            ResolutionState.MATCHED,
            PreparedOperation("bank-resolved-recipient-ref"),
        )


class FakePreview:
    def __init__(self, fee="0"):
        self.fee = fee
        self.calls = 0

    def preview(self, request, prepared):
        self.calls += 1
        return OperationPreview(
            summary="Transfer preview from trusted bank adapter",
            amount=str(request.payload["amount"]),
            currency=str(request.payload.get("currency", "XXX")),
            payer_account_last4="1234",
            payee_name="Recipient",
            payee_account_last4="5678",
            fee=self.fee,
            reversible=False,
        )


class FakeExecution:
    def __init__(self, result=None, timeout=False):
        self.calls = 0
        self.last_key = None
        self.timeout = timeout
        self.result = result or ExecutionResult(ExecutionState.SUCCESS, "txn-test")

    def execute(self, request, prepared, preview, idempotency_key):
        self.calls += 1
        self.last_key = idempotency_key
        if self.timeout:
            raise ToolTimeout()
        return self.result


class FakeStatus:
    def __init__(self, result=None):
        self.result = result or ExecutionResult(ExecutionState.SUCCESS, "txn-test")
        self.keys = []

    def lookup(self, idempotency_key):
        self.keys.append(idempotency_key)
        return self.result


class FakeReview:
    def __init__(self):
        self.calls = 0

    def submit(self, task_id, request, prepared, preview, assessment):
        self.calls += 1
        return type("Review", (), {"accepted": True, "review_reference": "review-1", "error_code": None})()


class FailingAudit(AuditLog):
    def append(self, event):
        if event.get("tool_name") == "execution":
            raise AuditError("simulated audit outage")
        return super().append(event)


class BankingAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit = AuditLog(Path(self.temp.name) / "audit.jsonl", b"test-only-hash-key")
        self.preview = FakePreview()
        self.execution = FakeExecution()
        self.status = FakeStatus()
        self.review = FakeReview()
        self.auth = FakeAuth()
        self.registry = ToolRegistry(
            authentication=self.auth,
            risk_assessment=FakeRisk(),
            transfer_resolver=FakeResolver(),
            preview=self.preview,
            execution=self.execution,
            status_query=self.status,
            manual_review=self.review,
        )
        self.agent = BankingAgent(complete_config(), self.registry, self.audit)
        self.request = OperationRequest(
            operation=Operation.TRANSFER,
            user_id="user-1",
            session_id="session-1",
            summary="Transfer to selected recipient",
            payload={
                "amount": "25",
                "currency": "XXX",
                "recipient_query": "Recipient",
                "source_account_ref": "primary-account-alias",
                "execution_time": "now",
                "purpose": "personal transfer",
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_transfer_requires_bound_confirmation_before_execution(self):
        task = self.agent.submit(self.request)
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.execution.calls, 0)
        self.agent.confirm(task.task_id, "user-1", "session-1", "随便")
        self.assertEqual(self.execution.calls, 0)
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.SUCCESS)
        self.assertEqual(self.execution.calls, 1)
        self.assertEqual(self.status.keys, [self.execution.last_key])

    def test_unconfigured_agent_fails_closed(self):
        agent = BankingAgent(AgentConfig(), ToolRegistry(), self.audit)
        task = agent.submit(self.request)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error_code, "agent_not_configured")

    def test_high_risk_requires_confirmation_then_review_and_never_executes(self):
        self.registry.risk_assessment = FakeRisk(RiskLevel.R4)
        task = self.agent.submit(self.request)
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.UNDER_REVIEW)
        self.assertEqual(self.review.calls, 1)
        self.assertEqual(self.execution.calls, 0)

    def test_changed_preview_invalidates_confirmation(self):
        task = self.agent.submit(self.request)
        self.preview.fee = "5"
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(task.error_code, "preview_changed_reconfirm_required")
        self.assertEqual(self.execution.calls, 0)
        self.assertEqual(task.preview.fee, "5")

    def test_timeout_queries_idempotency_key_without_retry(self):
        self.execution.timeout = True
        task = self.agent.submit(self.request)
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.SUCCESS)
        self.assertEqual(self.execution.calls, 1)
        self.assertEqual(self.status.keys, [self.execution.last_key])

    def test_sensitive_input_is_rejected_and_value_not_written_to_audit(self):
        request = OperationRequest(
            operation=Operation.TRANSFER,
            user_id="user-1",
            session_id="session-1",
            summary="bad input",
            payload={"password": "very-secret-value"},
        )
        task = self.agent.submit(request)
        self.assertEqual(task.error_code, "sensitive_input_field_rejected")
        records = self.audit.path.read_text(encoding="utf-8")
        self.assertNotIn("very-secret-value", records)

    def test_audit_chain_detects_tampering(self):
        self.audit.append({"action": "test", "user_id_hash": "hash"})
        contents = self.audit.path.read_text(encoding="utf-8").replace('"action":"test"', '"action":"changed"')
        self.audit.path.write_text(contents, encoding="utf-8")
        with self.assertRaises(AuditError):
            AuditLog(self.audit.path, b"test-only-hash-key")

    def test_audit_failure_before_execution_blocks_side_effect(self):
        audit = FailingAudit(Path(self.temp.name) / "failing-audit.jsonl", b"test-key")
        agent = BankingAgent(complete_config(), self.registry, audit)
        task = agent.submit(self.request)
        task = agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error_code, "audit_unavailable")
        self.assertEqual(self.execution.calls, 0)

    def test_ambiguous_recipient_waits_for_explicit_choice(self):
        self.registry.transfer_resolver = FakeResolver(ambiguous=True)
        task = self.agent.submit(self.request)
        self.assertEqual(task.state, TaskState.AWAITING_INFORMATION)
        self.assertEqual(len(task.public_dict()["recipient_choices"]), 2)
        task = self.agent.choose_recipient(task.task_id, "user-1", "session-1", 1)
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.execution.calls, 0)
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.SUCCESS)

    def test_transfer_missing_currency_or_execution_time_waits_for_information(self):
        payload = dict(self.request.payload)
        payload.pop("currency")
        payload.pop("execution_time")
        request = OperationRequest(
            operation=Operation.TRANSFER,
            user_id="user-1",
            session_id="session-1",
            summary="Transfer request",
            payload=payload,
        )
        task = self.agent.submit(request)
        self.assertEqual(task.state, TaskState.AWAITING_INFORMATION)
        self.assertEqual(task.public_dict()["missing_information"], ["currency", "execution_time"])

    def test_authentication_resume_reissues_confirmation_after_fresh_preview(self):
        task = self.agent.submit(self.request)
        self.auth.allowed = False
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.AWAITING_AUTHENTICATION)
        self.auth.allowed = True
        task = self.agent.resume_authentication(task.task_id, "user-1", "session-1")
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.execution.calls, 0)
        task = self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.state, TaskState.SUCCESS)

    def test_same_request_id_reuses_task_but_changed_payload_is_rejected(self):
        request = replace(self.request, request_id="client-request-1")
        task = self.agent.submit(request)
        retry = self.agent.submit(replace(request, channel="app"))
        self.assertEqual(retry.task_id, task.task_id)
        self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        retry = self.agent.submit(request)
        self.assertEqual(retry.state, TaskState.SUCCESS)
        self.assertEqual(self.execution.calls, 1)
        conflicting = self.agent.submit(replace(request, payload={**request.payload, "amount": "99"}))
        self.assertEqual(conflicting.error_code, "request_id_payload_conflict")
        self.assertEqual(self.execution.calls, 1)

    def test_expired_confirmation_never_calls_execution(self):
        task = self.agent.submit(self.request)
        task.confirmation_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.agent.confirm(task.task_id, "user-1", "session-1", "确认转账")
        self.assertEqual(task.error_code, "confirmation_expired")
        self.assertEqual(self.execution.calls, 0)

    def test_privacy_preserves_dates_while_masking_full_account_numbers(self):
        safe = redact_tool_output({"date": "2026-10-21", "timestamp": "2026-10-21T09:00:00+08:00", "account_number": "1234567890123456", "api_key": "secret-value"})
        self.assertEqual(safe["date"], "2026-10-21")
        self.assertEqual(safe["timestamp"], "2026-10-21T09:00:00+08:00")
        self.assertEqual(safe["account_number"], "••••3456")
        self.assertNotIn("api_key", safe)


if __name__ == "__main__":
    unittest.main()
