from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import (
    AuthLevel,
    AuthResult,
    ExecutionResult,
    OperationPreview,
    OperationRequest,
    PreparedOperation,
    QueryResult,
    RecipientChoice,
    ReviewResult,
    RiskAssessment,
    TransferResolution,
)


class AuthenticationTool(Protocol):
    def authenticate(
        self, user_id: str, session_id: str, required_level: AuthLevel
    ) -> AuthResult: ...


class RiskAssessmentTool(Protocol):
    def assess(
        self, request: OperationRequest, prepared: PreparedOperation | None
    ) -> RiskAssessment: ...


class AccountQueryTool(Protocol):
    def query(self, request: OperationRequest) -> QueryResult: ...


class PublicInformationTool(Protocol):
    def answer(self, question: str, jurisdiction: str) -> str: ...


class IntentParser(Protocol):
    """Optional, bank-approved LLM/parser adapter for turning chat into typed intent."""

    def parse(self, message: str) -> OperationRequest: ...


class TransferResolverTool(Protocol):
    """Resolve recipients using bank records; model-generated guesses are forbidden."""

    def resolve(
        self,
        request: OperationRequest,
        selected_recipient_ref: str | None = None,
    ) -> TransferResolution: ...


class PreviewTool(Protocol):
    def preview(
        self,
        request: OperationRequest,
        prepared: PreparedOperation | None,
    ) -> OperationPreview: ...


class ExecutionTool(Protocol):
    def execute(
        self,
        request: OperationRequest,
        prepared: PreparedOperation | None,
        preview: OperationPreview,
        idempotency_key: str,
    ) -> ExecutionResult: ...


class StatusQueryTool(Protocol):
    def lookup(self, idempotency_key: str) -> ExecutionResult | None: ...


class ManualReviewTool(Protocol):
    def submit(
        self,
        task_id: str,
        request: OperationRequest,
        prepared: PreparedOperation | None,
        preview: OperationPreview,
        assessment: RiskAssessment,
    ) -> ReviewResult: ...


class ToolTimeout(TimeoutError):
    """Execution timed out; query status with the same idempotency key."""


class ToolUnavailable(RuntimeError):
    pass


@dataclass
class ToolRegistry:
    authentication: AuthenticationTool | None = None
    risk_assessment: RiskAssessmentTool | None = None
    account_query: AccountQueryTool | None = None
    public_info: PublicInformationTool | None = None
    transfer_resolver: TransferResolverTool | None = None
    preview: PreviewTool | None = None
    execution: ExecutionTool | None = None
    status_query: StatusQueryTool | None = None
    manual_review: ManualReviewTool | None = None

    def get(self, name: str) -> Any:
        return getattr(self, name, None)
