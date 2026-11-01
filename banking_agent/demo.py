"""Fully synthetic, in-memory bank and Chinese-language demo conversation."""

from __future__ import annotations

import calendar
import re
import secrets
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable

from .agent import BankingAgent
from .audit import AuditError, AuditLog
from .config import AgentConfig
from .models import (
    AuthLevel,
    AuthResult,
    ExecutionResult,
    ExecutionState,
    Operation,
    OperationPreview,
    OperationRequest,
    PreparedOperation,
    QueryResult,
    RecipientChoice,
    ResolutionState,
    ReviewResult,
    RiskAssessment,
    RiskLevel,
    TaskState,
    TransferResolution,
)
from .tools import ToolRegistry
from .stocks import SyntheticStockMarket

# Fixed demonstration business date. Keeping it constant means the synthetic
# samples, the regression suite and the published walkthroughs never "expire";
# real wall-clock time is still used for event timestamps and token expiry.
DEMO_TODAY = date(2026, 10, 29)


def virtual_config() -> AgentConfig:
    """Configuration values are intentionally fictional and demo-only."""
    return AgentConfig.from_mapping({
        "bank_name": "星河虚拟银行（演示）",
        "jurisdiction": "纯模拟环境",
        "default_currency": "CNY",
        "timezone": "Asia/Shanghai",
        "low_risk_limit": "1000",
        "high_risk_limit": "10000",
        "daily_transfer_limit": "20000",
        "investment_confirm_limit": "5000",
        "confirmation_ttl": 300,
        "auth_methods": ["虚拟会话认证"],
        "manual_review_channel": "本地模拟审核队列",
        "support_channel": "演示帮助菜单",
        "available_tools": [
            "authentication", "risk_assessment", "account_query", "public_info",
            "transfer_resolver", "preview", "execution", "status_query", "manual_review",
        ],
        "policy_version": "virtual-demo-1.0",
    })


class TransferLedger:
    """Process-wide daily transfer totals so a new browser session cannot reset a limit."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[date, str], Decimal] = {}

    def total(self, day: date, last4s: Iterable[str] | None = None) -> Decimal:
        with self._lock:
            scope = set(last4s) if last4s is not None else None
            return sum((amount for (row_day, last4), amount in self._rows.items()
                        if row_day == day and (scope is None or last4 in scope)), Decimal("0"))

    def record(self, day: date, last4: str, amount: Decimal) -> None:
        with self._lock:
            key = (day, last4)
            self._rows[key] = self._rows.get(key, Decimal("0")) + amount


class AuthChallenge:
    """One-time demonstration security code: the only way to reach the enhanced level."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self.code = ""
        self.expires_at = datetime.now(timezone.utc)
        self.rotate()

    def rotate(self) -> None:
        self.code = f"{secrets.randbelow(900000) + 100000}"
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)

    def verify(self, code: str) -> bool:
        if not isinstance(code, str) or datetime.now(timezone.utc) >= self.expires_at:
            return False
        if not secrets.compare_digest(code.strip(), self.code):
            return False
        self.rotate()
        return True

    def public(self) -> dict[str, str]:
        return {"code": self.code, "expires_at": self.expires_at.isoformat(),
                "note": "演示专用一次性安全码，仅代表本次本机会话；不是真实短信验证码或硬件令牌。"}


