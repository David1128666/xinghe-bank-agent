from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .config import AgentConfig
from .models import Operation, OperationRequest, RiskAssessment, RiskLevel, amount_as_decimal


class PolicyError(ValueError):
    pass


def minimum_risk(request: OperationRequest) -> RiskLevel:
    operation = request.operation
    if operation is Operation.PUBLIC_INFO:
        return RiskLevel.R0
    if operation in {
        Operation.BALANCE, Operation.TRANSACTION_QUERY, Operation.DISPUTE_QUERY, Operation.PREFERENCE_QUERY, Operation.BILL_ANALYSIS,
        Operation.INVESTMENT_QUERY, Operation.CARD_QUERY, Operation.SUBSCRIPTION_QUERY,
        Operation.CROSS_SCENARIO_QUERY, Operation.AA_QUERY, Operation.SCHEDULE_QUERY,
    }:
        return RiskLevel.R1
    if operation in {Operation.CATEGORY_CORRECTION, Operation.BUDGET, Operation.REMINDER}:
        return RiskLevel.R2
    if operation is Operation.CARD_MANAGEMENT and request.payload.get("action") == "temporary_lock":
        return RiskLevel.R2
    if operation is Operation.CROSS_SCENARIO:
        return RiskLevel.R2 if request.payload.get("action") == "create_plan" else RiskLevel.R3
    return RiskLevel.R3


def effective_risk(
    request: OperationRequest,
    assessment: RiskAssessment,
    config: AgentConfig,
) -> RiskAssessment:
    level = max(minimum_risk(request), assessment.level)
    if assessment.manual_review_required:
        level = RiskLevel.R4

    if request.operation is Operation.TRANSFER or (
        request.operation is Operation.AA_REQUEST and request.payload.get("action") == "pay_request"
    ):
        amount = amount_as_decimal(request.payload.get("amount"))
        if amount is None or amount <= 0:
            raise PolicyError("transfer_amount_missing_or_invalid")
        try:
            low = Decimal(str(config.low_risk_limit))
            high = Decimal(str(config.high_risk_limit))
            daily = Decimal(str(config.daily_transfer_limit))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PolicyError("transfer_limits_not_configured") from exc
        if amount >= high or amount >= daily:
            level = RiskLevel.R4
        elif amount >= low:
            level = max(level, RiskLevel.R3)

    if request.operation is Operation.INVESTMENT:
        amount = amount_as_decimal(request.payload.get("amount"))
        if amount is not None and config.investment_confirm_limit is not None:
            try:
                limit = Decimal(str(config.investment_confirm_limit))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise PolicyError("investment_limit_invalid") from exc
            if amount >= limit:
                level = max(level, RiskLevel.R3)

    return RiskAssessment(
        level=level,
        flags=assessment.flags,
        manual_review_required=(level is RiskLevel.R4),
        rule_ids=assessment.rule_ids,
    )


def required_tools(operation: Operation, *, high_risk_possible: bool = True) -> tuple[str, ...]:
    if operation is Operation.PUBLIC_INFO:
        return ("public_info",)
    if operation in {
        Operation.BALANCE, Operation.TRANSACTION_QUERY, Operation.DISPUTE_QUERY, Operation.PREFERENCE_QUERY, Operation.BILL_ANALYSIS,
        Operation.INVESTMENT_QUERY, Operation.CARD_QUERY, Operation.SUBSCRIPTION_QUERY,
        Operation.CROSS_SCENARIO_QUERY, Operation.AA_QUERY, Operation.SCHEDULE_QUERY,
    }:
        return ("authentication", "risk_assessment", "account_query")
    tools = ["authentication", "risk_assessment", "preview", "execution", "status_query"]
    if operation in {Operation.TRANSFER, Operation.AA_REQUEST}:
        tools.append("transfer_resolver")
    if high_risk_possible:
        tools.append("manual_review")
    return tuple(tools)
