from __future__ import annotations

import hashlib
import copy
import json
import inspect
import re
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .audit import AuditError, AuditLog
from .config import AgentConfig
from .models import (
    AgentTask,
    AuthLevel,
    ExecutionState,
    Operation,
    OperationPreview,
    OperationRequest,
    PreparedOperation,
    QueryResult,
    ResolutionState,
    RiskAssessment,
    RiskLevel,
    TaskState,
    TransferResolution,
    amount_as_decimal,
)
from .policy import PolicyError, effective_risk, minimum_risk, required_tools
from .privacy import forbidden_input_keys, redact_tool_output
from .tools import ToolRegistry, ToolTimeout


_CONFIRM_PHRASES = {
    Operation.DISPUTE: "确认提交争议",
    Operation.TRANSFER: "确认转账",
    Operation.AA_REQUEST: "确认AA操作",
    Operation.SCHEDULE_MANAGEMENT: "确认计划操作",
    Operation.CATEGORY_CORRECTION: "确认修改分类",
    Operation.BUDGET: "确认预算标签",
    Operation.REMINDER: "确认创建提醒",
    Operation.INVESTMENT: "确认投资操作",
    Operation.CARD_MANAGEMENT: "确认卡片操作",
    Operation.SUBSCRIPTION: "确认订阅操作",
    Operation.CROSS_SCENARIO: "确认本次计划",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CENT = Decimal("0.01")
_MONEY_FIELDS = {
    "amount", "new_limit", "investable_amount", "reserve_amount",
    "flower_budget", "cake_budget", "max_amount", "purchase_total",
}


class BankingAgent:
    """Policy-gated orchestration core. It performs no banking action without adapters."""

    def __init__(
        self,
        config: AgentConfig,
        tools: ToolRegistry,
        audit: AuditLog,
    ):
        self.config = config
        self.tools = tools
        self.audit = audit
        self._tasks: dict[str, AgentTask] = {}
        self._request_tasks: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._lock = threading.RLock()

    def submit(self, request: OperationRequest) -> AgentTask:
        request = replace(request, payload=self._normalize_amounts(copy.deepcopy(dict(request.payload))))
        fingerprint = hashlib.sha256(json.dumps({"operation": request.operation.value, "summary": request.summary, "payload": dict(request.payload)}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        request_key = (request.user_id, request.session_id, request.request_id)
        task_id = str(uuid.uuid4())
        task = AgentTask(
            task_id=task_id,
            request=request,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            if request.request_id and request_key in self._request_tasks:
                existing_id, existing_fingerprint = self._request_tasks[request_key]
                if fingerprint == existing_fingerprint:
                    existing = self._tasks[existing_id]
                    if request.channel != existing.request.channel:
                        return self.switch_channel(existing_id, request.user_id, request.session_id, request.channel)
                    return existing
                self._tasks[task_id] = task
                return self._fail(task, "request_id_payload_conflict")
            self._tasks[task_id] = task
            if request.request_id:
                self._request_tasks[request_key] = (task_id, fingerprint)

        try:
            self._audit(task, "intent_received")
            task.state = TaskState.DRAFT
            if not request.user_id or not request.session_id or not request.summary:
                return self._fail(task, "required_request_fields_missing")
            if not _SAFE_ID.match(request.session_id):
                return self._fail(task, "invalid_session_reference")
            forbidden = forbidden_input_keys(dict(request.payload))
            if forbidden:
                return self._fail(task, "sensitive_input_field_rejected")

            missing = self.config.missing_fields_for(request.operation)
            missing.extend(self._missing_tools(request.operation))
            if missing:
                return self._fail(task, "agent_not_configured")

            task.internal_plan = self._build_plan(task)
            self._audit(task, "plan_created")
            if request.operation is Operation.PUBLIC_INFO:
                return self._answer_public_info(task)
            return self._prepare_account_operation(task)
        except AuditError:
            task.state = TaskState.FAILED
            task.error_code = "audit_unavailable"
            return task
        except PolicyError as exc:
            return self._fail_with_reason(task, "policy_rejected", str(exc))
        except ValueError as exc:
            return self._fail_with_reason(task, "tool_validation_failed", str(exc))
        except Exception:
            return self._fail(task, "operation_not_completed")

    @staticmethod
    def _build_plan(task: AgentTask) -> dict[str, Any]:
        operation = task.request.operation
        if operation.is_public:
            tools = ["public_info"]
        elif operation.is_mutating:
            tools = ["authentication"]
            if operation in {Operation.TRANSFER, Operation.AA_REQUEST}:
                tools.append("transfer_resolver")
            tools += ["risk_assessment", "preview", "user_confirmation", "execution", "status_query"]
        else:
            tools = ["authentication", "risk_assessment", "account_query"]
        steps = []
        previous: list[str] = []
        for index, tool in enumerate(tools, 1):
            step_id = f"step-{index}"
            steps.append({
                "step_id": step_id, "tool": tool, "depends_on": previous,
                "risk_level": minimum_risk(task.request).name,
                "authentication_required": not operation.is_public,
                "confirmation_required": operation.is_mutating,
                "manual_review_required": False,
                "recovery": "pause_and_query_existing_status" if tool in {"execution", "status_query"} else "pause_and_request_information",
            })
            previous = [step_id]
        return {"plan_id": task.task_id, "goal": operation.value, "steps": steps}

    def _prepare_account_operation(self, task: AgentTask) -> AgentTask:
        request = task.request
        required_fields = self._missing_information(request)
        if required_fields:
            task.missing_information = tuple(required_fields)
            task.state = TaskState.AWAITING_INFORMATION
            task.error_code = "required_information_missing"
            self._audit(task, "information_requested")
            return task

        assert self.tools.authentication is not None
        required_auth = AuthLevel.ENHANCED if request.operation.is_mutating else AuthLevel.STANDARD
        auth = self._authenticate(request.user_id, request.session_id, required_auth, request.channel)
        self._audit(
            task,
            "auth_checked",
            authentication_level=auth.level.name.lower(),
        )
        if not auth.authenticated or auth.level < required_auth:
            task.state = TaskState.AWAITING_AUTHENTICATION
            task.error_code = "authentication_required"
            task.auth_resume_stage = "begin"
            return task

        return self._resolve_and_assess(task)

    def _resolve_and_assess(
        self,
        task: AgentTask,
        selected_recipient_ref: str | None = None,
    ) -> AgentTask:
        request = task.request
        if request.operation is Operation.TRANSFER or (
            request.operation is Operation.AA_REQUEST
            and request.payload.get("action") == "send_request"
        ):
            assert self.tools.transfer_resolver is not None
            self._audit(task, "tool_called", tool_name="transfer_resolver")
            resolution = self.tools.transfer_resolver.resolve(request, selected_recipient_ref)
            if resolution.state is ResolutionState.AMBIGUOUS:
                task.choices = resolution.choices
                task.state = TaskState.AWAITING_INFORMATION
                task.error_code = "recipient_selection_required"
                self._audit(task, "information_requested")
                return task
            if resolution.state is ResolutionState.NOT_FOUND or resolution.prepared is None:
                task.state = TaskState.AWAITING_INFORMATION
                task.error_code = "recipient_not_resolved"
                task.missing_information = ("recipient",)
                self._audit(task, "information_requested")
                return task
            task.prepared = resolution.prepared

        return self._assess_and_continue(task)

    def _assess_and_continue(self, task: AgentTask) -> AgentTask:
        request = task.request
        assert self.tools.risk_assessment is not None
        self._audit(task, "tool_called", tool_name="risk_assessment")
        raw_assessment = self.tools.risk_assessment.assess(request, task.prepared)
        task.risk = effective_risk(request, raw_assessment, self.config)
        for step in task.internal_plan.get("steps", []):
            step["risk_level"] = task.risk.level.name
            step["manual_review_required"] = task.risk.manual_review_required
        self._audit(task, "risk_assessed", risk_level=task.risk.level.name)

        if not request.operation.is_mutating:
            if task.risk.level >= RiskLevel.R3:
                auth = self._authenticate(request.user_id, request.session_id, AuthLevel.ENHANCED, request.channel)
                self._audit(task, "auth_checked", authentication_level=auth.level.name.lower())
                if not auth.authenticated or auth.level < AuthLevel.ENHANCED:
                    task.state = TaskState.AWAITING_AUTHENTICATION
                    task.error_code = "enhanced_authentication_required"
                    task.auth_resume_stage = "sensitive_read"
                    return task
            if task.risk.level is RiskLevel.R4:
                return self._escalate_read(task)
            assert self.tools.account_query is not None
            self._audit(task, "tool_called", tool_name="account_query")
            query = self.tools.account_query.query(request)
            self._audit(task, "status_checked")
            task.result = self._safe_query_result(query)
            task.state = TaskState.SUCCESS
            self._audit(task, "completed", result="success")
            return task

        return self._create_preview(task)

    def choose_recipient(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
        choice_index: int,
    ) -> AgentTask:
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if (
                task.state is not TaskState.AWAITING_INFORMATION
                or task.request.operation not in {Operation.TRANSFER, Operation.AA_REQUEST}
                or choice_index < 0
                or choice_index >= len(task.choices)
            ):
                return task
            task.selected_choice_index = choice_index
            task.auth_resume_stage = "recipient_selection"
            assert self.tools.authentication is not None
            auth = self._authenticate(user_id, session_id, AuthLevel.ENHANCED, task.request.channel)
            self._audit(task, "auth_checked", authentication_level=auth.level.name.lower())
            if not auth.authenticated or auth.level < AuthLevel.ENHANCED:
                task.state = TaskState.AWAITING_AUTHENTICATION
                task.error_code = "enhanced_authentication_required"
                task.auth_resume_stage = "recipient_selection"
                return task
            choice = task.choices[choice_index]
            task.choices = ()
            task.missing_information = ()
            return self._resolve_and_assess(task, choice.opaque_recipient_ref)

    def provide_information(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
        fields: dict[str, Any],
    ) -> AgentTask:
        """Add user-supplied fields to a paused draft, then revalidate it."""
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state is not TaskState.AWAITING_INFORMATION:
                return task
            if forbidden_input_keys(fields):
                return self._fail(task, "sensitive_input_field_rejected")
            self._audit(task, "information_received")
            merged = dict(task.request.payload)
            merged.update(fields)
            task.request = replace(task.request, payload=self._normalize_amounts(merged))
            task.missing_information = ()
            task.error_code = None
            task.state = TaskState.DRAFT
            return self._prepare_account_operation(task)

    def resume_authentication(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
    ) -> AgentTask:
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state is not TaskState.AWAITING_AUTHENTICATION or not task.auth_resume_stage:
                return task
            assert self.tools.authentication is not None
            required_auth = (
                AuthLevel.ENHANCED if task.request.operation.is_mutating or task.auth_resume_stage == "sensitive_read" else AuthLevel.STANDARD
            )
            auth = self._authenticate(user_id, session_id, required_auth, task.request.channel)
            self._audit(task, "auth_checked", authentication_level=auth.level.name.lower())
            if not auth.authenticated or auth.level < required_auth:
                return task

            stage = task.auth_resume_stage
            task.auth_resume_stage = None
            task.error_code = None
            if stage == "begin":
                return self._prepare_account_operation(task)
            if stage == "sensitive_read":
                return self._assess_and_continue(task)
            if stage == "recipient_selection":
                if task.selected_choice_index is None or task.selected_choice_index >= len(task.choices):
                    return self._fail(task, "recipient_selection_expired")
                choice = task.choices[task.selected_choice_index]
                task.choices = ()
                return self._resolve_and_assess(task, choice.opaque_recipient_ref)
            if stage == "confirmation":
                assert self.tools.risk_assessment is not None and self.tools.preview is not None
                self._audit(task, "tool_called", tool_name="risk_assessment")
                fresh_risk = effective_risk(
                    task.request,
                    self.tools.risk_assessment.assess(task.request, task.prepared),
                    self.config,
                )
                self._audit(task, "risk_assessed", risk_level=fresh_risk.level.name)
                self._audit(task, "tool_called", tool_name="preview")
                fresh_preview = self.tools.preview.preview(task.request, task.prepared)
                task.risk = fresh_risk
                task.preview = fresh_preview
                task.confirmation_phrase = _CONFIRM_PHRASES.get(task.request.operation)
                task.confirmation_expires_at = self._confirmation_expiry()
                task.confirmed_preview_digest = self._preview_digest(fresh_preview, fresh_risk)
                task.state = TaskState.AWAITING_CONFIRMATION
                task.error_code = "reauthenticated_reconfirm_required"
                self._audit(task, "preview_created")
                self._audit(task, "confirmation_requested", confirmation={
                    "required": True,
                    "received": False,
                    "summary_hash": task.confirmed_preview_digest,
                    "expires_at": task.confirmation_expires_at.isoformat(),
                })
                return task
            return self._fail(task, "invalid_auth_resume_stage")

    def switch_channel(
        self, task_id: str, user_id: str, session_id: str, channel: str
    ) -> AgentTask:
        """Rebind a pending task to a new channel and invalidate any old confirmation."""
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state not in {
                TaskState.AWAITING_INFORMATION,
                TaskState.AWAITING_AUTHENTICATION,
                TaskState.AWAITING_CONFIRMATION,
            }:
                return task
            if channel not in {"app", "im", "other"}:
                return task
            if channel == task.request.channel:
                return task
            task.request = replace(task.request, channel=channel)
            task.confirmation_phrase = None
            task.confirmed_preview_digest = None
            task.confirmation_expires_at = None
            task.state = TaskState.AWAITING_AUTHENTICATION
            task.error_code = "channel_change_reauthentication_required"
            task.auth_resume_stage = "confirmation" if task.preview is not None else "begin"
            self._audit(task, "channel_changed", escalation_reason="fresh_auth_and_confirmation_required")
            return self.resume_authentication(task_id, user_id, session_id)

    def _create_preview(self, task: AgentTask) -> AgentTask:
        assert self.tools.preview is not None
        self._audit(task, "tool_called", tool_name="preview")
        preview = self.tools.preview.preview(task.request, task.prepared)
        task.preview = preview
        task.idempotency_key = str(uuid.uuid4())
        task.confirmation_phrase = _CONFIRM_PHRASES[task.request.operation]
        task.confirmation_expires_at = self._confirmation_expiry()
        task.confirmed_preview_digest = self._preview_digest(preview, task.risk)
        if self._direct_transfer_authorized(task):
            direct_result = self._try_direct_transfer(task)
            if direct_result is not None:
                return direct_result
        scoped_authorizer = getattr(self.tools.execution, "is_pre_authorized", None)
        # Only an explicitly confirmed plan range may skip the confirmation gate.
        if task.request.operation is Operation.CROSS_SCENARIO and callable(scoped_authorizer):
            try:
                in_scope = bool(scoped_authorizer(task.request, preview))
            except Exception:
                in_scope = False
            if in_scope and task.risk and task.risk.level is not RiskLevel.R4 and not task.risk.manual_review_required:
                self._audit(task, "scope_authorization_used", confirmation={
                    "required": True, "received": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "summary_hash": task.confirmed_preview_digest,
                    "expires_at": task.confirmation_expires_at.isoformat(),
                })
                return self._execute_once(task)
        task.state = TaskState.AWAITING_CONFIRMATION
        self._audit(task, "preview_created")
        self._audit(task, "confirmation_requested", confirmation={
            "required": True,
            "received": False,
            "summary_hash": task.confirmed_preview_digest,
            "expires_at": task.confirmation_expires_at.isoformat(),
        })
        return task

    def _direct_transfer_authorized(self, task: AgentTask) -> bool:
        """Opt-in policy hook. The standard Agent always requires confirmation."""
        return False

    def _try_direct_transfer(self, task: AgentTask) -> AgentTask | None:
        """Apply an explicit demo policy without claiming a user clicked confirm."""
        assert self.tools.risk_assessment is not None and self.tools.preview is not None
        auth = self._authenticate(task.request.user_id, task.request.session_id,
                                  AuthLevel.ENHANCED, task.request.channel)
        self._audit(task, "auth_checked", authentication_level=auth.level.name.lower())
        if not auth.authenticated or auth.level < AuthLevel.ENHANCED:
            task.state = TaskState.AWAITING_AUTHENTICATION
            task.error_code = "enhanced_authentication_required"
            task.auth_resume_stage = "confirmation"
            return task
        self._audit(task, "tool_called", tool_name="risk_assessment")
        fresh_risk = effective_risk(task.request,
            self.tools.risk_assessment.assess(task.request, task.prepared), self.config)
        self._audit(task, "tool_called", tool_name="preview")
        fresh_preview = self.tools.preview.preview(task.request, task.prepared)
        fresh_digest = self._preview_digest(fresh_preview, fresh_risk)
        previous_digest = task.confirmed_preview_digest
        task.risk, task.preview = fresh_risk, fresh_preview
        task.confirmed_preview_digest = fresh_digest
        self._audit(task, "risk_assessed", risk_level=fresh_risk.level.name)
        if fresh_digest != previous_digest or not self._direct_transfer_authorized(task):
            task.error_code = "preview_changed_reconfirm_required"
            task.confirmation_expires_at = self._confirmation_expiry()
            return None
        task.internal_plan["confirmation_policy"] = "virtual_transfer_below_3000"
        for step in task.internal_plan.get("steps", []):
            step["confirmation_required"] = False
        task.confirmation_phrase = None
        task.confirmation_expires_at = None
        self._audit(task, "preview_created")
        self._audit(task, "configured_transfer_authorization_used", confirmation={
            "required": False, "received": False, "timestamp": None,
            "summary_hash": fresh_digest, "expires_at": None,
        })
        return self._execute_once(task)

    def confirm(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
        phrase: str,
        channel: str | None = None,
    ) -> AgentTask:
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state is not TaskState.AWAITING_CONFIRMATION:
                return task
            active_channel = channel or task.request.channel
            if active_channel != task.request.channel:
                task.request = replace(task.request, channel=active_channel)
                task.state = TaskState.AWAITING_AUTHENTICATION
                task.error_code = "channel_change_reauthentication_required"
                task.auth_resume_stage = "confirmation"
                task.confirmation_phrase = None
                self._audit(task, "channel_changed", escalation_reason="confirmation_invalidated")
                return self.resume_authentication(task_id, user_id, session_id)
            if not task.confirmation_expires_at or datetime.now(timezone.utc) >= task.confirmation_expires_at:
                return self._fail(task, "confirmation_expired")
            if phrase != task.confirmation_phrase:
                self._audit(task, "confirmation_rejected", error_code="confirmation_phrase_mismatch")
                return task

            assert self.tools.authentication is not None
            auth = self._authenticate(user_id, session_id, AuthLevel.ENHANCED, task.request.channel)
            self._audit(
                task,
                "auth_checked",
                authentication_level=auth.level.name.lower(),
            )
            if not auth.authenticated or auth.level < AuthLevel.ENHANCED:
                task.state = TaskState.AWAITING_AUTHENTICATION
                task.error_code = "enhanced_authentication_required"
                task.auth_resume_stage = "confirmation"
                return task

            assert self.tools.risk_assessment is not None
            raw_risk = self.tools.risk_assessment.assess(task.request, task.prepared)
            fresh_risk = effective_risk(task.request, raw_risk, self.config)
            assert self.tools.preview is not None
            fresh_preview = self.tools.preview.preview(task.request, task.prepared)
            fresh_digest = self._preview_digest(fresh_preview, fresh_risk)
            if fresh_digest != task.confirmed_preview_digest:
                task.risk = fresh_risk
                task.preview = fresh_preview
                task.confirmation_expires_at = self._confirmation_expiry()
                task.confirmed_preview_digest = fresh_digest
                task.state = TaskState.AWAITING_CONFIRMATION
                task.error_code = "preview_changed_reconfirm_required"
                self._audit(task, "preview_changed", risk_level=fresh_risk.level.name)
                self._audit(task, "confirmation_requested", confirmation={
                    "required": True,
                    "received": False,
                    "summary_hash": fresh_digest,
                    "expires_at": task.confirmation_expires_at.isoformat(),
                })
                return task

            task.risk = fresh_risk
            task.preview = fresh_preview
            self._audit(
                task,
                "confirmation_received",
                confirmation={
                    "required": True,
                    "received": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "summary_hash": fresh_digest,
                    "expires_at": task.confirmation_expires_at.isoformat(),
                },
            )

            if fresh_risk.level is RiskLevel.R4:
                return self._submit_for_review(task)
            return self._execute_once(task)

    def _execute_once(self, task: AgentTask) -> AgentTask:
        assert self.tools.execution is not None
        assert self.tools.status_query is not None
        assert task.preview is not None and task.idempotency_key is not None
        try:
            self._audit(
                task,
                "tool_called",
                tool_name="execution",
                idempotency_key=task.idempotency_key,
            )
        except AuditError:
            task.state = TaskState.FAILED
            task.error_code = "audit_unavailable"
            return task
        # Lock the task against another confirmation before crossing the side-effect boundary.
        task.state = TaskState.PROCESSING
        try:
            self.tools.execution.execute(
                task.request,
                task.prepared,
                task.preview,
                task.idempotency_key,
            )
        except ToolTimeout:
            # A timeout is ambiguous: query by the same key and never submit again.
            pass
        except Exception:
            # The adapter may have accepted the operation before failing locally.
            pass

        try:
            self._audit(
                task,
                "tool_called",
                tool_name="status_query",
                idempotency_key=task.idempotency_key,
            )
        except AuditError:
            task.state = TaskState.UNKNOWN
            task.error_code = "audit_unavailable_after_submission"
            return task
        try:
            final = self.tools.status_query.lookup(task.idempotency_key)
        except Exception:
            final = None
        self._audit(task, "status_checked")
        if final is None:
            task.state = TaskState.UNKNOWN
            task.error_code = "status_unknown_do_not_retry"
            self._audit(task, "failed", result="unknown", error_code=task.error_code)
            return task

        task.transaction_reference = final.transaction_reference
        task.error_code = final.error_code
        task.result = dict(final.details)
        if final.state is ExecutionState.SUCCESS:
            task.state = TaskState.SUCCESS
            self._audit(task, "completed", result="success")
        elif final.state is ExecutionState.SCHEDULED:
            task.state = TaskState.SCHEDULED
            task.result = dict(final.details)
            self._audit(task, "completed", result="scheduled")
        elif final.state is ExecutionState.PROCESSING:
            task.state = TaskState.PROCESSING
            task.result = dict(final.details)
            self._audit(task, "completed", result="processing")
        elif final.state is ExecutionState.PARTIAL:
            task.state = TaskState.PARTIAL_SUCCESS
            task.result = dict(final.details)
            self._audit(task, "completed", result="partial")
        elif final.state is ExecutionState.FAILED:
            task.state = TaskState.FAILED
            self._audit(task, "failed", result="failed", error_code=final.error_code)
        else:
            task.state = TaskState.UNKNOWN
            task.error_code = "status_unknown_do_not_retry"
            self._audit(task, "failed", result="unknown", error_code=task.error_code)
        return task

    def _submit_for_review(self, task: AgentTask) -> AgentTask:
        assert self.tools.manual_review is not None
        assert task.preview is not None and task.risk is not None
        self._audit(task, "tool_called", tool_name="manual_review")
        try:
            result = self.tools.manual_review.submit(
                task.task_id,
                task.request,
                task.prepared,
                task.preview,
                task.risk,
            )
        except Exception:
            task.state = TaskState.UNKNOWN
            task.error_code = "manual_review_status_unknown"
            return task
        if result.accepted:
            task.state = TaskState.UNDER_REVIEW
            task.transaction_reference = result.review_reference
            self._audit(task, "escalated", result="processing")
        else:
            task.state = TaskState.FAILED
            task.error_code = result.error_code or "manual_review_submission_failed"
            self._audit(task, "failed", result="failed", error_code=task.error_code)
        return task

    def _escalate_read(self, task: AgentTask) -> AgentTask:
        if self.tools.manual_review is None:
            return self._fail(task, "manual_review_unavailable")
        assert task.risk is not None
        empty_preview = OperationPreview(summary="账户查询已暂停，等待人工审核")
        self._audit(task, "tool_called", tool_name="manual_review")
        try:
            result = self.tools.manual_review.submit(
                task.task_id, task.request, None, empty_preview, task.risk
            )
        except Exception:
            task.state = TaskState.UNKNOWN
            task.error_code = "manual_review_status_unknown"
            return task
        task.state = TaskState.UNDER_REVIEW if result.accepted else TaskState.FAILED
        task.error_code = result.error_code
        task.transaction_reference = result.review_reference
        self._audit(
            task,
            "escalated" if result.accepted else "failed",
            result="processing" if result.accepted else "failed",
            error_code=result.error_code,
        )
        return task

    def _answer_public_info(self, task: AgentTask) -> AgentTask:
        assert self.tools.public_info is not None
        self._audit(task, "tool_called", tool_name="public_info")
        answer = self.tools.public_info.answer(
            task.request.summary, self.config.jurisdiction or ""
        )
        task.result = answer
        task.state = TaskState.SUCCESS
        self._audit(task, "completed", result="success", risk_level="R0")
        return task

    def get_task(self, task_id: str, user_id: str, session_id: str) -> AgentTask:
        with self._lock:
            return self._owned_task(task_id, user_id, session_id)

    def cancel(self, task_id: str, user_id: str, session_id: str) -> AgentTask:
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state in {
                TaskState.DRAFT,
                TaskState.AWAITING_INFORMATION,
                TaskState.AWAITING_AUTHENTICATION,
                TaskState.AWAITING_CONFIRMATION,
            }:
                task.state = TaskState.CANCELLED
                task.error_code = None
                self._audit(task, "cancelled", result="failed")
            elif task.state is TaskState.UNDER_REVIEW and self.tools.manual_review is not None:
                cancel_review = getattr(self.tools.manual_review, "cancel_review", None)
                if callable(cancel_review) and task.transaction_reference:
                    self._audit(task, "tool_called", tool_name="manual_review_cancel")
                    try:
                        result = cancel_review(task.task_id, task.transaction_reference)
                    except Exception:
                        task.error_code = "review_cancellation_status_unknown"
                        return task
                    if result.accepted:
                        task.state = TaskState.CANCELLED
                        task.error_code = None
                        self._audit(task, "cancelled", result="failed")
            return task

    def refresh_unknown(self, task_id: str, user_id: str, session_id: str) -> AgentTask:
        """Refresh a processing or ambiguous operation by key; never re-execute it."""
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state not in {TaskState.UNKNOWN, TaskState.PROCESSING} or not task.idempotency_key:
                return task
            if self.tools.status_query is None:
                return task
            try:
                result = self.tools.status_query.lookup(task.idempotency_key)
            except Exception:
                return task
            self._audit(task, "status_checked")
            if result is None:
                return task
            task.transaction_reference = result.transaction_reference
            task.error_code = result.error_code
            task.result = dict(result.details)
            if result.state is ExecutionState.SUCCESS:
                task.state = TaskState.SUCCESS
                self._audit(task, "completed", result="success")
            elif result.state is ExecutionState.SCHEDULED:
                task.state = TaskState.SCHEDULED
                task.result = dict(result.details)
                self._audit(task, "completed", result="scheduled")
            elif result.state is ExecutionState.PROCESSING:
                task.state = TaskState.PROCESSING
                task.result = dict(result.details)
                self._audit(task, "completed", result="processing")
            elif result.state is ExecutionState.PARTIAL:
                task.state = TaskState.PARTIAL_SUCCESS
                task.result = dict(result.details)
                self._audit(task, "completed", result="partial")
            elif result.state is ExecutionState.FAILED:
                task.state = TaskState.FAILED
                self._audit(task, "failed", result="failed", error_code=result.error_code)
            return task

    def refresh_review(self, task_id: str, user_id: str, session_id: str) -> AgentTask:
        with self._lock:
            task = self._owned_task(task_id, user_id, session_id)
            if task.state is not TaskState.UNDER_REVIEW:
                return task
            auth = self._authenticate(user_id, session_id, AuthLevel.ENHANCED, task.request.channel)
            if not auth.authenticated or auth.level < AuthLevel.ENHANCED:
                return task
            status_tool = getattr(self.tools.manual_review, "get_status", None)
            if callable(status_tool):
                self._audit(task, "tool_called", tool_name="manual_review_status")
                status = status_tool(task.transaction_reference)
                self._audit(task, "status_checked")
                if status == "declined":
                    task.state = TaskState.FAILED
                    task.error_code = "manual_review_declined"
                    self._audit(task, "failed", result="declined", error_code=task.error_code)
            return task

    def _missing_tools(self, operation: Operation) -> list[str]:
        missing: list[str] = []
        configured = set(self.config.available_tools)
        for name in required_tools(operation):
            if name not in configured or self.tools.get(name) is None:
                missing.append(f"tool:{name}")
        return missing

    @staticmethod
    def _missing_information(request: OperationRequest) -> list[str]:
        payload = request.payload
        missing: list[str] = []

        def require_text(name: str) -> None:
            if not isinstance(payload.get(name), str) or not payload.get(name, "").strip():
                missing.append(name)

        if request.operation is Operation.TRANSFER:
            if amount_as_decimal(payload.get("amount")) is None or amount_as_decimal(payload.get("amount")) <= 0:
                missing.append("positive amount")
            for name in ("currency", "source_account_ref", "execution_time", "purpose"):
                require_text(name)
            if not any(
                isinstance(payload.get(name), str) and payload.get(name, "").strip()
                for name in ("recipient_query", "recipient_ref")
            ):
                missing.append("recipient")
            if payload.get("frequency", "once") != "once" and not payload.get("end_date") and not payload.get("occurrences"):
                missing.append("recurrence_end_condition")
        elif request.operation is Operation.INVESTMENT:
            if payload.get("action") == "buy_stock":
                for name in ("stock_code", "currency", "source_account_ref"):
                    require_text(name)
                shares = payload.get("shares")
                if isinstance(shares, bool) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(shares)) or int(shares) % 100:
                    missing.append("shares (positive integer multiple of 100)")
            else:
                for name in ("product_code", "currency"):
                    require_text(name)
                amount = amount_as_decimal(payload.get("amount"))
                if amount is None or amount <= 0:
                    missing.append("positive amount")
                if payload.get("action") == "settle_order":
                    require_text("order_id")
                if payload.get("action", "subscribe") not in {"redeem", "settle_order"}:
                    for name in ("region", "goal", "horizon", "liquidity", "profile_risk", "experience", "suitability_restrictions"):
                        require_text(name)
        elif request.operation is Operation.CARD_MANAGEMENT:
            if request.payload.get("action") != "apply":
                require_text("card_ref")
            if not isinstance(payload.get("action"), str) or not payload.get("action", "").strip():
                missing.append("action")
            if payload.get("action") == "limit_change" and (amount_as_decimal(payload.get("new_limit")) is None or amount_as_decimal(payload.get("new_limit")) <= 0):
                missing.append("new_limit")
            if payload.get("action") == "set_controls" and not isinstance(payload.get("controls"), dict):
                missing.append("controls")
        elif request.operation is Operation.SCHEDULE_MANAGEMENT:
            for name in ("action", "schedule_id"):
                require_text(name)
        elif request.operation is Operation.SUBSCRIPTION:
            for name in ("merchant_ref", "cancellation_target"):
                require_text(name)
        elif request.operation is Operation.AA_REQUEST:
            for name in ("action", "currency"):
                require_text(name)
            amount = amount_as_decimal(payload.get("amount"))
            if amount is None or amount <= 0:
                missing.append("positive amount")
            if payload.get("action") == "pay_request" and not payload.get("request_id"):
                missing.append("request_id")
            if payload.get("action") == "send_request" and not any(
                isinstance(payload.get(name), str) and payload.get(name, "").strip()
                for name in ("recipient_query", "recipient_ref")
            ):
                missing.append("recipient")
        elif request.operation is Operation.CATEGORY_CORRECTION:
            for name in ("transaction_id", "category"):
                require_text(name)
        elif request.operation is Operation.BUDGET:
            for name in ("label", "month"):
                require_text(name)
            amount = amount_as_decimal(payload.get("amount"))
            if amount is None or amount <= 0:
                missing.append("positive amount")
        elif request.operation is Operation.REMINDER:
            for name in ("reminder_date", "reminder_text"):
                require_text(name)
        elif request.operation is Operation.DISPUTE:
            for name in ("transaction_id", "reason"):
                require_text(name)
        elif request.operation is Operation.CROSS_SCENARIO:
            if payload.get("action") == "create_plan":
                for name in (
                    "plan_name", "birthday_date", "reserve_amount", "source_account_ref",
                    "release_condition", "flower_budget", "cake_budget", "merchant_allowlist",
                    "goods", "authorization_mode", "delivery_details", "notification_rule",
                ):
                    if name in {"reserve_amount", "flower_budget", "cake_budget"}:
                        if amount_as_decimal(payload.get(name)) is None:
                            missing.append(name)
                    elif not payload.get(name):
                        missing.append(name)
                mode = str(payload.get("authorization_mode", ""))
                if mode in {"range", "continuous"}:
                    for name in ("max_count", "valid_until"):
                        if not payload.get(name):
                            missing.append(name)
                    if amount_as_decimal(payload.get("max_amount")) is None:
                        missing.append("max_amount")
            elif payload.get("action") in {"execute_purchase", "cancel_plan", "activate_budget"}:
                names = ("plan_id",) if payload.get("action") == "cancel_plan" else ("plan_id",)
                for name in names:
                    require_text(name)
                if payload.get("action") == "execute_purchase":
                    orders = payload.get("orders")
                    if orders is not None:
                        if not isinstance(orders, list) or not orders:
                            missing.append("orders")
                        else:
                            for index, order in enumerate(orders):
                                if not isinstance(order, dict):
                                    missing.append(f"order_{index + 1}")
                                    continue
                                for field_name in ("goods", "merchant"):
                                    if not isinstance(order.get(field_name), str) or not order.get(field_name, "").strip():
                                        missing.append(f"order_{index + 1}_{field_name}")
                                if amount_as_decimal(order.get("amount")) is None or amount_as_decimal(order.get("amount")) <= 0:
                                    missing.append(f"order_{index + 1}_amount")
                    else:
                        for name in ("purchase_total", "merchant", "goods"):
                            if name == "purchase_total":
                                if amount_as_decimal(payload.get(name)) is None:
                                    missing.append(name)
                            else:
                                require_text(name)
        return missing

    @staticmethod
    def _normalize_amounts(payload: dict[str, Any]) -> dict[str, Any]:
        """Quantise every money field once so the confirmed preview and the debit agree."""
        normalized = dict(payload)
        for key in _MONEY_FIELDS:
            if key not in normalized:
                continue
            value = amount_as_decimal(normalized[key])
            if value is not None:
                normalized[key] = format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")
        orders = normalized.get("orders")
        if isinstance(orders, list):
            fixed = []
            for row in orders:
                if isinstance(row, dict) and "amount" in row:
                    value = amount_as_decimal(row["amount"])
                    if value is not None:
                        row = {**row, "amount": format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")}
                fixed.append(row)
            normalized["orders"] = fixed
        return normalized

    def _confirmation_expiry(self) -> datetime:
        ttl = self.config.confirmation_ttl
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("Confirmation TTL is missing or invalid")
        return datetime.now(timezone.utc) + timedelta(seconds=ttl)

    def _owned_task(self, task_id: str, user_id: str, session_id: str) -> AgentTask:
        task = self._tasks.get(task_id)
        if task is None or task.request.user_id != user_id or task.request.session_id != session_id:
            raise KeyError("Task not found")
        return task

    def _fail(self, task: AgentTask, code: str) -> AgentTask:
        task.state = TaskState.FAILED
        task.error_code = code
        try:
            self._audit(task, "failed", result="failed", error_code=code)
        except AuditError:
            task.error_code = "audit_unavailable"
        return task

    def _fail_with_reason(self, task: AgentTask, code: str, reason: str) -> AgentTask:
        """Keep the user-facing reason while giving the failure a stable, narrow error code."""
        task.state = TaskState.FAILED
        task.error_code = code
        task.result = {"reason": (reason or code)[:120]}
        try:
            self._audit(task, "failed", result="failed", error_code=code)
        except AuditError:
            task.error_code = "audit_unavailable"
        return task

    def _audit(self, task: AgentTask, action: str, **fields: Any) -> None:
        risk_level = fields.pop(
            "risk_level",
            task.risk.level.name if task.risk else (
                "R0" if task.request.operation is Operation.PUBLIC_INFO else None
            ),
        )
        preview = task.preview
        event: dict[str, Any] = {
            "channel": task.request.channel,
            "session_id": self.audit.hash_identifier(task.request.session_id),
            "plan_id": task.task_id,
            "step_id": action,
            "user_id_hash": self.audit.hash_identifier(task.request.user_id),
            "operation_type": task.request.operation.value,
            "action": action,
            "risk_level": risk_level,
            "policy_version": self.config.policy_version or "unconfigured",
            "policy_rule_ids": list(task.risk.rule_ids) if task.risk else [],
            "authentication_level": fields.get("authentication_level", "none"),
            "request_summary": f"{task.request.operation.value} request",
            "amount": preview.amount if preview else None,
            "currency": preview.currency if preview else None,
            "account_last4": preview.payer_account_last4 if preview else None,
            "counterparty_hash": (
                self.audit.hash_identifier(preview.payee_name)
                if preview and preview.payee_name
                else None
            ),
            "confirmation": fields.pop("confirmation", {
                "required": task.request.operation.is_mutating and task.internal_plan.get("confirmation_policy") != "virtual_transfer_below_3000",
                "received": False,
                "timestamp": None,
                "summary_hash": task.confirmed_preview_digest,
                "expires_at": (
                    task.confirmation_expires_at.isoformat()
                    if task.confirmation_expires_at
                    else None
                ),
            }),
            "tool_name": fields.pop("tool_name", None),
            "tool_request_id": task.task_id,
            "idempotency_key": fields.pop("idempotency_key", task.idempotency_key),
            "result": fields.pop("result", None),
            "transaction_reference": task.transaction_reference,
            "error_code": fields.pop("error_code", None),
            "escalation_reason": fields.pop("escalation_reason", None),
        }
        # Only allow known, non-sensitive scalar metadata from callers.
        event.update({key: value for key, value in fields.items() if key in {"details", "error_code"}})
        self.audit.append(event)

    def _authenticate(self, user_id: str, session_id: str, required: AuthLevel, channel: str):
        """Pass channel context to adapters that support it, retaining old test adapters."""
        assert self.tools.authentication is not None
        method = self.tools.authentication.authenticate
        parameters = inspect.signature(method).parameters
        if "channel" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            return method(user_id, session_id, required, channel=channel)
        return method(user_id, session_id, required)

    @staticmethod
    def _preview_digest(preview: OperationPreview, risk: RiskAssessment | None) -> str:
        public = preview.public_dict()
        safe = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        risk_part = json.dumps(
            {
                "level": risk.level.name if risk else None,
                "flags": list(risk.flags) if risk else [],
                "manual_review": risk.manual_review_required if risk else False,
                "rules": list(risk.rule_ids) if risk else [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(f"{safe}\n{risk_part}".encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_query_result(result: QueryResult) -> dict[str, Any]:
        return {
            "data": redact_tool_output(result.data),
            "data_as_of": result.data_as_of,
            "includes_pending": result.includes_pending,
        }