class SimulatedBank:
    """Bank-tool adapter backed only by fictional in-memory data."""

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or virtual_config()
        self.audit: AuditLog | None = None
        self._identity: tuple[str, str] | None = None
        self._channel_source: Callable[[], str] | None = None
        self.ledger: TransferLedger | None = None
        self.accounts: dict[str, dict[str, Any]] = {
            "checking": {"label": "模拟活期账户", "last4": "1234", "balance": Decimal("50000.00")},
            "savings": {"label": "模拟储蓄账户", "last4": "5678", "balance": Decimal("30000.00")},
        }
        self.recipients: dict[str, dict[str, Any]] = {
            "sim-recipient-xm": {"name": "小明", "last4": "6789", "phone": "13900000001", "history_note": "午餐搭子", "known": True, "balance": Decimal("900.00")},
            "sim-recipient-zw-1": {"name": "张伟", "last4": "2211", "phone": "13900000002", "history_note": "装修款", "known": True, "balance": Decimal("500.00")},
            "sim-recipient-zw-2": {"name": "张伟", "last4": "3344", "phone": "13900000003", "history_note": "旧同学", "known": True, "balance": Decimal("700.00")},
            "sim-recipient-lin": {"name": "林女士", "last4": "8091", "phone": "13900000004", "history_note": "新联系人", "known": False, "balance": Decimal("1200.00")},
        }
        self.cards = {
            "sim-card-1": {
                "last4": "9012", "status": "active", "limit": Decimal("20000.00"),
                "controls": {"domestic": True, "international": False, "online": True,
                             "offline": True, "card_not_present": True},
                "application_status": "已制卡（模拟）", "replacement_status": None,
            },
        }
        self.products = {
            "P-BOND-SIM": {
                "name": "星河稳盈债券组合（模拟）", "risk": RiskLevel.R2,
                "nav": Decimal("1.0234"), "fee_rate": Decimal("0.003"),
                "lock_period": "无锁定期（模拟）", "principal_risk": "可能损失本金",
                "liquidity": "工作日可申请赎回", "horizon": "1 年以上（示例）",
                "history": "示例净值序列仅用于界面演示，不代表未来表现",
                "updated_at": "2026-10-21T09:00:00+08:00", "early_exit_fee": "无（模拟）",
            },
            "P-EQUITY-SIM": {
                "name": "星河成长股票组合（模拟）", "risk": RiskLevel.R4,
                "nav": Decimal("2.1840"), "fee_rate": Decimal("0.008"),
                "lock_period": "建议持有 3 年以上（模拟）", "principal_risk": "可能损失本金",
                "liquidity": "建议长期持有", "horizon": "3 年以上（示例）",
                "history": "示例净值序列仅用于界面演示，不代表未来表现",
                "updated_at": "2026-10-21T09:00:00+08:00", "early_exit_fee": "可能有费用（模拟）",
            },
        }
        self.holdings: dict[str, Decimal] = {}
        self.stock_market = SyntheticStockMarket()
        self.investment_orders: dict[str, dict[str, Any]] = {}
        self.investment_profiles: dict[str, dict[str, Any]] = {}
        self.disputes: dict[str, dict[str, Any]] = {}
        self.budget_labels: dict[str, dict[str, str]] = {}
        self.reminders: list[dict[str, str]] = []
        self.subscriptions = {
            "moon-music": {
                "name": "月光音乐（虚构商户）", "amount": Decimal("18.00"),
                "period": "每月", "next_charge": "2026-11-01",
                "merchant_contract": "active", "bank_debit": "active",
                "card_recurring_payment": "active", "refund_status": None,
                "last_charge": "2026-10-01", "paid_entitlement_until": "2026-10-31",
                "penalty": "无（模拟）", "recoverable": True, "reminder": False,
                "price_alert": True, "price_history": [("2025-12-01", Decimal("16.00")), ("2026-10-01", Decimal("18.00"))],
            },
            "cloud-box": {
                "name": "云朵盒子（虚构商户）", "amount": Decimal("12.00"),
                "period": "每月", "next_charge": "2026-10-28",
                "merchant_contract": "active", "bank_debit": "active",
                "card_recurring_payment": "active", "refund_status": None,
                "last_charge": "2026-09-28", "paid_entitlement_until": "2026-10-27",
                "penalty": "可能失去当前周期权益（模拟）", "recoverable": True,
                "reminder": True, "price_alert": False, "price_history": [("2026-08-28", Decimal("12.00"))],
            },
        }
        self.transactions = [
            {"id": "SIM-TX-101", "date": "2026-10-19", "merchant": "青禾食堂（虚构）", "amount": "36.00", "category": "餐饮", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-102", "date": "2026-10-18", "merchant": "云朵盒子（虚构）", "amount": "12.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-103", "date": "2026-10-17", "merchant": "晨光便利店（虚构）", "amount": "21.50", "category": "日用", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-104", "date": "2026-10-15", "merchant": "青禾食堂（虚构）", "amount": "48.00", "category": "餐饮", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-105", "date": "2026-10-12", "merchant": "夜航数码集市（虚构）", "amount": "888.00", "category": "购物（推测）", "status": "已入账", "anomaly_features": ["金额高于该虚构样例的常见支出"]},
            {"id": "SIM-TX-106", "date": "2026-10-01", "merchant": "月光音乐（虚构商户）", "amount": "18.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-107", "date": "2026-09-28", "merchant": "云朵盒子（虚构商户）", "amount": "12.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-108", "date": "2026-09-19", "merchant": "青禾食堂（虚构）", "amount": "42.00", "category": "餐饮", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-109", "date": "2026-09-01", "merchant": "月光音乐（虚构商户）", "amount": "18.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-110", "date": "2026-08-28", "merchant": "云朵盒子（虚构商户）", "amount": "12.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-111", "date": "2026-08-01", "merchant": "月光音乐（虚构商户）", "amount": "18.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
            {"id": "SIM-TX-112", "date": "2025-12-01", "merchant": "月光音乐（虚构商户）", "amount": "16.00", "category": "数字服务", "status": "已入账", "anomaly_features": []},
        ]
        self.transfer_totals: dict[tuple[date, str], Decimal] = {}
        self.scheduled_transfers: dict[str, dict[str, Any]] = {}
        self.aa_requests: dict[str, dict[str, Any]] = {}
        self.aa_requests["SIM-AA-0000"] = {
            "request_id": "SIM-AA-0000", "from_recipient_ref": "sim-recipient-xm",
            "amount": Decimal("20.00"), "currency": "CNY", "purpose": "晚餐（模拟）", "status": "pending",
        }
        self.reservations: dict[str, Decimal] = {}
        self.cross_plans: dict[str, dict[str, Any]] = {}
        self._next_card = 2
        self._next_request = 1
        self._review_status: dict[str, str] = {}
        self.calendar_access_granted = False
        self.calendar_events = [
            {"event_id": "SIM-CAL-001", "date": "2026-11-10", "title": "纪念日A（虚构事件）"},
            {"event_id": "SIM-CAL-002", "date": "2026-12-15", "title": "家庭聚会（虚构事件）"},
        ]
        self._executions: dict[str, ExecutionResult] = {}
        self._reviews = 0

    @staticmethod
    def _money(value: Decimal) -> str:
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _today() -> date:
        # Fixed virtual business date (Asia/Shanghai baseline); see DEMO_TODAY.
        return DEMO_TODAY

    def _account_scope(self, account_ref: str | None) -> list[str]:
        if account_ref and account_ref in self.accounts:
            return [self.accounts[account_ref]["last4"]]
        return [row["last4"] for row in self.accounts.values()]

    def _daily_transfer_total(self, account_ref: str | None = None) -> Decimal:
        today = self._today()
        if self.ledger is not None:
            return self.ledger.total(today, self._account_scope(account_ref))
        return sum((amount for (day, account), amount in self.transfer_totals.items()
                    if day == today and (account_ref is None or account == account_ref)), Decimal("0"))

    def _record_transfer(self, account_ref: str, amount: Decimal) -> None:
        key = (self._today(), account_ref)
        self.transfer_totals[key] = self.transfer_totals.get(key, Decimal("0")) + amount
        if self.ledger is not None:
            self.ledger.record(self._today(), self.accounts[account_ref]["last4"], amount)

    def _order_audit(self, request: OperationRequest, plan_id: str, step_id: str, action: str, result: str | None = None) -> None:
        if self.audit is not None:
            self.audit.append({
                "plan_id": plan_id, "step_id": step_id, "action": action, "result": result,
                "channel": request.channel, "user_id_hash": self.audit.hash_identifier(request.user_id),
                "session_id": self.audit.hash_identifier(request.session_id), "operation_type": "cross_scenario",
                "policy_version": self.config.policy_version, "tool_name": "virtual_merchant",
                "request_summary": "synthetic order step",
            })

    def _transfer_limit_risk(self, amount: Decimal, *, include_today: bool = True) -> RiskAssessment | None:
        daily = Decimal(str(self.config.daily_transfer_limit))
        high = Decimal(str(self.config.high_risk_limit))
        total = self._daily_transfer_total() if include_today else Decimal("0")
        if amount >= high or total + amount >= daily:
            return RiskAssessment(RiskLevel.R4, flags=("模拟转账需要人工审核",),
                                  manual_review_required=True, rule_ids=("SIM-TRANSFER-LIMIT",))
        return None

    # AuthenticationTool
    def bind_identity(self, user_id: str, session_id: str) -> None:
        """Bind the adapter to its owning conversation; any other identity is rejected."""
        self._identity = (user_id, session_id)

    def bind_channel_source(self, source: Callable[[], str]) -> None:
        """The authenticated channel is server state, never caller-supplied text."""
        self._channel_source = source

    def authenticate(
        self, user_id: str, session_id: str, required_level: AuthLevel, channel: str = "app"
    ) -> AuthResult:
        identity = self._identity or ("demo-user", "demo-session")
        ok = (user_id, session_id) == identity
        # A request may claim any channel; the adapter always uses the server-side one.
        effective = self._channel_source() if self._channel_source is not None else channel
        channel_levels = {"app": AuthLevel.ENHANCED, "im": AuthLevel.STANDARD, "other": AuthLevel.NONE}
        level = channel_levels.get(effective, AuthLevel.NONE) if ok else AuthLevel.NONE
        return AuthResult(ok and level >= required_level, level)

    # RiskAssessmentTool
    def assess(self, request: OperationRequest, prepared: PreparedOperation | None) -> RiskAssessment:
        text = f"{request.summary} {request.payload.get('purpose', '')}".lower()
        suspicious = ("安全账户", "监管账户", "公检法", "验证码", "远程控制", "秘密转账", "退款前先付款", "屏幕共享", "投资群", "催促", "胁迫", "被监视", "紧急借款", "新设备", "异常位置", "陌生地点", "跨境", "境外收款", "共享屏幕", "监视", "安装远程控制")
        protective = request.operation is Operation.CARD_MANAGEMENT and request.payload.get("action") == "temporary_lock"
        if request.operation.is_mutating and not protective and any(term in text for term in suspicious):
            return RiskAssessment(RiskLevel.R4, flags=("需要独立核实当前请求",), manual_review_required=True, rule_ids=("SIM-FRAUD-CHECK",))
        if not request.operation.is_mutating and any(term in text for term in ("新设备", "异常位置", "敏感流水")):
            return RiskAssessment(RiskLevel.R3, flags=("此查询需要增强认证",), rule_ids=("SIM-SENSITIVE-READ",))
        if request.operation in {Operation.TRANSFER, Operation.AA_REQUEST}:
            recipient_id = prepared.opaque_reference.split(":", 1)[1] if prepared else ""
            if request.payload.get("action") == "pay_request":
                payment = self.aa_requests.get(str(request.payload.get("request_id", "")), {})
                recipient_id = str(payment.get("from_recipient_ref", ""))
            if recipient_id and not self.recipients.get(recipient_id, {}).get("known", True):
                return RiskAssessment(RiskLevel.R4, flags=("首次联系的模拟收款人需要额外核实",), manual_review_required=True, rule_ids=("SIM-NEW-RECIPIENT",))
            amount = Decimal(str(request.payload.get("amount", "0")))
            if request.operation is Operation.TRANSFER or request.payload.get("action") == "pay_request":
                limit_risk = self._transfer_limit_risk(amount, include_today=request.payload.get("execution_time", "now") == "now")
                if limit_risk:
                    return limit_risk
            return RiskAssessment(RiskLevel.R1, rule_ids=("SIM-TRANSFER-CHECK",))

        if request.operation is Operation.INVESTMENT:
            if request.payload.get("action") == "buy_stock":
                self.stock_market.quote(request.payload)
                return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-STOCK-EXPLICIT-BUY",))
            product = self.products.get(str(request.payload.get("product_code", "")))
            action = str(request.payload.get("action", "subscribe"))
            if action == "settle_order":
                return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-SETTLEMENT",))
            if action == "redeem":
                amount = Decimal(str(request.payload.get("amount", "0")))
                if product is None or amount > self.holdings.get(str(request.payload.get("product_code")), Decimal("0")):
                    return RiskAssessment(RiskLevel.R4, flags=("模拟持仓不足或产品不存在",), manual_review_required=True, rule_ids=("SIM-REDEMPTION-CHECK",))
            else:
                profile = dict(request.payload)
                profile["risk_tolerance"] = request.payload.get("profile_risk", "R0")
                suitable, _ = self._assess_product_fit(product, profile) if product else (False, ["模拟产品不存在"])
                if not suitable:
                    return RiskAssessment(
                        RiskLevel.R4, flags=("模拟适当性检查未通过",), manual_review_required=True,
                        rule_ids=("SIM-SUITABILITY",),
                    )
            if product is None:
                return RiskAssessment(
                    RiskLevel.R4,
                    flags=("模拟产品不存在",),
                    manual_review_required=True,
                    rule_ids=("SIM-SUITABILITY",),
                )
            return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-SUITABILITY-PASS",))

        if request.operation is Operation.CARD_MANAGEMENT:
            action = str(request.payload.get("action", ""))
            if action in {"unlock", "permanent_loss", "limit_change", "replace", "apply", "set_controls"}:
                return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-CARD-CHANGE",))
            return RiskAssessment(RiskLevel.R2, rule_ids=("SIM-CARD-PROTECT",))

        if request.operation is Operation.SUBSCRIPTION:
            return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-SUBSCRIPTION",))
        if request.operation in {Operation.BUDGET, Operation.REMINDER}:
            return RiskAssessment(RiskLevel.R2, rule_ids=("SIM-PREFERENCES",))
        if request.operation is Operation.SCHEDULE_MANAGEMENT:
            schedule = self.scheduled_transfers.get(str(request.payload.get("schedule_id", "")))
            if request.payload.get("action") == "run_schedule" and schedule:
                recipient = self.recipients[schedule["recipient_ref"]]
                if not recipient.get("known", True):
                    return RiskAssessment(RiskLevel.R4, flags=("模拟收款人需要额外核实",), manual_review_required=True, rule_ids=("SIM-NEW-RECIPIENT",))
                limit_risk = self._transfer_limit_risk(schedule["amount"])
                if limit_risk:
                    return limit_risk
            return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-SCHEDULE-MANAGEMENT",))
        if request.operation is Operation.CROSS_SCENARIO:
            if request.payload.get("action") == "create_plan":
                return RiskAssessment(RiskLevel.R2, rule_ids=("SIM-PLAN-CREATE",))
            return RiskAssessment(RiskLevel.R3, rule_ids=("SIM-PLAN-EXECUTE",))
        return RiskAssessment(RiskLevel.R1, rule_ids=("SIM-READ",))

    # AccountQueryTool
    def query(self, request: OperationRequest) -> QueryResult:
        payload = request.payload
        if request.operation is Operation.BALANCE:
            data = {
                "environment": "模拟数据",
                "accounts": [
                    {
                        "name": account["label"],
                        "account_last4": account["last4"],
                        "currency": "CNY",
                        "ledger_balance": self._money(account["balance"]),
                        "reserved_for_virtual_plans": self._money(self.reservations.get(name, Decimal("0"))),
                        "available_balance": self._money(account["balance"] - self.reservations.get(name, Decimal("0"))),
                    }
                    for name, account in self.accounts.items()
                ],
            }
        elif request.operation is Operation.BILL_ANALYSIS:
            data = self._bill_report(payload)
        elif request.operation is Operation.DISPUTE_QUERY:
            data = {"environment": "模拟数据", "disputes": list(self.disputes.values())}
        elif request.operation is Operation.PREFERENCE_QUERY:
            data = {"environment": "模拟数据", "budget_labels": list(self.budget_labels.values()), "reminders": list(self.reminders)}
        elif request.operation is Operation.TRANSACTION_QUERY:
            rows = list(self.transactions)
            search = str(payload.get("transaction_reference") or payload.get("search", "")).strip()
            if search:
                rows = [r for r in rows if search.casefold() in (r["merchant"] + r["id"] + str(r.get("reference", ""))).casefold()]
            data = {"environment": "模拟数据", "transactions": rows, "count": len(rows)}
        elif request.operation is Operation.INVESTMENT_QUERY:
            action = str(payload.get("action", "products"))
            if action == "stocks":
                data = {"environment": "纯虚构教学股票", "stocks": self.stock_market.listings(),
                        "market_note": "固定虚构样例价；点击买入后立即模拟成交并扣活期余额，不连接真实证券市场。"}
            elif action == "stock_holdings":
                data = {"environment": "纯虚构教学股票", "stock_holdings": self.stock_market.holdings(),
                        "stock_orders": list(self.stock_market.orders)}
            elif action == "holdings":
                rows = [
                    {"product_code": code, "product_name": self.products[code]["name"],
                     "holding_value": self._money(amount), "risk_level": self.products[code]["risk"].name}
                    for code, amount in self.holdings.items() if amount > 0
                ]
                data = {"environment": "模拟数据", "holdings": rows}
            elif action == "order_status":
                order_id = str(payload.get("order_id", ""))
                order = self.investment_orders.get(order_id)
                data = {"environment": "模拟数据", "order": {k: v for k, v in order.items() if k != "idempotency_key"} if order else None, "order_id": order_id}
            else:
                max_risk = str(payload.get("max_risk", "R4"))
                rows = []
                for code, product in self.products.items():
                    if product["risk"] > RiskLevel.parse(max_risk):
                        continue
                    rows.append({
                        "product_code": code, "name": product["name"],
                        "risk_level": product["risk"].name, "nav": str(product["nav"]),
                        "fee_rate": str(product["fee_rate"]), "lock_period": product["lock_period"],
                        "liquidity": product["liquidity"], "suggested_horizon": product["horizon"],
                        "principal_risk": product["principal_risk"], "early_exit_fee": product["early_exit_fee"],
                        "quote_as_of": product["updated_at"], "historical_performance_note": product["history"],
                    })
                compare = payload.get("compare_codes") or []
                selected = [row for row in rows if row["product_code"] in compare] if compare else rows
                profile = payload.get("profile") or {}
                required = ("region", "goal", "horizon", "liquidity", "risk_tolerance", "experience", "amount", "suitability_restrictions")
                missing = [key for key in required if not profile.get(key)]
                matches = []
                reasons = {}
                if not missing:
                    self.investment_profiles[request.user_id] = dict(profile)
                    for row in selected:
                        suitable, reason = self._assess_product_fit(self.products[row["product_code"]], profile)
                        if suitable:
                            matches.append(row["product_code"])
                            reasons[row["product_code"]] = reason
                data = {
                    "environment": "模拟数据", "products": selected,
                    "quote_as_of": self._now(), "profile_missing_fields": missing,
                    "suitable_product_codes": matches, "suitability_reasons": reasons,
                    "recommendation_note": "仅在完整资料下按风险档案和期限作演示匹配；不构成收益承诺或真实推荐。" if not missing else "补齐问卷前只展示目录，不作个性化推荐。",
                }
        elif request.operation is Operation.CARD_QUERY:
            action = str(payload.get("action", "list"))
            if action == "application_status":
                data = {"environment": "模拟数据", "application_statuses": [
                    {"card_last4": c["last4"], "application_status": c["application_status"], "replacement_status": c["replacement_status"],
                     "limit_request_status": c.get("limit_request_status"), "requested_limit": self._money(c["requested_limit"]) if c.get("requested_limit") is not None else None}
                    for c in self.cards.values()
                ]}
            else:
                data = {"environment": "模拟数据", "cards": [
                    {"card_last4": c["last4"], "status": c["status"], "limit": self._money(c["limit"]), "controls": dict(c["controls"])}
                    for c in self.cards.values()
                ]}
        elif request.operation is Operation.SUBSCRIPTION_QUERY:
            rows = []
            for key, sub in self.subscriptions.items():
                rows.append({
                    "subscription_ref": key, "merchant": sub["name"], "amount": self._money(sub["amount"]),
                    "period": sub["period"], "estimated_monthly_cost": self._money(sub["amount"]),
                    "last_charge": sub["last_charge"], "next_charge": sub["next_charge"],
                    "merchant_contract": sub["merchant_contract"], "bank_debit": sub["bank_debit"],
                    "card_recurring_payment": sub["card_recurring_payment"],
                    "renewal_reminder": sub["reminder"], "price_change_alert": sub["price_alert"],
                })
            total = sum((sub["amount"] for sub in self.subscriptions.values() if sub["merchant_contract"] == "active"), Decimal("0"))
            changes = []
            for sub in self.subscriptions.values():
                history = sub["price_history"]
                if len(history) >= 2 and history[-1][1] != history[-2][1]:
                    changes.append({"merchant": sub["name"], "from": self._money(history[-2][1]), "to": self._money(history[-1][1]), "effective_date": history[-1][0], "alert_enabled": sub["price_alert"], "note": "检测到样例金额变化；请核对商户通知，不代表已确认涨价事实。"})
            data = {"environment": "模拟数据", "subscriptions": rows, "active_monthly_total": self._money(total), "recurring_detections": self._recurring_detections(), "price_change_alerts": changes}
        elif request.operation is Operation.CROSS_SCENARIO_QUERY:
            if payload.get("action") == "calendar":
                data = {
                    "environment": "模拟数据", "authorization_required": not self.calendar_access_granted,
                    "events": list(self.calendar_events) if self.calendar_access_granted else [],
                    "message": "这是虚构日历样例；事件名称不代表任何亲属身份。请用户自行确认目标日期。" if self.calendar_access_granted else "读取前请明确授权“模拟日历”；此工具只访问本地虚构事件。",
                }
            else:
                data = {
                    "environment": "模拟数据", "plans": [self._public_plan(plan) for plan in self.cross_plans.values()],
                    "scheduled_transfers": [self._public_schedule(item) for item in self.scheduled_transfers.values()],
                }
        elif request.operation is Operation.SCHEDULE_QUERY:
            data = {"environment": "模拟数据", "scheduled_transfers": [self._public_schedule(item) for item in self.scheduled_transfers.values()],
                    "renewal_reminders": [{"merchant": sub["name"], "remind_before": sub["next_charge"], "amount": self._money(sub["amount"]), "delivery": "仅在本地提醒列表显示"} for sub in self.subscriptions.values() if sub["reminder"] and sub["merchant_contract"] == "active"],
                    "plan_reminders": [{"plan_id": plan["plan_id"], "date": plan["purchase_date"], "notification_rule": plan["notification_rule"]} for plan in self.cross_plans.values() if plan["status"] == "scheduled"], "custom_reminders": list(self.reminders)}
        elif request.operation is Operation.AA_QUERY:
            data = {"environment": "模拟数据", "payment_requests": [
                {"request_id": row["request_id"], "from": self.recipients[row["from_recipient_ref"]]["name"],
                 "amount": self._money(row["amount"]), "currency": row["currency"], "purpose": row["purpose"], "status": row["status"]}
                for row in self.aa_requests.values()
            ]}
        else:
            data = {"environment": "模拟数据", "message": "该查询类型在演示数据中没有样例。"}
        return QueryResult(data=data, data_as_of=self._now(), includes_pending=False)

    def _bill_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        start, end, label = self._resolve_period(payload)
        rows = [row for row in self.transactions if start <= date.fromisoformat(row["date"]) <= end]
        by_category: dict[str, Decimal] = {}
        by_merchant: dict[str, Decimal] = {}
        by_month: dict[str, Decimal] = {}
        for row in rows:
            amount = Decimal(row["amount"])
            by_category[row["category"]] = by_category.get(row["category"], Decimal("0")) + amount
            by_merchant[row["merchant"]] = by_merchant.get(row["merchant"], Decimal("0")) + amount
            month = row["date"][:7]
            by_month[month] = by_month.get(month, Decimal("0")) + amount
        anomalies = [
            {"transaction_id": row["id"], "merchant": row["merchant"], "amount": row["amount"],
             "features": row["anomaly_features"], "interpretation": "存在需要核实的异常特征，不代表已确认欺诈。"}
            for row in rows if row.get("anomaly_features")
        ]
        recurring = self._recurring_detections(rows)
        largest = sorted(by_category.items(), key=lambda item: item[1], reverse=True)[:2]
        suggestions = [f"可回看“{category}”类的虚构样例明细，比较是否有可调整支出。" for category, amount in largest if amount > 100]
        if recurring:
            suggestions.append("可核对周期性扣款是否仍在使用；此建议不会自动取消任何服务。")
        return {
            "environment": "模拟数据", "period": label, "period_start": start.isoformat(), "period_end": end.isoformat(),
            "includes_pending": False, "total_spend": self._money(sum(by_category.values(), Decimal("0"))),
            "by_category": {k: self._money(v) for k, v in by_category.items()},
            "by_merchant": {k: self._money(v) for k, v in by_merchant.items()},
            "monthly_trend": {k: self._money(v) for k, v in sorted(by_month.items())},
            "recurring_expenses": recurring, "anomalies_to_review": anomalies,
            "anomaly_options": ["查看对应交易记录", "核对目标卡片后临时锁卡", "联系虚拟客服", "核实后单独确认提交争议申请"] if anomalies else [],
            "category_labels_are_estimates": True, "category_correction_examples": [
                {"transaction_id": row["id"], "current_category": row["category"]} for row in rows[:5]
            ], "savings_suggestions": suggestions, "transactions": rows,
        }

    def _resolve_period(self, payload: dict[str, Any]) -> tuple[date, date, str]:
        if payload.get("start_date") and payload.get("end_date"):
            start, end = date.fromisoformat(str(payload["start_date"])), date.fromisoformat(str(payload["end_date"]))
            if end < start or (end - start).days > 3660:
                raise ValueError("模拟账单日期范围无效或过长")
            return start, end, f"{start.isoformat()} 至 {end.isoformat()}"
        if payload.get("year"):
            year = int(payload["year"])
            return date(year, 1, 1), date(year, 12, 31), f"{year} 年"
        month_text = str(payload.get("month", "2026-10"))
        match = re.fullmatch(r"(\d{4})-(\d{1,2})", month_text)
        if not match:
            raise ValueError("账单月份应为 YYYY-MM")
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise ValueError("账单月份无效")
        start = date(year, month, 1)
        end = (date(year + 1, 1, 1) - timedelta(days=1)) if month == 12 else (date(year, month + 1, 1) - timedelta(days=1))
        return start, end, f"{year}-{month:02d}"

    def _recurring_detections(self, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        source = rows if rows is not None else self.transactions
        by_merchant: dict[str, list[dict[str, Any]]] = {}
        for row in source:
            canonical = re.sub(r"\s*（(?:虚构商户|虚构)）", "", row["merchant"])
            by_merchant.setdefault(canonical, []).append(row)
        result = []
        for merchant, items in by_merchant.items():
            months = {item["date"][:7] for item in items}
            if len(items) >= 2 and len(months) >= 2:
                values = [Decimal(item["amount"]) for item in items]
                result.append({"merchant": merchant, "occurrences": len(items),
                               "observed_amounts": [self._money(v) for v in values],
                               "pattern": "疑似周期性支出（需核对，不保证为订阅）"})
        return result

    @staticmethod
    def _assess_product_fit(product: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        region = str(profile.get("region", "")).lower()
        if not any(token in region for token in ("虚构", "模拟", "demo")):
            return False, ["演示产品目录仅在虚构或模拟地区资料下评估"]
        try:
            risk = RiskLevel.parse(str(profile.get("risk_tolerance", "R0")))
        except ValueError:
            return False, ["风险档案未通过模拟校验"]
        if product["risk"] > risk:
            return False, ["产品风险等级高于用户模拟风险承受能力"]
        restrictions = str(profile.get("suitability_restrictions", "")).strip().lower()
        if restrictions not in {"无", "无特殊限制", "无限制", "none", "no"}:
            return False, ["存在待核实的适当性限制"]
        horizon = str(profile.get("horizon", ""))
        years = re.search(r"(\d+(?:\.\d+)?)", horizon)
        years_value = float(years.group(1)) if years else (5.0 if "长期" in horizon else 0.0)
        if "月" in horizon and "年" not in horizon:
            years_value /= 12
        elif "天" in horizon and "年" not in horizon:
            years_value /= 365
        required_years = 3.0 if product["risk"] >= RiskLevel.R4 else 1.0
        if years_value < required_years:
            return False, [f"模拟投资期限需至少 {int(required_years)} 年"]
        available = Decimal(str(profile.get("investable_amount", profile.get("amount", "0"))))
        requested_amount = Decimal(str(profile.get("amount", "0")))
        if not available.is_finite() or not requested_amount.is_finite() or requested_amount <= 0 or requested_amount > available:
            return False, ["投资金额超过所确认的可投资金额或金额无效"]
        goal = str(profile.get("goal", ""))
        if not any(token in goal for token in ("稳健", "配置", "增长", "成长", "长期", "保值", "增值")):
            return False, ["投资目标与演示产品资料无法匹配"]
        liquidity = str(profile.get("liquidity", ""))
        experience = str(profile.get("experience", ""))
        if product["risk"] >= RiskLevel.R4 and any(token in liquidity for token in ("高", "随时", "即时")):
            return False, ["高流动性需求与模拟产品赎回安排不匹配"]
        if product["risk"] >= RiskLevel.R4 and any(token in experience for token in ("新手", "无经验", "不了解")):
            return False, ["模拟产品风险与所述投资经验不匹配"]
        reasons.extend([f"风险等级 {product['risk'].name} 不高于模拟档案 {risk.name}", f"期限 {horizon} 满足演示要求", f"目标“{goal}”与产品资料相符"])
        return True, reasons

    @staticmethod
    def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in plan.items() if k not in {"internal_notes", "user_id", "idempotency_key"}}
        for field in ("reserve_amount", "reserved_remaining"):
            if isinstance(result.get(field), Decimal):
                result[field] = SimulatedBank._money(result[field])
        return result

    @staticmethod
    def _public_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
        return {**schedule, "amount": SimulatedBank._money(schedule["amount"])}

    @staticmethod
    def _items(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in re.split(r"[,，、]", str(value or "")) if item.strip()]

    # PublicInformationTool
    def answer(self, question: str, jurisdiction: str) -> str:
        if "网点" in question or "营业时间" in question:
            return f"虚构网点：星河云端演示厅；位置：虚拟世界演示区 A-01；样例营业时间：09:00–17:00。全部为本地样例，数据时间：{self._now()}。"
        if any(word in question for word in ("客服", "召回", "盗刷", "应急")):
            return "虚拟客服入口：本地演示帮助菜单。可先查看交易记录；确认目标卡后临时锁卡；提交争议须单独确认。已完成的转账不会由聊天自动撤销，召回/退款不能保证成功。本演示没有真实联系方式。"
        return (
            "这是星河虚拟银行的离线演示环境。这里的网点、产品、账户、规则和交易均为虚构样例；"
            "没有连接任何真实银行或外部服务。你可以查询模拟余额/账单，或演示转账、卡片及订阅流程。"
        )

    # TransferResolverTool
    def resolve(
        self,
        request: OperationRequest,
        selected_recipient_ref: str | None = None,
    ) -> TransferResolution:
        if selected_recipient_ref:
            recipient_id = selected_recipient_ref
            if recipient_id not in self.recipients:
                return TransferResolution(ResolutionState.NOT_FOUND)
            return TransferResolution(
                ResolutionState.MATCHED,
                PreparedOperation(f"recipient:{recipient_id}"),
            )
        explicit_ref = str(request.payload.get("recipient_ref", ""))
        if explicit_ref and explicit_ref in self.recipients:
            matches = [explicit_ref]
        else:
            query = str(request.payload.get("recipient_query", "")).strip()
            matches = [
                recipient_id
                for recipient_id, recipient in self.recipients.items()
                if query and (
                    query.casefold() in recipient["name"].casefold()
                    or query in recipient.get("phone", "")
                    or query.casefold() in recipient.get("history_note", "").casefold()
                )
            ]
            exact_names = [recipient_id for recipient_id in matches
                           if self.recipients[recipient_id]["name"].casefold() == query.casefold()]
            if exact_names:
                matches = exact_names
        if not matches:
            return TransferResolution(ResolutionState.NOT_FOUND)
        if len(matches) > 1:
            choices = tuple(
                RecipientChoice(
                    self.recipients[recipient_id]["name"],
                    self.recipients[recipient_id]["last4"],
                    recipient_id,
                )
                for recipient_id in matches
            )
            return TransferResolution(ResolutionState.AMBIGUOUS, choices=choices)
        return TransferResolution(
            ResolutionState.MATCHED,
            PreparedOperation(f"recipient:{matches[0]}"),
        )

    # PreviewTool
    def preview(
        self,
        request: OperationRequest,
        prepared: PreparedOperation | None,
    ) -> OperationPreview:
        payload = request.payload
        if payload.get("currency") and payload["currency"] != "CNY":
            raise ValueError("虚拟演示工具仅支持 CNY，不执行隐式换汇")
        if request.operation is Operation.BUDGET:
            self._resolve_period({"month": payload["month"]})
            return OperationPreview(summary=f"设置虚拟预算标签“{payload['label']}”，月份 {payload['month']}。", amount=str(payload["amount"]), currency="CNY", reversible=True,
                                    details={"影响": "只设置支出目标标签，不冻结或转移任何资金", "环境": "纯虚拟模拟"})
        if request.operation is Operation.REMINDER:
            reminder_date = date.fromisoformat(str(payload["reminder_date"]))
            if reminder_date < self._today():
                raise ValueError("提醒日期不能在过去")
            return OperationPreview(summary=f"创建本地虚拟提醒：{payload['reminder_text']}。", execute_at=reminder_date.isoformat(), reversible=True,
                                    details={"通知方式": "显示在本地提醒列表，不发送外部消息", "环境": "纯虚拟模拟"})
        if request.operation is Operation.DISPUTE:
            row = next((item for item in self.transactions if item["id"] == str(payload["transaction_id"])), None)
            if not row:
                raise ValueError("模拟交易不存在")
            return OperationPreview(summary=f"提交模拟交易 {row['id']} 的争议申请。", amount=row["amount"], currency="CNY", execute_at="确认后登记（模拟）", reversible=False,
                                    details={"商户": row["merchant"], "原因": str(payload["reason"]), "影响": "仅提交审核，不自动拒付、退款、销卡，也不代表已确认欺诈", "环境": "纯虚拟模拟"})
        if request.operation is Operation.TRANSFER:
            source_ref = str(payload.get("source_account_ref", ""))
            account = self.accounts.get(source_ref)
            recipient_id = (prepared.opaque_reference.split(":", 1)[1] if prepared else "")
            recipient = self.recipients.get(recipient_id)
            if account is None or recipient is None:
                raise ValueError("模拟账户或收款人引用无效")
            amount = Decimal(str(payload["amount"]))
            fee = Decimal("0.00")
            execute_at = str(payload.get("execution_time", "now"))
            scheduled = execute_at != "now"
            if scheduled:
                run_date = date.fromisoformat(execute_at)
                if run_date <= self._today():
                    raise ValueError("定时转账日期必须晚于今天")
                recurrence = str(payload.get("frequency", "once"))
                if recurrence not in {"once", "weekly", "monthly", "yearly"}:
                    raise ValueError("不支持的周期频率")
                end_condition = str(payload.get("end_condition", "单次执行"))
                if recurrence != "once" and not payload.get("end_date") and not payload.get("occurrences"):
                    raise ValueError("周期转账必须提供结束日期或执行次数")
                if recurrence != "once" and payload.get("end_date") and date.fromisoformat(str(payload["end_date"])) < run_date:
                    raise ValueError("周期结束日期不能早于首次执行日期")
            arrival = "创建后等待计划时间（模拟）" if scheduled else "即时（模拟）"
            return OperationPreview(
                summary=(
                    ("登记模拟定时转账（确认时不扣款）：" if scheduled else "模拟转账：")
                    + f"从{account['label']}尾号 {account['last4']} 向{recipient['name']}"
                    f"尾号 {recipient['last4']} 转账 {self._money(amount)} CNY。"
                ),
                amount=self._money(amount), currency="CNY",
                payer_account_last4=account["last4"],
                payee_name=recipient["name"], payee_account_last4=recipient["last4"],
                fee=self._money(fee), execute_at=execute_at,
                expected_arrival=arrival, reversible=scheduled,
                details={
                    "用途": str(payload.get("purpose", "")), "频率": str(payload.get("frequency", "仅一次")),
                    "结束条件": str(payload.get("end_condition", "单次执行")),
                    "周期执行次数": str(payload.get("occurrences", "1")),
                    "取消方式": "在本地虚拟计划列表中取消；此演示不会后台自动扣款。" if scheduled else "不适用",
                    "当前可用余额": self._money(account["balance"] - self.reservations.get(source_ref, Decimal("0"))),
                    "当日剩余额度": self._money(max(Decimal("0"), Decimal(str(self.config.daily_transfer_limit)) - self._daily_transfer_total())),
                    "环境": "纯虚拟模拟",
                },
            )

        if request.operation is Operation.SCHEDULE_MANAGEMENT:
            schedule = self.scheduled_transfers.get(str(payload["schedule_id"]))
            if not schedule:
                raise ValueError("模拟定时转账任务不存在")
            action = str(payload["action"])
            if action == "cancel_schedule":
                return OperationPreview(
                    summary=f"取消模拟定时转账 {schedule['schedule_id']}（{schedule['status']}）。",
                    amount=self._money(schedule["amount"]), currency="CNY",
                    execute_at="确认后（模拟）", reversible=False,
                    details={"执行日期": schedule["execute_at"], "频率": schedule["frequency"], "已执行次数": str(schedule["runs"]), "环境": "纯虚拟模拟"},
                )
            if action == "run_schedule":
                recipient = self.recipients[schedule["recipient_ref"]]
                account = self.accounts[schedule["source_account_ref"]]
                return OperationPreview(
                    summary=f"手动模拟触发计划 {schedule['schedule_id']}：从尾号 {account['last4']} 向{recipient['name']}尾号 {recipient['last4']} 支付 {self._money(schedule['amount'])} CNY。",
                    amount=self._money(schedule["amount"]), currency="CNY", payer_account_last4=account["last4"],
                    payee_name=recipient["name"], payee_account_last4=recipient["last4"],
                    fee="0.00", execute_at="用户触发后（模拟）", expected_arrival="即时（模拟）", reversible=False,
                    details={"计划日期": schedule["execute_at"], "频率": schedule["frequency"], "用途": schedule["purpose"],
                             "当前可用余额": self._money(account["balance"] - self.reservations.get(schedule["source_account_ref"], Decimal("0"))), "环境": "纯虚拟模拟"},
                )
            raise ValueError("不支持的模拟计划操作")

        if request.operation is Operation.AA_REQUEST:
            action = str(payload["action"])
            amount = Decimal(str(payload["amount"]))
            if action == "send_request":
                recipient_id = prepared.opaque_reference.split(":", 1)[1] if prepared else ""
                recipient = self.recipients.get(recipient_id)
                if not recipient:
                    raise ValueError("模拟收款人引用无效")
                return OperationPreview(
                    summary=f"创建 AA 付款请求：向{recipient['name']}尾号 {recipient['last4']} 请求 {self._money(amount)} CNY。不会扣取对方资金。",
                    amount=self._money(amount), currency="CNY", payee_name=recipient["name"],
                    payee_account_last4=recipient["last4"], execute_at="确认后发送请求（模拟）",
                    reversible=True, details={"用途": str(payload.get("purpose", "")), "资金影响": "仅发送付款请求；对方必须自行同意付款。", "环境": "纯虚拟模拟"},
                )
            request_record = self.aa_requests.get(str(payload.get("request_id", "")))
            if not request_record or request_record["status"] != "pending":
                raise ValueError("模拟 AA 请求不存在或已处理")
            recipient = self.recipients[request_record["from_recipient_ref"]]
            return OperationPreview(
                summary=f"支付模拟 AA 请求 {request_record['request_id']}：从活期尾号 1234 向{recipient['name']}尾号 {recipient['last4']} 支付 {self._money(amount)} CNY。",
                amount=self._money(amount), currency="CNY", payer_account_last4="1234",
                payee_name=recipient["name"], payee_account_last4=recipient["last4"],
                fee="0.00", execute_at="确认后（模拟）", expected_arrival="即时（模拟）", reversible=False,
                details={"请求状态": "待支付", "资金影响": "只在当前虚拟用户再次确认后扣款；不会代替他人授权。", "环境": "纯虚拟模拟"},
            )

        if request.operation is Operation.INVESTMENT:
            if payload.get("action") == "buy_stock":
                quote = self.stock_market.quote(payload)
                return OperationPreview(
                    summary=f"一键模拟买入{quote['stock_name']}（{quote['stock_code']}）{quote['shares']} 股。",
                    amount=quote["amount"], currency="CNY", payer_account_last4="1234", fee=quote["fee"],
                    execute_at="点击后立即模拟成交", expected_arrival="立即记入虚构股票持仓", reversible=False,
                    details={"股票代码": quote["stock_code"], "股票名称": quote["stock_name"],
                             "买入股数": str(quote["shares"]), "每股样例价": quote["price"],
                             "含费扣款合计": quote["total"], "报价说明": "固定虚构样例价，不是实时行情",
                             "资金来源": "虚构活期账户尾号 1234", "环境": "纯虚构教学成交"},
                )
            if payload.get("action", "subscribe") not in {"subscribe", "redeem", "settle_order"}:
                raise ValueError("不支持的投资操作")
            if payload.get("action") == "settle_order":
                order = self.investment_orders.get(str(payload["order_id"]))
                if not order or order["status"] != "processing":
                    raise ValueError("模拟投资订单不存在或已结算")
                return OperationPreview(
                    summary=f"手动模拟结算投资订单 {order['order_id']}，{order['action']} {order['product_code']}。",
                    amount=order["amount"], currency="CNY", fee=order["fee"], execute_at="确认后（模拟）", reversible=False,
                    details={"当前状态": "processing", "结算影响": "申购转为可用持仓；赎回金额扣费后记入虚拟余额", "环境": "纯虚拟模拟"},
                )
            product = self.products[str(payload["product_code"])]
            amount = Decimal(str(payload["amount"]))
            fee = (amount * product["fee_rate"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            action = str(payload.get("action", "subscribe"))
            action_label = "模拟赎回" if action == "redeem" else "模拟申购"
            return OperationPreview(
                summary=f"{action_label}：{product['name']}（{payload['product_code']}）",
                amount=self._money(amount), currency="CNY", payer_account_last4="1234",
                fee=self._money(fee), execute_at="确认后（模拟）",
                expected_arrival="申购 1 个工作日内确认；赎回预计 2 个工作日（模拟）", reversible=False,
                details={
                    "操作": "赎回" if action == "redeem" else "申购",
                    "当前模拟净值": str(product["nav"]), "报价时间": product["updated_at"],
                    "风险等级": product["risk"].name,
                    "模拟手续费": self._money(fee),
                    "锁定期": product["lock_period"],
                    "本金风险": product["principal_risk"],
                    "流动性": product["liquidity"], "提前退出费用": product["early_exit_fee"],
                    "数据说明": "虚构产品资料，不代表真实报价",
                },
            )

        if request.operation is Operation.CARD_MANAGEMENT:
            action = str(payload["action"])
            if action not in {"apply", "replace", "temporary_lock", "unlock", "permanent_loss", "limit_change", "set_controls"}:
                raise ValueError("不支持的卡片操作")
            card = self.cards.get(str(payload.get("card_ref", "")))
            if action != "apply" and not card:
                raise ValueError("模拟卡片引用无效")
            if card and action in {"unlock", "temporary_lock"} and card["status"] not in {"active", "temporarily_locked"}:
                raise ValueError("永久挂失或未制卡的卡片不能通过临时锁定/解锁恢复")
            if action == "apply":
                return OperationPreview(
                    summary="申请一张新的虚拟演示卡。",
                    execute_at="确认后登记申请（模拟）", reversible=True,
                    details={"费用": "0 CNY（虚构）", "申请进度": "确认后进入本地模拟制卡队列", "环境": "纯虚拟模拟"},
                )
            labels = {
                "temporary_lock": "临时锁定", "unlock": "解锁", "permanent_loss": "永久挂失",
                "limit_change": "额度调整", "set_controls": "交易限制设置",
                "apply": "申请模拟卡片", "replace": "申请模拟补卡",
            }
            details = {"当前状态": card["status"], "操作": labels.get(action, action), "环境": "纯虚拟模拟"}
            if action == "limit_change":
                details["当前额度"] = self._money(card["limit"])
                details["申请额度"] = self._money(Decimal(str(payload["new_limit"])))
                details["审批说明"] = "额度申请可能获批或被拒；此模拟不保证结果。"
            if action == "set_controls":
                details["当前限制"] = str(card["controls"])
                target_controls = dict(card["controls"])
                target_controls.update(payload.get("controls", {}))
                details["目标限制"] = str(target_controls)
            if action == "permanent_loss":
                details.update({"立即生效": "模拟挂失确认后立即停用", "自动扣款": "商户或周期付款可能仍需单独处理", "补卡费用": "0 CNY（虚构）", "预计送达": "2 个模拟工作日", "操作后果": "原卡不可恢复"})
            if action == "replace":
                details.update({"补卡费用": "0 CNY（虚构）", "预计送达": "2 个模拟工作日", "原卡状态": card["status"]})
            return OperationPreview(
                summary=f"模拟卡片操作：尾号 {card['last4']}，{labels.get(action, action)}。",
                currency="CNY", payer_account_last4=card["last4"],
                amount=self._money(Decimal(str(payload.get("new_limit", "0")))) if action == "limit_change" else None,
                execute_at="确认后（模拟）", reversible=action in {"temporary_lock", "set_controls"},
                details=details,
            )

        if request.operation is Operation.SUBSCRIPTION:
            sub = self.subscriptions.get(str(payload["merchant_ref"]))
            if not sub:
                raise ValueError("模拟商户引用无效")
            target = str(payload["cancellation_target"])
            target_labels = {
                "merchant_subscription": "商户订阅合同",
                "bank_debit": "银行代扣授权",
                "create_bank_debit": "创建或恢复银行代扣授权",
                "card_recurring_payment": "银行卡周期付款",
                "refund_request": "退款申请",
                "set_reminder": "续费提醒设置",
                "set_price_alert": "价格变化提醒设置",
            }
            target = str(payload["cancellation_target"])
            if target not in target_labels:
                raise ValueError("不支持的订阅处理对象")
            return OperationPreview(
                summary=f"模拟处理{target_labels.get(target, target)}：{sub['name']}。",
                amount=self._money(sub["amount"]), currency="CNY",
                execute_at="确认后（模拟）", reversible=target != "refund_request",
                details={
                    "商户": sub["name"], "金额和周期": f"{self._money(sub['amount'])} CNY / {sub['period']}",
                    "下次扣费": sub["next_charge"], "处理对象": target_labels.get(target, target),
                    "当前合同状态": sub["merchant_contract"],
                    "当前代扣状态": sub["bank_debit"],
                    "最近扣费": sub["last_charge"], "已付权益到期": sub["paid_entitlement_until"],
                    "违约或权益影响": sub["penalty"], "是否可恢复": str(sub["recoverable"]),
                    "生效时间": "确认后（模拟）", "是否退款": "否；退款需单独申请" if target != "refund_request" else "仅提交退款申请，不代表已退款",
                    "权益说明": "仅影响所选模拟对象；停止代扣不等于商户合同终止，也不代表已退款。",
                    "环境": "纯虚拟模拟",
                },
            )

        if request.operation is Operation.CATEGORY_CORRECTION:
            row = next((item for item in self.transactions if item["id"] == str(payload["transaction_id"])), None)
            if not row:
                raise ValueError("模拟交易记录不存在")
            return OperationPreview(
                summary=f"修改模拟交易 {row['id']} 的消费分类：{row['category']} → {payload['category']}。",
                execute_at="确认后（模拟）", reversible=True,
                details={"商户": row["merchant"], "金额": row["amount"], "数据说明": "仅更正本地演示分类，不改变交易或退款状态。", "环境": "纯虚拟模拟"},
            )

        if request.operation is Operation.CROSS_SCENARIO:
            if payload.get("action") not in {"create_plan", "activate_budget", "cancel_plan", "execute_purchase"}:
                raise ValueError("不支持的计划操作")
            if payload.get("action") == "create_plan":
                start_date = date.fromisoformat(str(payload["birthday_date"]))
                budget = Decimal(str(payload["reserve_amount"]))
                flower = Decimal(str(payload["flower_budget"]))
                cake = Decimal(str(payload["cake_budget"]))
                if start_date <= self._today() or flower <= 0 or cake <= 0 or flower + cake > budget:
                    raise ValueError("计划日期须在未来，且商品分配不能超出预留总额")
                if not set(self._items(payload["goods"])) <= {"鲜花", "蛋糕"}:
                    raise ValueError("该虚拟采购目录只支持鲜花和蛋糕")
                reserve_start = str(payload.get("reserve_start_date", "now"))
                if reserve_start != "now" and date.fromisoformat(reserve_start) > start_date - timedelta(days=2):
                    raise ValueError("预算预留日期不能晚于采购日期")
                if str(payload["source_account_ref"]) not in self.accounts:
                    raise ValueError("虚拟资金来源账户无效")
                mode = str(payload["authorization_mode"])
                if mode not in {"once", "range", "continuous"}:
                    raise ValueError("授权类型无效")
                return OperationPreview(
                    summary=f"创建跨场景虚拟计划“{payload['plan_name']}”：预算 {self._money(budget)} CNY，纪念日为 {start_date.isoformat()}，计划于 {(start_date - timedelta(days=2)).isoformat()} 采购鲜花和蛋糕。",
                    amount=self._money(budget), currency="CNY", payer_account_last4=self.accounts[str(payload["source_account_ref"])]["last4"],
                    execute_at="创建计划；商品不会立即购买", reversible=True,
                    details={
                        "计划步骤": "确认生日日期→预留预算→到期前两天核对订单→按授权模式办理",
                        "预算分配": f"鲜花 {self._money(flower)}；蛋糕 {self._money(cake)}",
                        "释放条件": str(payload["release_condition"]), "商户范围": ", ".join(self._items(payload["merchant_allowlist"])),
                        "商品范围": ", ".join(self._items(payload["goods"])), "授权模式": mode,
                        "范围上限": f"最多 {self._money(Decimal(str(payload.get('max_amount', budget))))} CNY；指定商户和商品；最多 {payload.get('max_count', 1)} 次；有效至 {payload.get('valid_until', start_date.isoformat())}",
                        "配送位置": str(payload["delivery_details"]), "通知规则": str(payload["notification_rule"]),
                        "预留开始": "确认后立即预留" if reserve_start == "now" else reserve_start,
                        "商品偏好": str(payload.get("preferences", "演示标准款；无额外偏好，确认此摘要即选择该款")),
                        "资金影响": "仅作本地虚拟预算预留；未来开始的预算需手动模拟激活，未触发前不会消费。",
                        "环境": "纯虚拟模拟",
                    },
                )
            plan = self.cross_plans.get(str(payload.get("plan_id", "")))
            if not plan or plan["status"] not in {"scheduled", "ready", "partial_success"}:
                raise ValueError("模拟计划不存在或当前不可执行")
            if payload.get("action") == "activate_budget":
                return OperationPreview(
                    summary=f"手动模拟激活计划 {plan['plan_id']} 的预算预留。", amount=self._money(plan["reserve_amount"]),
                    currency="CNY", payer_account_last4=self.accounts[plan["source_account_ref"]]["last4"],
                    execute_at="确认后（模拟）", reversible=True,
                    details={"预留开始": str(plan.get("reserve_start_date", "now")), "释放条件": plan["release_condition"], "环境": "纯虚拟模拟"},
                )
            if payload.get("action") == "cancel_plan":
                return OperationPreview(
                    summary=f"取消生日虚拟计划 {plan['plan_id']}，释放未使用的 {self._money(Decimal(str(plan.get('reserved_remaining', plan['reserve_amount']))))} CNY 预算预留。",
                    amount=self._money(Decimal(str(plan.get("reserved_remaining", plan["reserve_amount"])))), currency="CNY",
                    execute_at="确认后（模拟）", reversible=False,
                    details={"计划状态": plan["status"], "释放条件": plan["release_condition"], "环境": "纯虚拟模拟"},
                )
            orders = payload.get("orders") or []
            total = sum((Decimal(str(item["amount"])) for item in orders), Decimal("0")) if orders else Decimal(str(payload.get("purchase_total", "0")))
            merchant = ", ".join(str(item.get("merchant", "")) for item in orders) if orders else str(payload.get("merchant", ""))
            goods = ", ".join(f"{item.get('goods')} {self._money(Decimal(str(item.get('amount', '0'))))} CNY" for item in orders) if orders else str(payload.get("goods", ""))
            return OperationPreview(
                summary=f"模拟计划采购：{plan['plan_name']}；商户 {merchant}；总价 {self._money(total)} CNY。",
                amount=self._money(total), currency="CNY", payer_account_last4=self.accounts[plan["source_account_ref"]]["last4"],
                execute_at="用户触发后（模拟）", expected_arrival="计划日期前送达（模拟）", reversible=False,
                details={"商品及分项金额": goods, "配送位置": plan["delivery_details"],
                         "授权范围": plan["authorization_mode"], "总额上限": self._money(plan["reserve_amount"]),
                         "配送费": "0.00 CNY（模拟）", "其他费用": "0.00 CNY（模拟）",
                         "退款规则": "虚构商户规则：下单前可取消，下单后请联系虚拟商户", "环境": "纯虚拟模拟"},
            )

        return OperationPreview(summary="模拟操作预览", details={"环境": "纯虚拟模拟"})

    def _within_purchase_scope(self, plan: dict[str, Any], orders: list[dict[str, Any]], simulate_due: bool) -> bool:
        caps = {"鲜花": Decimal(str(plan["flower_budget"])), "蛋糕": Decimal(str(plan["cake_budget"]))}
        singles = {name: Decimal(str(plan.get("spent_by_goods", {}).get(name, "0"))) for name in caps}
        total = Decimal("0")
        allowed_goods = set(self._items(plan["goods"]))
        for order in orders:
            amount = Decimal(str(order["amount"]))
            requested = {item for item in re.split(r"[和及与,，、\s]+", str(order["goods"])) if item}
            if not amount.is_finite() or amount <= 0 or not requested or not requested <= allowed_goods:
                return False
            if str(order["merchant"]) not in self._items(plan["merchant_allowlist"]):
                return False
            if len(requested) == 1:
                singles[next(iter(requested))] += amount
            total += amount
        remaining = Decimal(str(plan.get("reserved_remaining", plan["reserve_amount"])))
        due = self._today().isoformat() >= str(plan["purchase_date"]) or simulate_due
        return bool(
            plan["status"] in {"scheduled", "ready"} and orders and due
            and total <= Decimal(str(plan.get("max_amount", plan["reserve_amount"])))
            and total <= remaining and total + Decimal(str(plan.get("total_spent", "0"))) <= sum(caps.values(), Decimal("0"))
            and all(singles[name] <= cap for name, cap in caps.items())
            and total <= self.reservations.get(plan["source_account_ref"], Decimal("0"))
            and plan["purchase_count"] < int(plan.get("max_count", 1))
            and self._today() <= date.fromisoformat(str(plan["valid_until"]))
        )

    def is_pre_authorized(self, request: OperationRequest, preview: OperationPreview) -> bool:
        # Only an explicitly confirmed plan range may execute without a fresh confirmation.
        # Investment (including one-click stock buying) always goes through the confirm gate.
        if request.operation is not Operation.CROSS_SCENARIO or request.payload.get("action") != "execute_purchase":
            return False
        plan = self.cross_plans.get(str(request.payload.get("plan_id", "")))
        if not plan or plan.get("authorization_mode") not in {"range", "continuous"}:
            return False
        orders = request.payload.get("orders") or [{"amount": request.payload["purchase_total"],
                                                  "merchant": request.payload["merchant"], "goods": request.payload["goods"]}]
        return self._within_purchase_scope(plan, orders, bool(request.payload.get("simulate_due")))

    # ExecutionTool
    def execute(
        self,
        request: OperationRequest,
        prepared: PreparedOperation | None,
        preview: OperationPreview,
        idempotency_key: str,
    ) -> ExecutionResult:
        if idempotency_key in self._executions:
            return self._executions[idempotency_key]
        payload = request.payload
        reference = f"SIM-TXN-{uuid.uuid4().hex[:10].upper()}"
        if request.operation is Operation.BUDGET:
            key = f"{payload['month']}:{payload['label']}"
            self.budget_labels[key] = {"label": str(payload["label"]), "month": str(payload["month"]), "amount": str(payload["amount"])}
            result = ExecutionResult(ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.REMINDER:
            self.reminders.append({"reminder_id": f"SIM-REM-{len(self.reminders) + 1:04d}", "date": str(payload["reminder_date"]), "text": str(payload["reminder_text"]), "status": "scheduled"})
            result = ExecutionResult(ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.DISPUTE:
            dispute_id = f"SIM-DSP-{len(self.disputes) + 1:04d}"
            self.disputes[dispute_id] = {"dispute_id": dispute_id, "transaction_id": payload["transaction_id"], "status": "submitted_for_review", "refund_status": "not_refunded"}
            result = ExecutionResult(ExecutionState.PROCESSING, dispute_id)
        elif request.operation is Operation.TRANSFER:
            source_ref = str(payload["source_account_ref"])
            recipient_id = prepared.opaque_reference.split(":", 1)[1] if prepared else ""
            amount = Decimal(str(payload["amount"]))
            account = self.accounts[source_ref]
            if str(payload.get("execution_time", "now")) != "now":
                schedule_id = f"SIM-SCH-{len(self.scheduled_transfers) + 1:04d}"
                schedule = {
                    "schedule_id": schedule_id, "source_account_ref": source_ref,
                    "recipient_ref": recipient_id, "amount": amount, "currency": "CNY",
                    "execute_at": str(payload["execution_time"]), "frequency": str(payload.get("frequency", "once")),
                    "end_date": str(payload.get("end_date", "")),
                    "occurrences": int(payload["occurrences"]) if str(payload.get("occurrences", "")).isdigit() else 0,
                    "purpose": str(payload.get("purpose", "")), "status": "scheduled", "runs": 0,
                    "created_at": self._now(), "anchor_day": date.fromisoformat(str(payload["execution_time"])).day,
                }
                self.scheduled_transfers[schedule_id] = schedule
                result = ExecutionResult(ExecutionState.SCHEDULED, schedule_id, details={
                    "schedule_id": schedule_id, "execute_at": schedule["execute_at"],
                    "frequency": schedule["frequency"], "status": "scheduled",
                    "note": "只登记虚拟任务，当前没有扣款；演示不运行后台调度器。",
                })
            elif account["balance"] - self.reservations.get(source_ref, Decimal("0")) < amount:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_INSUFFICIENT_FUNDS")
            else:
                account["balance"] -= amount
                self.recipients[recipient_id]["balance"] += amount
                self._record_transfer(source_ref, amount)
                tx_id = f"SIM-TX-{len(self.transactions) + 101}"
                self.transactions.insert(0, {
                    "id": tx_id, "reference": reference, "date": self._today().isoformat(),
                    "merchant": f"转账给{self.recipients[recipient_id]['name']}（虚构）",
                    "amount": self._money(amount), "category": "转账", "status": "已入账（模拟）", "anomaly_features": [],
                })
                result = ExecutionResult(ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.SCHEDULE_MANAGEMENT:
            schedule = self.scheduled_transfers[str(payload["schedule_id"])]
            action = str(payload["action"])
            if action == "cancel_schedule" and schedule["status"] == "scheduled":
                schedule["status"] = "cancelled"
                result = ExecutionResult(ExecutionState.SUCCESS, schedule["schedule_id"])
            elif action == "run_schedule":
                account = self.accounts[schedule["source_account_ref"]]
                amount = schedule["amount"]
                recipient = self.recipients[schedule["recipient_ref"]]
                reserved = self.reservations.get(schedule["source_account_ref"], Decimal("0"))
                due = self._today().isoformat() >= schedule["execute_at"] or bool(payload.get("simulate_due"))
                if schedule["status"] != "scheduled" or not due or account["balance"] - reserved < amount:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_SCHEDULE_NOT_RUNNABLE")
                else:
                    account["balance"] -= amount
                    recipient["balance"] += amount
                    self._record_transfer(schedule["source_account_ref"], amount)
                    schedule["runs"] += 1
                    max_runs = schedule["occurrences"]
                    if schedule["frequency"] == "once" or (max_runs > 0 and schedule["runs"] >= max_runs):
                        schedule["status"] = "completed"
                    else:
                        next_date = date.fromisoformat(schedule["execute_at"])
                        if schedule["frequency"] == "weekly":
                            next_date += timedelta(days=7)
                        elif schedule["frequency"] == "yearly":
                            next_year = next_date.year + 1
                            next_date = next_date.replace(year=next_year, day=min(schedule["anchor_day"], calendar.monthrange(next_year, next_date.month)[1]))
                        else:
                            next_month = next_date.month % 12 + 1
                            next_year = next_date.year + (1 if next_date.month == 12 else 0)
                            next_date = next_date.replace(year=next_year, month=next_month, day=min(schedule["anchor_day"], calendar.monthrange(next_year, next_month)[1]))
                        schedule["execute_at"] = next_date.isoformat()
                        if schedule["end_date"] and schedule["execute_at"] > schedule["end_date"]:
                            schedule["status"] = "completed"
                    self.transactions.insert(0, {"id": f"SIM-TX-{len(self.transactions) + 101}", "reference": reference, "date": self._today().isoformat(), "merchant": f"转账给{recipient['name']}（虚构）", "amount": self._money(amount), "category": "转账", "status": "已入账（模拟）", "anomaly_features": []})
                    result = ExecutionResult(ExecutionState.SUCCESS, reference)
            else:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_SCHEDULE_NOT_CANCELLABLE")
        elif request.operation is Operation.AA_REQUEST:
            action = str(payload["action"])
            amount = Decimal(str(payload["amount"]))
            if action == "send_request":
                recipient_id = prepared.opaque_reference.split(":", 1)[1] if prepared else ""
                request_id = f"SIM-AA-{self._next_request:04d}"
                self._next_request += 1
                self.aa_requests[request_id] = {
                    "request_id": request_id, "from_recipient_ref": recipient_id,
                    "amount": amount, "currency": "CNY", "purpose": str(payload.get("purpose", "")),
                    "status": "pending",
                }
                result = ExecutionResult(ExecutionState.SUCCESS, request_id, details={"request_id": request_id, "note": "仅发送付款请求；未扣取对方资金。"})
            else:
                payment = self.aa_requests[str(payload["request_id"])]
                source = self.accounts["checking"]
                recipient_id = payment["from_recipient_ref"]
                if payment["status"] != "pending" or amount != payment["amount"]:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_REQUEST_CHANGED")
                elif source["balance"] - self.reservations.get("checking", Decimal("0")) < amount:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_INSUFFICIENT_FUNDS")
                else:
                    source["balance"] -= amount
                    self.recipients[recipient_id]["balance"] += amount
                    self._record_transfer("checking", amount)
                    payment["status"] = "paid"
                    self.transactions.insert(0, {"id": f"SIM-TX-{len(self.transactions) + 101}", "reference": reference, "date": self._today().isoformat(), "merchant": f"AA 付款给{self.recipients[recipient_id]['name']}（虚构）", "amount": self._money(amount), "category": "转账", "status": "已入账（模拟）", "anomaly_features": []})
                    result = ExecutionResult(ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.INVESTMENT and payload.get("action") == "buy_stock":
            quote = self.stock_market.quote(payload)
            account = self.accounts["checking"]
            total = Decimal(quote["total"])
            if account["balance"] - self.reservations.get("checking", Decimal("0")) < total:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_INSUFFICIENT_FUNDS")
            elif preview.amount != quote["amount"] or preview.fee != quote["fee"]:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_STOCK_QUOTE_CHANGED")
            else:
                account["balance"] -= total
                order = self.stock_market.fill(quote)
                self.transactions.insert(0, {"id": f"SIM-TX-{len(self.transactions) + 101}",
                    "reference": order["order_id"], "date": self._today().isoformat(),
                    "merchant": f"买入{quote['stock_name']}（虚构股票）", "amount": quote["total"],
                    "category": "股票投资", "status": "已成交（模拟）", "anomaly_features": []})
                result = ExecutionResult(ExecutionState.SUCCESS, order["order_id"], details={"stock_order": order})
        elif request.operation is Operation.INVESTMENT:
            amount = Decimal(str(payload["amount"]))
            account = self.accounts["checking"]
            code = str(payload["product_code"])
            action = str(payload.get("action", "subscribe"))
            product = self.products[code]
            fee = (amount * product["fee_rate"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if action == "settle_order":
                order = self.investment_orders[str(payload["order_id"])]
                if order["status"] != "processing":
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_ORDER_ALREADY_SETTLED")
                else:
                    if order["action"] == "subscribe":
                        self.holdings[code] = self.holdings.get(code, Decimal("0")) + Decimal(order["amount"])
                    else:
                        account["balance"] += Decimal(order["amount"]) - Decimal(order["fee"])
                    order["status"] = "settled"
                    order["settled_at"] = self._now()
                    result = ExecutionResult(ExecutionState.SUCCESS, order["order_id"], details={"status": "settled"})
                    self._executions[order["idempotency_key"]] = result
            elif action == "redeem":
                if self.holdings.get(code, Decimal("0")) < amount:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_INSUFFICIENT_HOLDINGS")
                else:
                    self.holdings[code] -= amount
                    order_id = f"SIM-INV-{len(self.investment_orders) + 1:04d}"
                    self.investment_orders[order_id] = {"order_id": order_id, "action": "redeem", "product_code": code, "amount": self._money(amount), "fee": self._money(fee), "status": "processing", "estimated_settlement": "2 个模拟工作日", "idempotency_key": idempotency_key}
                    result = ExecutionResult(ExecutionState.PROCESSING, order_id, details={"order_id": order_id, "status": "processing"})
            elif account["balance"] - self.reservations.get("checking", Decimal("0")) < amount + fee:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_INSUFFICIENT_FUNDS")
            else:
                account["balance"] -= amount + fee
                order_id = f"SIM-INV-{len(self.investment_orders) + 1:04d}"
                self.investment_orders[order_id] = {"order_id": order_id, "action": "subscribe", "product_code": code, "amount": self._money(amount), "fee": self._money(fee), "status": "processing", "estimated_settlement": "1 个模拟工作日", "idempotency_key": idempotency_key}
                result = ExecutionResult(ExecutionState.PROCESSING, order_id, details={"order_id": order_id, "status": "processing"})
        elif request.operation is Operation.CARD_MANAGEMENT:
            action = str(payload["action"])
            card = self.cards.get(str(payload.get("card_ref", "")))
            if action == "apply":
                new_ref = f"sim-card-{self._next_card}"
                last4 = f"{5100 + self._next_card:04d}"
                self._next_card += 1
                self.cards[new_ref] = {
                    "last4": last4, "status": "application_pending", "limit": Decimal("5000.00"),
                    "controls": {"domestic": True, "international": False, "online": True, "offline": True, "card_not_present": True},
                    "application_status": "已受理，等待虚拟制卡", "replacement_status": None,
                }
                result = ExecutionResult(ExecutionState.PROCESSING, reference, details={"card_last4": last4, "application_status": "已受理"})
                self._executions[idempotency_key] = result
                return result
            if not card:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_CARD_NOT_FOUND")
                self._executions[idempotency_key] = result
                return result
            if action == "temporary_lock":
                card["status"] = "temporarily_locked"
            elif action == "unlock":
                card["status"] = "active"
            elif action == "permanent_loss":
                card["status"] = "permanently_blocked"
            elif action == "limit_change":
                card["limit_request_status"] = "submitted_for_simulated_review"
                card["requested_limit"] = Decimal(str(payload.get("new_limit", "0")))
            elif action == "set_controls":
                for key, value in payload.get("controls", {}).items():
                    if key in card["controls"] and isinstance(value, bool):
                        card["controls"][key] = value
            elif action == "replace":
                card["replacement_status"] = "submitted"
                card["status"] = "permanently_blocked"
            result = ExecutionResult(ExecutionState.PROCESSING if action in {"limit_change", "replace"} else ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.SUBSCRIPTION:
            sub = self.subscriptions[str(payload["merchant_ref"])]
            target = str(payload["cancellation_target"])
            if target == "merchant_subscription":
                sub["merchant_contract"] = "cancelled"
            elif target == "bank_debit":
                sub["bank_debit"] = "stopped"
            elif target == "create_bank_debit":
                sub["bank_debit"] = "active"
            elif target == "card_recurring_payment":
                sub["card_recurring_payment"] = "stopped"
            elif target == "set_reminder":
                sub["reminder"] = bool(payload.get("enabled", True))
            elif target == "set_price_alert":
                sub["price_alert"] = bool(payload.get("enabled", True))
            else:
                sub["refund_status"] = "request_submitted"
            result = ExecutionResult(ExecutionState.PROCESSING if target == "refund_request" else ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.CATEGORY_CORRECTION:
            row = next((item for item in self.transactions if item["id"] == str(payload["transaction_id"])), None)
            if not row:
                result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_TRANSACTION_NOT_FOUND")
            else:
                row["category"] = str(payload["category"])
                result = ExecutionResult(ExecutionState.SUCCESS, reference)
        elif request.operation is Operation.CROSS_SCENARIO:
            if payload.get("action") == "create_plan":
                plan_id = f"SIM-PLAN-{len(self.cross_plans) + 1:04d}"
                source_ref = str(payload["source_account_ref"])
                amount = Decimal(str(payload["reserve_amount"]))
                reserved = self.reservations.get(source_ref, Decimal("0"))
                reserve_start = str(payload.get("reserve_start_date", "now"))
                active = reserve_start == "now" or date.fromisoformat(reserve_start) <= self._today()
                if self.accounts[source_ref]["balance"] - reserved < amount:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_INSUFFICIENT_FUNDS")
                else:
                    plan = {key: value for key, value in payload.items()}
                    plan["merchant_allowlist"] = self._items(payload["merchant_allowlist"])
                    plan["goods"] = self._items(payload["goods"])
                    plan.update({"plan_id": plan_id, "status": "scheduled", "birthday_date": str(payload["birthday_date"]),
                                 "purchase_date": (date.fromisoformat(str(payload["birthday_date"])) - timedelta(days=2)).isoformat(),
                                 "reserve_amount": amount, "reserved_remaining": amount if active else Decimal("0"), "reserve_active": active,
                                 "source_account_ref": source_ref, "purchase_count": 0,
                                 "total_spent": "0.00", "spent_by_goods": {"鲜花": "0.00", "蛋糕": "0.00"},
                                 "steps": [
                                     {"step_id": "calendar_date", "description": "确认用户提供的虚构纪念日日期", "status": "completed"},
                                     {"step_id": "budget_reserve", "description": "本地虚拟预算预留", "status": "completed" if active else "scheduled"},
                    *[{"step_id": f"purchase_{index + 1}", "description": f"采购{good}", "status": "scheduled"} for index, good in enumerate(self._items(payload["goods"]))],
                                     {"step_id": "notify", "description": str(payload["notification_rule"]), "status": "scheduled"},
                                 ],
                                 "created_at": self._now(), "idempotency_key": idempotency_key})
                    self.cross_plans[plan_id] = plan
                    if active:
                        self.reservations[source_ref] = reserved + amount
                    result = ExecutionResult(ExecutionState.SCHEDULED, plan_id, details={"plan_id": plan_id, "status": "scheduled", "reserved_amount": self._money(amount if active else Decimal("0"))})
            elif payload.get("action") == "activate_budget":
                plan = self.cross_plans[str(payload["plan_id"])]
                source = plan["source_account_ref"]
                amount = plan["reserve_amount"]
                due = plan.get("reserve_start_date", "now") == "now" or str(plan["reserve_start_date"]) <= self._today().isoformat() or bool(payload.get("simulate_due"))
                if plan.get("reserve_active") or not due or self.accounts[source]["balance"] - self.reservations.get(source, Decimal("0")) < amount:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_BUDGET_NOT_ACTIVATABLE")
                else:
                    self.reservations[source] = self.reservations.get(source, Decimal("0")) + amount
                    plan["reserved_remaining"] = amount
                    plan["reserve_active"] = True
                    next(step for step in plan["steps"] if step["step_id"] == "budget_reserve")["status"] = "completed"
                    result = ExecutionResult(ExecutionState.SUCCESS, plan["plan_id"])
            elif payload.get("action") == "cancel_plan":
                plan = self.cross_plans[str(payload["plan_id"])]
                if plan["status"] == "completed":
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_PLAN_ALREADY_COMPLETED")
                else:
                    held = Decimal(str(plan.get("reserved_remaining", plan["reserve_amount"])))
                    self.reservations[plan["source_account_ref"]] = max(Decimal("0"), self.reservations.get(plan["source_account_ref"], Decimal("0")) - held)
                    if self.reservations[plan["source_account_ref"]] == 0:
                        self.reservations.pop(plan["source_account_ref"], None)
                    plan["status"] = "cancelled"
                    plan["reserved_remaining"] = Decimal("0")
                    result = ExecutionResult(ExecutionState.SUCCESS, plan["plan_id"])
            else:
                plan = self.cross_plans[str(payload["plan_id"])]
                orders = payload.get("orders") or [{
                    "amount": payload.get("purchase_total", "0"),
                    "merchant": payload.get("merchant", ""), "goods": payload.get("goods", ""),
                }]
                total = sum((Decimal(str(item["amount"])) for item in orders), Decimal("0"))
                reservation = self.reservations.get(plan["source_account_ref"], Decimal("0"))
                within_bounds = self._within_purchase_scope(plan, orders, bool(payload.get("simulate_due")))
                if not within_bounds:
                    result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_SCOPE_BOUNDARY_EXCEEDED")
                else:
                    order_results = []
                    spent = Decimal("0")
                    previous_total = Decimal(str(plan.get("total_spent", "0")))
                    held = Decimal(str(plan.get("reserved_remaining", plan["reserve_amount"])))
                    plan["status"] = "processing"
                    self._executions[idempotency_key] = ExecutionResult(ExecutionState.PROCESSING, reference)
                    for index, item in enumerate(orders, start=1):
                        self._order_audit(request, plan["plan_id"], f"order-{index}", "tool_called")
                        merchant = str(item["merchant"])
                        goods = str(item["goods"])
                        amount = Decimal(str(item["amount"]))
                        if "缺货" in merchant or item.get("simulate_failure"):
                            order_result = {"step_id": f"order-{index}", "goods": goods, "merchant": merchant, "status": "failed", "error": "SIM_VIRTUAL_OUT_OF_STOCK", "charged": "0.00"}
                        else:
                            self.accounts[plan["source_account_ref"]]["balance"] -= amount
                            spent += amount
                            requested = {name for name in re.split(r"[和及与,，、\s]+", goods) if name}
                            if len(requested) == 1:
                                name = next(iter(requested))
                                plan["spent_by_goods"][name] = self._money(Decimal(plan["spent_by_goods"][name]) + amount)
                            plan["total_spent"] = self._money(previous_total + spent)
                            order_ref = f"SIM-ORDER-{uuid.uuid4().hex[:10].upper()}"
                            order_result = {"step_id": f"order-{index}", "goods": goods, "merchant": merchant, "status": "success", "charged": self._money(amount), "reference": order_ref}
                            self.transactions.insert(0, {"id": f"SIM-TX-{len(self.transactions) + 101}", "reference": order_ref, "date": self._today().isoformat(), "merchant": merchant, "amount": self._money(amount), "category": "购物", "status": "已入账（模拟）", "anomaly_features": []})
                        order_results.append(order_result)
                        plan["purchase_results"] = list(order_results)
                        plan["reserved_remaining"] = held - spent
                        self.reservations[plan["source_account_ref"]] = reservation - spent
                        self._executions[idempotency_key] = ExecutionResult(ExecutionState.PROCESSING, reference, details={"orders": list(order_results)})
                        self._order_audit(request, plan["plan_id"], f"order-{index}", "status_checked", order_result["status"])
                        for step in plan["steps"]:
                            if step["description"] == f"采购{goods}":
                                step["status"] = order_result["status"]
                                step.update({k: v for k, v in order_result.items() if k in {"reference", "error"}})
                    plan["purchase_count"] += 1
                    plan["purchase_results"] = order_results
                    plan["spent_amount"] = self._money(spent)
                    plan.setdefault("notifications", []).append({"message": "本次虚拟采购结果已记录，请核对逐项状态。", "timestamp": self._now()})
                    for step in plan["steps"]:
                        if step["step_id"] == "notify":
                            step["status"] = "completed"
                    failures = any(item["status"] == "failed" for item in order_results)
                    if failures:
                        plan["status"] = "partial_success"
                        plan["reserved_remaining"] = max(Decimal("0"), held - spent)
                        self.reservations[plan["source_account_ref"]] = max(Decimal("0"), reservation - spent)
                        result = ExecutionResult(ExecutionState.PARTIAL, reference, details={
                            "plan_id": plan["plan_id"], "status": "partial_success", "orders": order_results,
                            "spent_amount": self._money(spent), "reserved_for_unfinished_steps": self._money(plan["reserved_remaining"]),
                            "next_step": "核对失败步骤；本地模拟未自动重试。可取消计划释放剩余预留。",
                        })
                    else:
                        remaining = held - spent
                        if plan["authorization_mode"] == "continuous" and plan["purchase_count"] < int(plan.get("max_count", 1)) and remaining > 0:
                            plan["reserved_remaining"] = remaining
                            self.reservations[plan["source_account_ref"]] = reservation - spent
                            plan["status"] = "scheduled"
                            result = ExecutionResult(ExecutionState.SUCCESS, reference, details={"plan_id": plan["plan_id"], "status": "scheduled", "orders": order_results, "remaining_reserved": self._money(remaining)})
                            self._executions[idempotency_key] = result
                            return result
                        self.reservations[plan["source_account_ref"]] = max(Decimal("0"), reservation - held)
                        if self.reservations[plan["source_account_ref"]] == 0:
                            self.reservations.pop(plan["source_account_ref"], None)
                        plan["reserved_remaining"] = Decimal("0")
                        plan["status"] = "completed"
                        plan["purchase_reference"] = reference
                        result = ExecutionResult(ExecutionState.SUCCESS, reference, details={"plan_id": plan["plan_id"], "status": "completed", "orders": order_results, "spent_amount": self._money(spent), "unused_reserve_released": self._money(held - spent)})
        else:
            result = ExecutionResult(ExecutionState.FAILED, error_code="SIM_OPERATION_UNSUPPORTED")
        self._executions[idempotency_key] = result
        return result

    # StatusQueryTool
    def lookup(self, idempotency_key: str) -> ExecutionResult | None:
        return self._executions.get(idempotency_key)

    # ManualReviewTool
    def submit(self, task_id, request, prepared, preview, assessment) -> ReviewResult:
        self._reviews += 1
        ref = f"SIM-REVIEW-{self._reviews:04d}"
        self._review_status[ref] = "pending"
        return ReviewResult(True, ref)

    def cancel_review(self, task_id: str, review_reference: str) -> ReviewResult:
        if self._review_status.get(review_reference) != "pending":
            return ReviewResult(False, review_reference, "SIM_REVIEW_NOT_CANCELLABLE")
        self._review_status[review_reference] = "cancelled"
        return ReviewResult(True, review_reference)

    def get_status(self, review_reference: str) -> str:
        return self._review_status.get(review_reference, "unknown")

    def simulate_review_rejection(self, review_reference: str) -> bool:
        if self._review_status.get(review_reference) != "pending":
            return False
        self._review_status[review_reference] = "declined"
        return True


def build_demo_agent(audit_path: str = "data/demo-audit.jsonl") -> tuple[BankingAgent, SimulatedBank]:
    config = virtual_config()
    bank = SimulatedBank(config)
    audit = AuditLog(audit_path, b"virtual-demo-only-audit-key")
    bank.audit = audit
    registry = ToolRegistry(
        authentication=bank,
        risk_assessment=bank,
        account_query=bank,
        public_info=bank,
        transfer_resolver=bank,
        preview=bank,
        execution=bank,
        status_query=bank,
        manual_review=bank,
    )
    return BankingAgent(config, registry, audit), bank


class DemoConversation:
    """Small rule-based Chinese interface for trying the orchestration core locally."""

    def __init__(self, agent: BankingAgent, bank: SimulatedBank, *,
                 user_id: str = "demo-user", session_id: str = "demo-session",
                 channel: str = "app", auth_challenge: AuthChallenge | None = None):
        self.agent = agent
        self.bank = bank
        self.user_id = user_id
        self.session_id = session_id
        self.channel = channel
        self.auth_challenge = auth_challenge or AuthChallenge()
        self.pending_task_id: str | None = None
        self.workflow = None

    def _audit_event(self, action: str, detail: str) -> None:
        try:
            self.agent.audit.append({
                "channel": self.channel,
                "session_id": self.agent.audit.hash_identifier(self.session_id),
                "user_id_hash": self.agent.audit.hash_identifier(self.user_id),
                "plan_id": "virtual-session", "step_id": action, "action": action,
                "operation_type": Operation.PUBLIC_INFO.value, "risk_level": "R1",
                "policy_version": self.agent.config.policy_version or "virtual-demo",
                "authentication_level": "enhanced" if self.channel == "app" else "standard",
                "request_summary": detail,
            })
        except AuditError:
            pass

    def set_channel(self, channel: str) -> str:
        if channel not in {"app", "im", "other"}:
            raise ValueError("不支持的演示渠道")
        if channel == self.channel:
            return ""
        self.channel = channel
        self._audit_event("channel_changed", f"channel={channel}")
        if self.pending_task_id:
            task = self.agent.switch_channel(self.pending_task_id, self.user_id, self.session_id, channel)
            return self._render(task)
        return ""

    def _strengthen_authentication(self, code: str) -> str:
        if self.channel == "app":
            return "当前已在 APP 安全界面（增强认证），无需重复认证。"
        if not self.auth_challenge.verify(code):
            return ("演示安全码错误或已过期，认证等级未提升。请查看页面「安全认证」显示的最新安全码。")
        self.channel = "app"
        self._audit_event("auth_strengthened", "demo security code accepted")
        if self.pending_task_id:
            return self._render(self.agent.switch_channel(self.pending_task_id, self.user_id, self.session_id, "app"))
        return "已通过演示安全码完成增强认证，当前渠道为 APP 安全界面。" 

    def handle(self, message: str) -> str:
        try:
            return self._handle(message)
        except AuditError:
            return "【纯虚拟模拟】审计不可用，自动流程已暂停。请查询现有任务状态，不要重复提交。"
        except (ValueError, KeyError, ArithmeticError):
            return "【纯虚拟模拟】信息或工具状态不一致，未确认操作完成。请核对当前任务状态。"

    def _handle(self, message: str) -> str:
        text = message.strip()
        if not text:
            return "请说出你想演示的操作。"
        if re.search(r"\b(?:USD|EUR|GBP|JPY|HKD)\b|美元|欧元|英镑|日元|港币", text, re.I):
            return "【纯虚拟模拟】当前演示仅支持 CNY。其他币种请求未提交，不会自动换汇或改成 CNY。"
        if text == "模拟审核拒绝" and self.pending_task_id:
            task = self.agent.get_task(self.pending_task_id, self.user_id, self.session_id)
            if task.state is not TaskState.UNDER_REVIEW or self.channel != "app":
                return "请在 APP 演示渠道对等待审核的任务模拟审核拒绝。"
            self.agent.audit.append({"action": "simulated_review_decision", "plan_id": task.task_id,
                                     "step_id": "manual-review", "result": "declined", "operation_type": task.request.operation.value,
                                     "user_id_hash": self.agent.audit.hash_identifier(self.user_id), "channel": self.channel})
            self.bank.simulate_review_rejection(task.transaction_reference)
            return self._render(self.agent.refresh_review(task.task_id, self.user_id, self.session_id))

        if text.startswith("组合计划：") or text.startswith("组合计划:"):
            if self.pending_task_id:
                pending = self.agent.get_task(self.pending_task_id, self.user_id, self.session_id)
                if pending.state in {TaskState.AWAITING_CONFIRMATION, TaskState.AWAITING_INFORMATION, TaskState.AWAITING_AUTHENTICATION}:
                    return "请先完成或取消当前待处理操作，再创建组合计划。"
            from .workflow import BankingWorkflow
            parts = [part.strip() for part in re.split(r"[；;]", re.split(r"[:：]", text, maxsplit=1)[1]) if part.strip()]
            try:
                requests = [self._parse(part) for part in parts]
            except (ValueError, ArithmeticError):
                return "组合计划日期或金额格式无效；未执行任何步骤。"
            if not requests or any(request is None for request in requests):
                return "组合计划包含无法识别的步骤，请按 README 中的示例填写。"
            try:
                self.workflow = BankingWorkflow(self.agent, requests)
                task = self.workflow.advance(self.channel)
            except (ValueError, AuditError):
                return "组合计划未创建或执行；请核对步骤并检查本地审计。"
            if task:
                self.pending_task_id = task.task_id
            return self._format_data(self.workflow.public_dict()) + ("\n" + self._render(task) if task else "")
        if text in {"组合计划状态", "继续组合计划", "取消组合计划"}:
            if self.workflow is None:
                return "当前没有组合计划。"
            try:
                if text == "取消组合计划":
                    self.workflow.cancel()
                elif text == "继续组合计划":
                    task = self.workflow.advance(self.channel)
                    if task:
                        self.pending_task_id = task.task_id
                        return self._format_data(self.workflow.public_dict()) + "\n" + self._render(task)
                return self._format_data(self.workflow.public_dict())
            except AuditError:
                return "审计不可用，组合计划已暂停。"

        match = re.fullmatch(r"(?:认证|安全认证)\s*([0-9]{6})", text)
        if match:
            return self._strengthen_authentication(match.group(1))
        if text in {"切换到APP", "切换到应用", "切换到安全界面"}:
            return ("认证增强不能只靠一句指令完成：请在页面「安全认证」中输入演示安全码，"
                    "或直接发送“认证 123456”这样的六位码。")
        if text in {"切换到即时通讯", "切换到IM", "切换到聊天渠道"}:
            rendered = self.set_channel("im")
            return rendered or ("当前虚拟渠道已降级为即时通讯（标准认证）；办理资金类操作需要"
                                "回到 APP 安全界面并输入演示安全码。")
        if text in {"授权读取模拟日历", "允许读取模拟日历"}:
            try:
                self.agent.audit.append({
                    "channel": self.channel,
                    "session_id": self.agent.audit.hash_identifier(self.session_id),
                    "plan_id": "virtual-calendar", "step_id": "calendar-access",
                    "user_id_hash": self.agent.audit.hash_identifier(self.user_id),
                    "operation_type": Operation.CROSS_SCENARIO_QUERY.value,
                    "action": "calendar_access_authorized", "risk_level": "R1",
                    "policy_version": self.agent.config.policy_version or "virtual-demo",
                    "authentication_level": "standard", "request_summary": "user authorized synthetic calendar access",
                    "confirmation": {"required": True, "received": True, "summary_hash": "synthetic-calendar-scope", "expires_at": None},
                    "tool_name": "virtual_calendar", "result": "success",
                })
            except AuditError:
                return "【纯虚拟模拟】审计日志不可用，模拟日历访问授权未生效。"
            self.bank.calendar_access_granted = True
            return "【纯虚拟模拟】已记录你对本地虚构日历的访问授权。查看事件后仍需你确认哪一个日期对应目标纪念日。"

        task = None
        if self.pending_task_id:
            task = self.agent.get_task(self.pending_task_id, self.user_id, self.session_id)
            if text in {"取消", "取消操作", "不做了"}:
                task = self.agent.cancel(task.task_id, self.user_id, self.session_id)
                return self._render(task)
            if task.state.value == "AWAITING_CONFIRMATION":
                if text == task.confirmation_phrase:
                    task = self.agent.confirm(
                        task.task_id, self.user_id, self.session_id, text
                    )
                    return self._render(task)
                return f"请先核对模拟摘要。确认请原样回复“{task.confirmation_phrase}”；取消请回复“取消”。"

            if task.state.value == "AWAITING_INFORMATION":
                if task.choices:
                    choice_match = re.search(r"(?:第)?\s*([1-9])\s*(?:个|号)?", text)
                    if choice_match:
                        index = int(choice_match.group(1)) - 1
                        task = self.agent.choose_recipient(
                            task.task_id, self.user_id, self.session_id, index
                        )
                        return self._render(task)
                    return "请回复收款人选项序号（例如：2）。"
                fields = self._supplement(task.missing_information, text)
                if fields:
                    task = self.agent.provide_information(
                        task.task_id, self.user_id, self.session_id, fields
                    )
                    return self._render(task)

            if task.state.value == "AWAITING_AUTHENTICATION":
                task = self.agent.resume_authentication(task.task_id, self.user_id, self.session_id)
                return self._render(task)

            if task.state.value in {"UNKNOWN", "PROCESSING"} and text in {"查状态", "查询状态", "刷新状态"}:
                task = self.agent.refresh_unknown(task.task_id, self.user_id, self.session_id)
                return self._render(task)
            if text in {"任务状态", "查看当前任务"}:
                return self._render(task)

        try:
            request = self._parse(text)
        except (ValueError, ArithmeticError):
            return "【纯虚拟模拟】日期或金额格式无效，操作未提交。"
        if request is None:
            return (
                "这是纯虚拟演示。可试：查余额、看账单、现在转账100元给小明用途午餐、"
                "现在转账50元给张伟用途AA、锁卡尾号9012、查询网点信息。"
            )
        task = self.agent.submit(request)
        self.pending_task_id = task.task_id
        return self._render(task)

    def _parse(self, text: str) -> OperationRequest | None:
        low = text.lower()
        amount = self._extract_amount(text)
        codes = re.findall(r"P-[A-Z]+-SIM", text.upper())
        dates = self._extract_dates(text)

        stock_code = re.search(r"XH\d{3}", text.upper())
        if "股票持仓" in text or "股票订单" in text:
            return self._request(Operation.INVESTMENT_QUERY, text, {"action": "stock_holdings"})
        if stock_code and any(word in text for word in ("一键买入", "买入", "购买")):
            shares_match = re.search(r"(-?\d+(?:\.\d+)?)\s*股", text)
            shares = shares_match.group(1) if shares_match else "100"
            payload = {"action": "buy_stock", "stock_code": stock_code.group(0) if stock_code else "",
                       "shares": shares, "currency": "CNY", "source_account_ref": "checking"}
            if stock_code and stock_code.group(0) in self.bank.stock_market.catalog and shares.isdigit():
                payload["amount"] = self.bank._money(self.bank.stock_market.catalog[stock_code.group(0)]["price"] * int(shares))
            return self._request(Operation.INVESTMENT, text, payload)
        if "股票" in text and any(word in text for word in ("行情", "列表", "目录", "查看", "购买", "一键")):
            return self._request(Operation.INVESTMENT_QUERY, text, {"action": "stocks"})

        if "预算标签" in text:
            if any(word in text for word in ("查看", "查询", "列表")):
                return self._request(Operation.PREFERENCE_QUERY, text, {})
            label = re.search(r"预算标签\s*[:：]?\s*([^\d，,。\s]+)", text)
            return self._request(Operation.BUDGET, text, {"label": label.group(1) if label else "", "amount": amount or "", "month": self.bank._today().strftime("%Y-%m")})
        if "创建提醒" in text:
            return self._request(Operation.REMINDER, text, {"reminder_date": dates[0] if dates else "", "reminder_text": self._extract_named_value(text, ("内容",)) or ""})

        if "争议" in text:
            if any(word in text for word in ("查询", "列表", "进度", "查看")):
                return self._request(Operation.DISPUTE_QUERY, text, {})
            tx = re.search(r"SIM-TX-\d+", text.upper())
            return self._request(Operation.DISPUTE, text, {"transaction_id": tx.group(0) if tx else "", "reason": self._extract_named_value(text, ("原因", "理由")) or ""})

        if any(word in text for word in ("查看AA", "AA请求列表", "付款请求列表")):
            return self._request(Operation.AA_QUERY, text, {})
        if "支付AA请求" in text or "支付请求" in text:
            request_id = re.search(r"SIM-AA-\d+", text.upper())
            request_id = request_id.group(0) if request_id else ""
            record = self.bank.aa_requests.get(request_id)
            return self._request(Operation.AA_REQUEST, text, {
                "action": "pay_request", "request_id": request_id,
                "amount": amount or (self.bank._money(record["amount"]) if record else ""), "currency": "CNY",
            })
        if any(word in text for word in ("AA收款", "请求付款", "AA付款请求")):
            recipient = self._find_recipient(text)
            purpose = self._extract_named_value(text, ("用途", "备注")) or "AA 分摊（模拟）"
            return self._request(Operation.AA_REQUEST, text, {
                "action": "send_request", "recipient_query": recipient or "", "amount": amount or "",
                "currency": "CNY" if amount else "", "purpose": purpose,
            })

        if any(word in text for word in ("定时任务列表", "查看定时转账", "周期转账列表", "查看提醒", "提醒列表")):
            return self._request(Operation.SCHEDULE_QUERY, text, {})
        schedule_id_match = re.search(r"SIM-SCH-\d+", text.upper())
        if schedule_id_match and any(word in text for word in ("取消计划", "取消定时", "停止周期")):
            return self._request(Operation.SCHEDULE_MANAGEMENT, text, {"action": "cancel_schedule", "schedule_id": schedule_id_match.group(0)})
        if schedule_id_match and any(word in text for word in ("模拟运行计划", "运行计划", "触发计划")):
            return self._request(Operation.SCHEDULE_MANAGEMENT, text, {"action": "run_schedule", "schedule_id": schedule_id_match.group(0), "simulate_due": "模拟" in text})

        if any(word in text for word in ("跨场景计划列表", "查看生日计划", "查看已创建计划")):
            return self._request(Operation.CROSS_SCENARIO_QUERY, text, {})
        if "查看模拟日历" in text or "查询模拟日历" in text:
            return self._request(Operation.CROSS_SCENARIO_QUERY, text, {"action": "calendar"})
        if "查看模拟日历" in text or "查询模拟日历" in text:
            return self._request(Operation.CROSS_SCENARIO_QUERY, text, {"action": "calendar"})
        plan_id_match = re.search(r"SIM-PLAN-\d+", text.upper())
        if plan_id_match and "模拟预留计划" in text:
            return self._request(Operation.CROSS_SCENARIO, text, {"action": "activate_budget", "plan_id": plan_id_match.group(0), "simulate_due": True})
        if plan_id_match and "取消计划" in text:
            return self._request(Operation.CROSS_SCENARIO, text, {"action": "cancel_plan", "plan_id": plan_id_match.group(0)})
        if plan_id_match and any(word in text for word in ("执行计划", "采购计划", "模拟采购")):
            merchants = next((name for name in ("虚拟花店", "虚拟蛋糕店", "云朵虚构商城", "虚拟缺货商户") if name in text), "")
            goods_match = re.search(r"商品\s*[:：]?\s*(.*?)(?=总价|金额|[，,。]|$)", text)
            goods = goods_match.group(1).strip() if goods_match else ""
            orders = []
            for pattern, good_name in ((r"(?:鲜花|花束)", "鲜花"), (r"蛋糕", "蛋糕")):
                for match in re.finditer(rf"{pattern}\s*(\d+(?:\.\d{{1,2}})?)\s*(?:元|块)\s*[@由]?\s*(虚拟花店|虚拟蛋糕店|云朵虚构商城|虚拟缺货商户)", text):
                    orders.append({"goods": good_name, "amount": match.group(1), "merchant": match.group(2)})
            total = str(sum((Decimal(item["amount"]) for item in orders), Decimal("0"))) if orders else (amount or "")
            return self._request(Operation.CROSS_SCENARIO, text, {
                "action": "execute_purchase", "plan_id": plan_id_match.group(0),
                "purchase_total": total, "merchant": merchants, "goods": goods,
                "simulate_due": "模拟到期" in text,
                "orders": orders if orders else None,
            })
        if any(word in text for word in ("生日计划", "生日采购计划", "创建跨场景计划")):
            flower_cake_amounts = re.findall(r"(?:鲜花|花束|蛋糕)[^\d]{0,8}(\d+(?:\.\d{1,2})?)\s*(?:元|块)?", text)
            nums = re.findall(r"(?<!\d)(\d+(?:\.\d{1,2})?)\s*(?:元|块|人民币|CNY)", text, re.I)
            flower = flower_cake_amounts[0] if len(flower_cake_amounts) > 0 else (nums[1] if len(nums) > 1 else "")
            cake = flower_cake_amounts[1] if len(flower_cake_amounts) > 1 else (nums[2] if len(nums) > 2 else "")
            date_value = dates[0] if dates else ""
            mode = "continuous" if "持续授权" in text else ("range" if "范围授权" in text else "once")
            shops = [name for name in ("虚拟花店", "虚拟蛋糕店", "云朵虚构商城", "虚拟缺货商户") if name in text]
            goods = [name for name in ("鲜花", "花束", "蛋糕") if name in text]
            max_amount_match = re.search(r"(?:最大金额|上限)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块|CNY)?", text, re.I)
            count = re.search(r"(?:最多执行|执行次数|执行)\s*[:：]?\s*(\d+)\s*次", text)
            valid_until = re.search(r"有效至\s*[:：]?\s*(20\d{2}-\d{1,2}-\d{1,2})", text)
            reserve_start = re.search(r"预留开始\s*[:：]?\s*(20\d{2}-\d{1,2}-\d{1,2})", text)
            if "花束" in goods and "鲜花" not in goods:
                goods[goods.index("花束")] = "鲜花"
            payload = {
                "action": "create_plan", "plan_name": "生日鲜花和蛋糕计划",
                "birthday_date": date_value, "reserve_amount": amount or (str(Decimal(flower or "0") + Decimal(cake or "0")) if flower and cake else ""),
                "source_account_ref": "savings" if "储蓄" in text or "5678" in text else ("checking" if "活期" in text or "1234" in text else ""),
                "release_condition": "生日当天结束后释放未使用预留", "flower_budget": flower, "cake_budget": cake,
                "merchant_allowlist": shops, "goods": sorted(set(goods)), "authorization_mode": mode,
                "delivery_details": self._extract_named_value(text, ("虚拟配送点", "配送点", "收货点")) or "",
                "notification_rule": "执行日前提醒并通知结果", "max_amount": max_amount_match.group(1) if max_amount_match else (amount or ""),
                "max_count": int(count.group(1)) if count else 1, "valid_until": valid_until.group(1) if valid_until else date_value,
                "reserve_start_date": reserve_start.group(1) if reserve_start else (date_value[:8] + "01" if "当月预留" in text and date_value else "now"),
                "preferences": self._extract_named_value(text, ("偏好",)) or "演示标准款；无额外偏好",
            }
            if mode == "once":
                payload["max_amount"] = payload["max_amount"] or payload["reserve_amount"]
            return self._request(Operation.CROSS_SCENARIO, text, payload)

        if "余额" in text:
            return self._request(Operation.BALANCE, text, {})
        if any(word in text for word in ("分类改为", "更正分类", "修改分类")):
            tx = re.search(r"SIM-TX-\d+", text.upper())
            category = self._extract_named_value(text, ("分类改为", "更正分类", "修改分类"))
            if category:
                category = re.split(r"[，,。\s]", category, maxsplit=1)[0]
            return self._request(Operation.CATEGORY_CORRECTION, text, {"transaction_id": tx.group(0) if tx else "", "category": category or ""})
        if any(word in text for word in ("账单", "消费分析", "支出分析", "消费分类", "年度消费")):
            period = {"month": "2026-10"}
            if dates and len(dates) > 1:
                period = {"start_date": dates[0], "end_date": dates[1]}
            elif re.search(r"(20\d{2})年(\d{1,2})月", text):
                match = re.search(r"(20\d{2})年(\d{1,2})月", text)
                period = {"month": f"{match.group(1)}-{int(match.group(2)):02d}"}
            elif re.search(r"20\d{2}年", text):
                period = {"year": int(re.search(r"(20\d{2})年", text).group(1))}
            elif re.search(r"20\d{2}-\d{1,2}", text):
                match = re.search(r"(20\d{2}-\d{1,2})", text)
                period = {"month": match.group(1)}
            return self._request(Operation.BILL_ANALYSIS, text, period)
        if "交易记录" in text or "流水" in text or "交易状态" in text:
            reference = re.search(r"SIM-TXN-[A-Z0-9]+", text.upper())
            tx = next((m for m in self.bank.transactions if m["id"] in text), {})
            return self._request(Operation.TRANSACTION_QUERY, text, {
                "search": tx.get("id", ""), "transaction_reference": reference.group(0) if reference else "",
            })

        if "转账" in text or "转给" in text or "汇给" in text:
            recipient_query = self._find_recipient(text)
            if not recipient_query:
                recipient_match = re.search(r"(?:给|转给|汇给)\s*([\u4e00-\u9fff]{2,5})", text)
                recipient_query = recipient_match.group(1) if recipient_match else None
            purpose = self._extract_named_value(text, ("用途", "备注", "原因")) or ""
            if "储蓄" in text or "尾号5678" in text:
                source_ref = "savings"
            elif "活期" in text or "尾号1234" in text:
                source_ref = "checking"
            else:
                source_ref = ""
            if any(word in text for word in ("现在", "立即", "马上")):
                execution = "now"
            else:
                execution = dates[0] if dates else ""
            frequency = "monthly" if "每月" in text else ("weekly" if "每周" in text else ("yearly" if "每年" in text else "once"))
            count_match = re.search(r"(?:执行|转)\s*(\d+)\s*次", text)
            end_date_match = re.search(r"(?:截至|结束于|到期日)\s*(20\d{2}-\d{1,2}-\d{1,2})", text)
            payload = {
                "source_account_ref": source_ref, "currency": "CNY" if amount or any(x in text for x in ("人民币", "CNY")) else "",
                "execution_time": execution, "purpose": purpose,
                "frequency": frequency, "end_condition": f"共执行 {count_match.group(1)} 次" if count_match else (end_date_match.group(1) if end_date_match else "单次执行"),
                "occurrences": int(count_match.group(1)) if count_match else (1 if frequency == "once" else ""),
                "end_date": end_date_match.group(1) if end_date_match else "",
            }
            if amount:
                payload["amount"] = amount
            if recipient_query:
                payload["recipient_query"] = recipient_query
            return self._request(Operation.TRANSFER, text, payload)

        if ((any(word in text for word in ("投资产品", "理财产品", "产品目录", "产品比较", "比较产品", "筛选产品", "风险测评", "投资建议"))
             or (codes and any(word in text for word in ("比较", "筛选"))))
                and not any(word in text for word in ("申购", "赎回"))):
            if "持仓" in text:
                return self._request(Operation.INVESTMENT_QUERY, text, {"action": "holdings"})
            order = re.search(r"SIM-INV-\d+", text.upper())
            if order:
                return self._request(Operation.INVESTMENT_QUERY, text, {"action": "order_status", "order_id": order.group(0)})
            profile = {
                "region": self._extract_named_value(text, ("地区",)), "goal": self._extract_named_value(text, ("目标",)),
                "horizon": self._extract_named_value(text, ("期限",)), "liquidity": self._extract_named_value(text, ("流动性",)),
                "risk_tolerance": (re.search(r"R[0-4]", text.upper()).group(0) if re.search(r"R[0-4]", text.upper()) else ""),
                "experience": self._extract_named_value(text, ("经验",)), "amount": amount or self._extract_named_value(text, ("金额",)),
                "suitability_restrictions": self._extract_named_value(text, ("适当性限制", "限制")),
            }
            return self._request(Operation.INVESTMENT_QUERY, text, {"action": "products", "compare_codes": codes if "比较" in text else [], "max_risk": profile["risk_tolerance"] or "R4", "profile": profile})

        order_match = re.search(r"SIM-INV-\d+", text.upper())
        if "模拟结算投资" in text and order_match:
            order = self.bank.investment_orders.get(order_match.group(0))
            if not order:
                return self._request(Operation.INVESTMENT_QUERY, text, {"action": "order_status", "order_id": order_match.group(0)})
            return self._request(Operation.INVESTMENT, text, {
                "action": "settle_order", "order_id": order["order_id"], "product_code": order["product_code"],
                "amount": order["amount"], "currency": "CNY",
            })
        if "投资进度" in text and order_match:
            return self._request(Operation.INVESTMENT_QUERY, text, {"action": "order_status", "order_id": order_match.group(0)})
        if "投资持仓" in text:
            return self._request(Operation.INVESTMENT_QUERY, text, {"action": "holdings"})
        if any(word in text for word in ("申购", "赎回", "投资")) and codes:
            profile = self.bank.investment_profiles.get(self.user_id, {})
            inline_risk = re.search(r"R[0-4]", text.upper())
            return self._request(Operation.INVESTMENT, text, {
                "action": "redeem" if "赎回" in text else "subscribe", "product_code": codes[0],
                "amount": amount or "", "currency": "CNY" if amount else "",
                "region": self._extract_named_value(text, ("地区",)) or profile.get("region", ""),
                "goal": self._extract_named_value(text, ("目标",)) or profile.get("goal", ""),
                "horizon": self._extract_named_value(text, ("期限",)) or profile.get("horizon", ""),
                "liquidity": self._extract_named_value(text, ("流动性",)) or profile.get("liquidity", ""),
                "profile_risk": inline_risk.group(0) if inline_risk else profile.get("risk_tolerance", ""),
                "experience": self._extract_named_value(text, ("经验",)) or profile.get("experience", ""),
                "investable_amount": profile.get("amount", amount or ""),
                "suitability_restrictions": self._extract_named_value(text, ("适当性限制", "限制")) or profile.get("suitability_restrictions", ""),
            })

        if any(word in text for word in ("卡片列表", "查看卡片", "卡片状态")):
            return self._request(Operation.CARD_QUERY, text, {"action": "list"})
        if "申请进度" in text or "补卡进度" in text:
            return self._request(Operation.CARD_QUERY, text, {"action": "application_status"})
        card_match = re.search(r"尾号\s*(\d{4})", text)
        card_ref = next((ref for ref, card in self.bank.cards.items() if card_match and card["last4"] == card_match.group(1)), "")
        if not card_ref and "演示卡" in text:
            card_ref = "sim-card-1"
        if "申请卡" in text or "申领卡" in text:
            return self._request(Operation.CARD_MANAGEMENT, text, {"action": "apply"})
        if "补卡" in text:
            return self._request(Operation.CARD_MANAGEMENT, text, {"card_ref": card_ref, "action": "replace"})
        if "额度" in text and any(word in text for word in ("调整", "提高", "降低", "申请")):
            new_limit = self._extract_amount(text) or ""
            return self._request(Operation.CARD_MANAGEMENT, text, {"card_ref": card_ref, "action": "limit_change", "new_limit": new_limit})
        if any(word in text for word in ("线上交易", "线下交易", "境内交易", "境外交易", "无卡交易", "设置交易限制")):
            controls: dict[str, bool] = {}
            for key, label in (("online", "线上交易"), ("offline", "线下交易"), ("domestic", "境内交易"), ("international", "境外交易"), ("card_not_present", "无卡交易")):
                if label in text:
                    controls[key] = not any(neg in text for neg in ("关闭", "禁用", "停止"))
            return self._request(Operation.CARD_MANAGEMENT, text, {"card_ref": card_ref, "action": "set_controls", "controls": controls})
        if "锁卡" in text or "解锁" in text or "挂失" in text:
            action = "temporary_lock" if "锁卡" in text else ("unlock" if "解锁" in text else "permanent_loss")
            return self._request(Operation.CARD_MANAGEMENT, text, {"card_ref": card_ref, "action": action})

        if any(word in text for word in ("订阅列表", "查看订阅", "订阅费用", "周期扣款", "续费情况")):
            return self._request(Operation.SUBSCRIPTION_QUERY, text, {})
        merchant_ref = "moon-music" if "月光音乐" in text else ("cloud-box" if "云朵盒子" in text else "")
        if "续费提醒" in text or "价格变化提醒" in text:
            target = "set_reminder" if "续费提醒" in text else "set_price_alert"
            return self._request(Operation.SUBSCRIPTION, text, {"merchant_ref": merchant_ref, "cancellation_target": target, "enabled": not any(w in text for w in ("关闭", "取消提醒"))})
        if any(word in text for word in ("取消订阅", "停止代扣", "创建代扣", "恢复代扣", "退款申请", "取消商户合同", "停止银行卡周期付款")):
            if "创建代扣" in text or "恢复代扣" in text:
                target = "create_bank_debit"
            elif "代扣" in text:
                target = "bank_debit"
            elif "银行卡" in text or "周期付款" in text:
                target = "card_recurring_payment"
            elif "退款" in text:
                target = "refund_request"
            else:
                target = "merchant_subscription"
            return self._request(Operation.SUBSCRIPTION, text, {"merchant_ref": merchant_ref, "cancellation_target": target})

        if any(word in text for word in ("网点", "营业时间", "客服", "召回", "应急", "虚拟银行", "演示规则")):
            return self._request(Operation.PUBLIC_INFO, text, {})
        return None

    @staticmethod
    def _extract_dates(text: str) -> list[str]:
        matches = re.findall(r"20\d{2}-\d{1,2}-\d{1,2}", text)
        if matches:
            return [date(*map(int, item.split("-"))).isoformat() for item in matches]
        zh = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
        if zh:
            return [date(int(zh.group(1)), int(zh.group(2)), int(zh.group(3))).isoformat()]
        if "明天" in text:
            return [(SimulatedBank._today() + timedelta(days=1)).isoformat()]
        return []

    @staticmethod
    def _extract_named_value(text: str, labels: tuple[str, ...]) -> str | None:
        for label in labels:
            match = re.search(re.escape(label) + r"\s*[:：]?\s*([^，,。；;]+)", text)
            if match:
                return match.group(1).strip()
        return None

    def _request(self, operation: Operation, text: str, payload: dict[str, Any]) -> OperationRequest:
        return OperationRequest(
            operation=operation,
            user_id=self.user_id,
            session_id=self.session_id,
            summary=text,
            payload=payload,
            channel=self.channel,
        )

    def _find_recipient(self, text: str) -> str | None:
        names = sorted({value["name"] for value in self.bank.recipients.values()}, key=len, reverse=True)
        name = next((name for name in names if name in text), None)
        if name:
            return name
        phone = re.search(r"(?<!\d)1\d{10}(?!\d)", text)
        if phone:
            return phone.group(0)
        return self._extract_named_value(text, ("历史备注", "收款备注"))

    _UNIT = r"(?:元|块|人民币|CNY)"
    _MALFORMED_AMOUNT = re.compile(
        r"(?:\d{1,3}(?:[,，]\d{3})+|\d+[eE][+-]?\d+|(?<!\d)\d{1,3}(?:\s\d{3})+|\d+\.\d{3,})"
        r"\s*" + _UNIT, re.I)
    _AMOUNT = re.compile(r"(?<![0-9A-Za-z.,．])([0-9]{1,9}(?:\.[0-9]{1,2})?)\s*" + _UNIT, re.I)

    @staticmethod
    def _extract_amount(text: str) -> str | None:
        if re.search(r"-\s*\d+(?:\.\d+)?\s*(?:元|块|人民币|CNY)", text, re.I):
            raise ValueError("金额不能为负数")
        if re.search(r"[０-９]+\s*(?:元|块|人民币|CNY)", text):
            raise ValueError("金额请使用半角阿拉伯数字")
        if DemoConversation._MALFORMED_AMOUNT.search(text):
            raise ValueError("金额格式无法识别（疑似科学计数法、千分位或三位以上小数），未提交任何操作")
        match = DemoConversation._AMOUNT.search(text)
        return match.group(1) if match else None

    def _supplement(self, missing: tuple[str, ...], text: str) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if "reason" in missing:
            fields["reason"] = self._extract_named_value(text, ("原因", "理由")) or text
        if "label" in missing:
            fields["label"] = self._extract_named_value(text, ("标签",)) or text
        if "reminder_date" in missing:
            dates = self._extract_dates(text)
            if dates:
                fields["reminder_date"] = dates[0]
        if "reminder_text" in missing:
            fields["reminder_text"] = self._extract_named_value(text, ("内容",)) or text
        if "transaction_id" in missing:
            match = re.search(r"SIM-TX-\d+", text.upper())
            if match:
                fields["transaction_id"] = match.group(0)
        if "purpose" in missing:
            fields["purpose"] = text
        if "positive amount" in missing:
            amount = self._extract_amount(text)
            if amount:
                fields["amount"] = amount
        if "region" in missing:
            fields["region"] = self._extract_named_value(text, ("地区",)) or ""
        if "goal" in missing:
            fields["goal"] = self._extract_named_value(text, ("目标",)) or ""
        if "horizon" in missing:
            fields["horizon"] = self._extract_named_value(text, ("期限",)) or ""
        if "liquidity" in missing:
            fields["liquidity"] = self._extract_named_value(text, ("流动性",)) or ""
        if "profile_risk" in missing:
            match = re.search(r"R[0-4]", text.upper())
            fields["profile_risk"] = match.group(0) if match else ""
        if "experience" in missing:
            fields["experience"] = self._extract_named_value(text, ("经验",)) or ""
        if "suitability_restrictions" in missing:
            fields["suitability_restrictions"] = self._extract_named_value(text, ("适当性限制", "限制")) or ""
        if "currency" in missing:
            fields["currency"] = "CNY" if any(x in text.upper() for x in ("元", "CNY", "人民币")) else text.strip().upper()
        if "source_account_ref" in missing:
            if "储蓄" in text or "5678" in text:
                fields["source_account_ref"] = "savings"
            elif "活期" in text or "1234" in text:
                fields["source_account_ref"] = "checking"
        if "execution_time" in missing and any(x in text for x in ("现在", "立即", "马上")):
            fields["execution_time"] = "now"
        elif "execution_time" in missing:
            dates = self._extract_dates(text)
            if dates:
                fields["execution_time"] = dates[0]
        if "recurrence_end_condition" in missing:
            count_match = re.search(r"(?:执行|转)\s*(\d+)\s*次", text)
            end_match = re.search(r"(?:截至|结束于|到期日)\s*(20\d{2}-\d{1,2}-\d{1,2})", text)
            if count_match:
                fields["occurrences"] = int(count_match.group(1))
                fields["end_condition"] = f"共执行 {count_match.group(1)} 次"
            elif end_match:
                fields["end_date"] = end_match.group(1)
                fields["end_condition"] = end_match.group(1)
        if "recipient" in missing:
            name = self._find_recipient(text)
            fields["recipient_query"] = name or text.strip()
        if "product_code" in missing:
            product = re.search(r"P-[A-Z]+-SIM", text.upper())
            if product:
                fields["product_code"] = product.group(0)
        if "merchant_ref" in missing:
            if "月光音乐" in text:
                fields["merchant_ref"] = "moon-music"
            elif "云朵盒子" in text:
                fields["merchant_ref"] = "cloud-box"
        if "cancellation_target" in missing:
            if "代扣" in text:
                fields["cancellation_target"] = "bank_debit"
            elif "银行卡" in text:
                fields["cancellation_target"] = "card_recurring_payment"
            elif "商户合同" in text:
                fields["cancellation_target"] = "merchant_subscription"
            elif "退款" in text:
                fields["cancellation_target"] = "refund_request"
        if "card_ref" in missing:
            match = re.search(r"(?:尾号\s*)?(\d{4})", text)
            ref = next((ref for ref, card in self.bank.cards.items() if match and card["last4"] == match.group(1)), None)
            if ref:
                fields["card_ref"] = ref
            elif "演示卡" in text:
                fields["card_ref"] = "sim-card-1"
        if "action" in missing:
            if "解锁" in text:
                fields["action"] = "unlock"
            elif "锁" in text:
                fields["action"] = "temporary_lock"
            elif "挂失" in text:
                fields["action"] = "permanent_loss"
            elif "补卡" in text:
                fields["action"] = "replace"
            elif "申请" in text:
                fields["action"] = "apply"
        plan_fields = {"birthday_date", "reserve_amount", "flower_budget", "cake_budget", "merchant_allowlist", "goods", "delivery_details", "max_amount", "valid_until", "max_count"}
        if plan_fields.intersection(missing):
            dates = self._extract_dates(text)
            nums = re.findall(r"(?<!\d)(\d+(?:\.\d{1,2})?)\s*(?:元|块|人民币|CNY)", text, re.I)
            if "birthday_date" in missing and dates:
                fields["birthday_date"] = dates[0]
            if "source_account_ref" in missing:
                if "储蓄" in text or "5678" in text:
                    fields["source_account_ref"] = "savings"
                elif "活期" in text or "1234" in text:
                    fields["source_account_ref"] = "checking"
            if "reserve_amount" in missing and nums:
                fields["reserve_amount"] = nums[0]
            if "flower_budget" in missing and nums:
                fields["flower_budget"] = nums[1] if len(nums) > 1 else nums[0]
            if "cake_budget" in missing and nums:
                fields["cake_budget"] = nums[2] if len(nums) > 2 else (nums[1] if len(nums) > 1 else nums[0])
            shops = [name for name in ("虚拟花店", "虚拟蛋糕店", "云朵虚构商城") if name in text]
            goods = [name for name in ("鲜花", "蛋糕") if name in text]
            if "merchant_allowlist" in missing and shops:
                fields["merchant_allowlist"] = shops
            if "goods" in missing and goods:
                fields["goods"] = goods
            delivery = self._extract_named_value(text, ("虚拟配送点", "配送点", "收货点"))
            if "delivery_details" in missing and delivery:
                fields["delivery_details"] = delivery
            if "valid_until" in missing and dates:
                fields["valid_until"] = dates[0]
            if "max_count" in missing:
                fields["max_count"] = 1
            if "max_amount" in missing and nums:
                max_match = re.search(r"(?:最大金额|上限)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)", text)
                fields["max_amount"] = max_match.group(1) if max_match else nums[0]
            if "plan_name" in missing and "生日" in text:
                fields["plan_name"] = "生日鲜花和蛋糕计划"
            if "release_condition" in missing:
                fields["release_condition"] = "生日当天结束后释放未使用预留"
            if "authorization_mode" in missing:
                fields["authorization_mode"] = "continuous" if "持续授权" in text else ("range" if "范围授权" in text else "once")
            if "merchant_allowlist" in missing and not shops:
                fields["merchant_allowlist"] = ["虚拟花店", "虚拟蛋糕店"]
            if "goods" in missing and not goods:
                fields["goods"] = ["鲜花", "蛋糕"]
            if "delivery_details" in missing and not delivery:
                fields["delivery_details"] = "模拟收货点A"
            if "notification_rule" in missing:
                fields["notification_rule"] = "执行日前提醒并通知结果"
            if "max_count" in missing:
                fields["max_count"] = 1
            if "valid_until" in missing and dates:
                fields["valid_until"] = dates[0]
        if "plan_id" in missing:
            plan = re.search(r"SIM-PLAN-\d+", text.upper())
            if plan:
                fields["plan_id"] = plan.group(0)
        return fields

    def _render(self, task) -> str:
        state = task.state.value
        prefix = "【纯虚拟模拟】"
        if state == "AWAITING_INFORMATION":
            if task.choices:
                options = "\n".join(
                    f"{index + 1}. {choice.display_name}，尾号 {choice.account_last4}"
                    for index, choice in enumerate(task.choices)
                )
                return f"{prefix}收款人匹配到多个模拟账户，请选择序号：\n{options}"
            fields = "、".join(task.missing_information) or "必要信息"
            return f"{prefix}还需要补充：{fields}。直接回复缺少的信息即可。"
        if state == "AWAITING_AUTHENTICATION":
            return f"{prefix}当前渠道的认证级别不足，操作已暂停。请切回 APP 安全界面重新认证；原确认不会沿用。任务编号：{task.task_id}。"
        if state == "AWAITING_CONFIRMATION":
            preview = task.preview.public_dict() if task.preview else {}
            lines = [prefix + "请核对操作摘要：", str(preview.get("summary", ""))]
            for key in ("amount", "currency", "fee", "execute_at", "expected_arrival", "reversible"):
                if preview.get(key) is not None:
                    lines.append(f"{key}: {preview[key]}")
            for key, value in (preview.get("details") or {}).items():
                lines.append(f"{key}: {value}")
            if task.risk and task.risk.flags:
                lines.append("安全提示：此操作存在需要核实的风险信号，请通过你已知的官方渠道独立核实后再决定。")
            lines.append(f"确认请输入“{task.confirmation_phrase}”；取消请输入“取消”。")
            return "\n".join(lines)
        if state == "UNDER_REVIEW":
            return f"{prefix}已提交模拟人工审核，状态为等待审核；编号：{task.transaction_reference}。这不代表审核通过。"
        if state == "SCHEDULED":
            details = task.result or {}
            reference = details.get("plan_id") or details.get("schedule_id") or task.transaction_reference
            return f"{prefix}已登记模拟计划，状态为 SCHEDULED；编号：{reference}。没有立即扣款，也没有后台调度器自动执行。"
        if state == "SUCCESS":
            if task.request.operation in {
                Operation.BALANCE, Operation.BILL_ANALYSIS, Operation.TRANSACTION_QUERY, Operation.DISPUTE_QUERY, Operation.PREFERENCE_QUERY,
                Operation.INVESTMENT_QUERY, Operation.CARD_QUERY, Operation.SUBSCRIPTION_QUERY,
                Operation.CROSS_SCENARIO_QUERY, Operation.SCHEDULE_QUERY, Operation.AA_QUERY,
            }:
                return prefix + "查询完成：\n" + self._format_data(task.result)
            if task.request.operation is Operation.PUBLIC_INFO:
                return prefix + str(task.result)
            if task.result:
                return f"{prefix}模拟操作成功。模拟交易编号：{task.transaction_reference}。\n" + self._format_data(task.result)
            return f"{prefix}模拟操作成功。模拟交易编号：{task.transaction_reference}。"
        if state == "PROCESSING":
            return f"{prefix}模拟操作处理中，尚未确认最终完成。服务编号：{task.transaction_reference or task.task_id}；任务编号：{task.task_id}。"
        if state == "PARTIAL_SUCCESS":
            import json
            details = json.dumps(task.result or {}, ensure_ascii=False, indent=2)
            return f"{prefix}模拟计划部分完成；未完成步骤未自动重试。逐步结果：\n{details}\n编号：{task.transaction_reference or task.task_id}。"
        if state == "UNKNOWN":
            return f"{prefix}状态暂不明确；请查询状态，不要重复提交。任务编号：{task.task_id}。"
        if state == "CANCELLED":
            return prefix + "操作已取消。"
        reason = ""
        if isinstance(task.result, dict) and task.result.get("reason"):
            reason = f"原因：{task.result['reason']}。"
        return f"{prefix}操作未完成，状态：{state}，原因代码：{task.error_code or '未知'}。{reason}"

    @staticmethod
    def _format_data(data: Any) -> str:
        import json
        return json.dumps(data, ensure_ascii=False, indent=2)


def run_demo(audit_path: str = "data/demo-audit.jsonl") -> None:
    agent, bank = build_demo_agent(audit_path)
    conversation = DemoConversation(agent, bank)
    print("星河虚拟银行 Agent 演示（仅合成数据，不连接任何真实服务）。输入“退出”结束。")
    print(f"演示安全码：{conversation.auth_challenge.code}（5 分钟内有效；需要增强认证时发送“认证 {conversation.auth_challenge.code}”）")
    while True:
        try:
            message = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n演示结束。")
            return
        if message in {"退出", "exit", "quit"}:
            print("演示结束。")
            return
        print("Agent>", conversation.handle(message))
