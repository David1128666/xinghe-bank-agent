from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any, Mapping
from .privacy import redact_tool_output


class Operation(str, Enum):
    PUBLIC_INFO = "public_info"
    BALANCE = "balance"
    TRANSACTION_QUERY = "transaction_query"
    DISPUTE = "dispute"
    DISPUTE_QUERY = "dispute_query"
    BILL_ANALYSIS = "bill_analysis"
    CATEGORY_CORRECTION = "category_correction"
    BUDGET = "budget"
    REMINDER = "reminder"
    PREFERENCE_QUERY = "preference_query"
    TRANSFER = "transfer"
    AA_REQUEST = "aa_request"
    AA_QUERY = "aa_query"
    SCHEDULE_QUERY = "schedule_query"
    SCHEDULE_MANAGEMENT = "schedule_management"
    INVESTMENT = "investment"
    INVESTMENT_QUERY = "investment_query"
    CARD_MANAGEMENT = "card_management"
    CARD_QUERY = "card_query"
    SUBSCRIPTION = "subscription"
    SUBSCRIPTION_QUERY = "subscription_query"
    CROSS_SCENARIO = "cross_scenario"
    CROSS_SCENARIO_QUERY = "cross_scenario_query"

    @property
    def is_public(self) -> bool:
        return self is Operation.PUBLIC_INFO

    @property
    def is_mutating(self) -> bool:
        return self in {
            Operation.TRANSFER,
            Operation.DISPUTE,
            Operation.AA_REQUEST,
            Operation.SCHEDULE_MANAGEMENT,
            Operation.INVESTMENT,
            Operation.CARD_MANAGEMENT,
            Operation.SUBSCRIPTION,
            Operation.CROSS_SCENARIO,
            Operation.CATEGORY_CORRECTION,
            Operation.BUDGET,
            Operation.REMINDER,
        }


class TaskState(str, Enum):
    DRAFT = "DRAFT"
    AWAITING_INFORMATION = "AWAITING_INFORMATION"
    AWAITING_AUTHENTICATION = "AWAITING_AUTHENTICATION"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    UNDER_REVIEW = "UNDER_REVIEW"
    SCHEDULED = "SCHEDULED"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class RiskLevel(IntEnum):
    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3
    R4 = 4

    @classmethod
    def parse(cls, value: str | RiskLevel) -> RiskLevel:
        if isinstance(value, cls):
            return value
        return cls[value.upper()]


class AuthLevel(IntEnum):
    NONE = 0
    STANDARD = 1
    ENHANCED = 2
    MANUAL = 3


class ResolutionState(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class ExecutionState(str, Enum):
    SUCCESS = "success"
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OperationRequest:
    operation: Operation
    user_id: str
    session_id: str
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    request_id: str = ""
    channel: str = "other"


@dataclass(frozen=True)
class AuthResult:
    authenticated: bool
    level: AuthLevel
    reason_code: str | None = None


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    flags: tuple[str, ...] = ()
    manual_review_required: bool = False
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecipientChoice:
    display_name: str
    account_last4: str
    opaque_recipient_ref: str = field(repr=False)


@dataclass(frozen=True)
class PreparedOperation:
    """Opaque, bank-resolved data. Never return or write this object to audit."""

    opaque_reference: str = field(repr=False)


@dataclass(frozen=True)
class TransferResolution:
    state: ResolutionState
    prepared: PreparedOperation | None = None
    choices: tuple[RecipientChoice, ...] = ()


@dataclass(frozen=True)
class OperationPreview:
    """Fields returned by a trusted bank preview/quote tool."""

    summary: str
    amount: str | None = None
    currency: str | None = None
    payer_account_last4: str | None = None
    payee_name: str | None = None
    payee_account_last4: str | None = None
    fee: str | None = None
    execute_at: str | None = None
    expected_arrival: str | None = None
    reversible: bool | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return redact_tool_output({
            "summary": self.summary,
            "amount": self.amount,
            "currency": self.currency,
            "payer_account_last4": self.payer_account_last4,
            "payee_name": self.payee_name,
            "payee_account_last4": self.payee_account_last4,
            "fee": self.fee,
            "execute_at": self.execute_at,
            "expected_arrival": self.expected_arrival,
            "reversible": self.reversible,
            "details": dict(self.details),
        })


@dataclass(frozen=True)
class QueryResult:
    data: Any
    data_as_of: str | None = None
    includes_pending: bool | None = None


@dataclass(frozen=True)
class ExecutionResult:
    state: ExecutionState
    transaction_reference: str | None = None
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewResult:
    accepted: bool
    review_reference: str | None = None
    error_code: str | None = None


@dataclass
class AgentTask:
    task_id: str
    request: OperationRequest = field(repr=False)
    state: TaskState = TaskState.DRAFT
    risk: RiskAssessment | None = None
    preview: OperationPreview | None = None
    prepared: PreparedOperation | None = field(default=None, repr=False)
    choices: tuple[RecipientChoice, ...] = field(default=(), repr=False)
    missing_information: tuple[str, ...] = ()
    confirmation_phrase: str | None = None
    confirmation_expires_at: datetime | None = None
    confirmed_preview_digest: str | None = None
    idempotency_key: str | None = None
    result: Any = None
    transaction_reference: str | None = None
    error_code: str | None = None
    created_at: datetime | None = None
    auth_resume_stage: str | None = field(default=None, repr=False)
    selected_choice_index: int | None = field(default=None, repr=False)
    internal_plan: dict[str, Any] = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation": self.request.operation.value,
            "state": self.state.value,
            "risk_level": self.risk.level.name if self.risk else None,
            "preview": self.preview.public_dict() if self.preview else None,
            "recipient_choices": [
                {
                    "name": redact_tool_output(choice.display_name),
                    "account_last4": redact_tool_output(choice.account_last4),
                }
                for choice in self.choices
            ],
            "missing_information": list(self.missing_information),
            "confirmation_phrase": self.confirmation_phrase,
            "confirmation_expires_at": (
                self.confirmation_expires_at.isoformat()
                if self.confirmation_expires_at
                else None
            ),
            "result": self.result,
            "transaction_reference": self.transaction_reference,
            "error_code": self.error_code,
        }


def amount_as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except Exception:
        return None
