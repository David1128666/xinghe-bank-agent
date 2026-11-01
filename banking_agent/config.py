from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import Operation


@dataclass(frozen=True)
class AgentConfig:
    bank_name: str | None = None
    jurisdiction: str | None = None
    default_currency: str | None = None
    timezone: str | None = None
    low_risk_limit: str | int | float | None = None
    high_risk_limit: str | int | float | None = None
    daily_transfer_limit: str | int | float | None = None
    investment_confirm_limit: str | int | float | None = None
    confirmation_ttl: int | None = None
    auth_methods: tuple[str, ...] = ()
    manual_review_channel: str | None = None
    support_channel: str | None = None
    available_tools: tuple[str, ...] = ()
    policy_version: str | None = None
    audit_hash_key_env: str = "BANKING_AGENT_AUDIT_HASH_KEY"
    _raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> AgentConfig:
        return cls(
            bank_name=values.get("bank_name"),
            jurisdiction=values.get("jurisdiction"),
            default_currency=values.get("default_currency"),
            timezone=values.get("timezone"),
            low_risk_limit=values.get("low_risk_limit"),
            high_risk_limit=values.get("high_risk_limit"),
            daily_transfer_limit=values.get("daily_transfer_limit"),
            investment_confirm_limit=values.get("investment_confirm_limit"),
            confirmation_ttl=values.get("confirmation_ttl"),
            auth_methods=tuple(values.get("auth_methods") or ()),
            manual_review_channel=values.get("manual_review_channel"),
            support_channel=values.get("support_channel"),
            available_tools=tuple(values.get("available_tools") or ()),
            policy_version=values.get("policy_version"),
            audit_hash_key_env=values.get(
                "audit_hash_key_env", "BANKING_AGENT_AUDIT_HASH_KEY"
            ),
            _raw=dict(values),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> AgentConfig:
        with Path(path).open("r", encoding="utf-8") as handle:
            values = json.load(handle)
        if not isinstance(values, dict):
            raise ValueError("Configuration must be a JSON object")
        return cls.from_mapping(values)

    def missing_fields_for(self, operation: Operation) -> list[str]:
        fields = ["bank_name", "jurisdiction", "policy_version"]
        if operation is not Operation.PUBLIC_INFO:
            fields.extend(["timezone", "auth_methods", "support_channel"])
        if operation.is_mutating:
            fields.extend(
                [
                    "default_currency",
                    "low_risk_limit",
                    "high_risk_limit",
                    "daily_transfer_limit",
                    "confirmation_ttl",
                    "manual_review_channel",
                ]
            )
            if operation is Operation.INVESTMENT:
                fields.append("investment_confirm_limit")

        missing: list[str] = []
        for name in fields:
            value = getattr(self, name)
            if value is None or value == "" or value == ():
                missing.append(name)
        if operation.is_mutating and self.confirmation_ttl is not None:
            if not isinstance(self.confirmation_ttl, int) or self.confirmation_ttl <= 0:
                missing.append("confirmation_ttl (must be positive seconds)")

        if operation.is_mutating:
            try:
                low = Decimal(str(self.low_risk_limit))
                high = Decimal(str(self.high_risk_limit))
                daily = Decimal(str(self.daily_transfer_limit))
                if low < 0 or high <= low or daily <= 0:
                    missing.append("risk_limits (invalid ordering or value)")
            except (InvalidOperation, TypeError, ValueError):
                if "risk_limits (invalid ordering or value)" not in missing:
                    missing.append("risk_limits (invalid ordering or value)")

        if operation is Operation.PUBLIC_INFO:
            if "public_info" not in self.available_tools:
                missing.append("available_tools.public_info")
        return missing
