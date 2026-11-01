from __future__ import annotations

import re
from datetime import date
from typing import Any


_SECRET_KEY = re.compile(
    r"password|passwd|pin|cvv|cvc|otp|verification.?code|security.?answer|"
    r"access.?token|api.?key|credential|secret",
    re.IGNORECASE,
)
_ACCOUNT_KEY = re.compile(r"account.?number|card.?number|pan", re.IGNORECASE)
_LONG_DIGIT_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){7,18}\d(?!\d)")
_SHORT_CODE = re.compile(
    r"(验证码|校验码|动态码|短信码|口令|密码|安全码)(\s*(?:是|为|:|：)?\s*)(\d{4,8})(?!\d)"
)


def redact_text(value: str) -> str:
    def mask(match: re.Match[str]) -> str:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", match.group(0)):
            try:
                date.fromisoformat(match.group(0))
                return match.group(0)
            except ValueError:
                pass
        digits = re.sub(r"\D", "", match.group(0))
        return f"••••{digits[-4:]}"

    def mask_code(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}••••"

    value = _SHORT_CODE.sub(mask_code, value)
    return _LONG_DIGIT_RUN.sub(mask, value)


def forbidden_input_keys(value: Any, prefix: str = "") -> list[str]:
    """Return field paths only; never include the submitted values."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _SECRET_KEY.search(str(key)) or _ACCOUNT_KEY.search(str(key)):
                found.append(path)
            else:
                found.extend(forbidden_input_keys(child, path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(forbidden_input_keys(child, f"{prefix}[{index}]"))
    return found


def redact_tool_output(value: Any, field_name: str | None = None) -> Any:
    """Redact sensitive structured fields before exposing adapter output."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if _SECRET_KEY.search(key_text):
                continue
            if _ACCOUNT_KEY.search(key_text):
                digits = re.sub(r"\D", "", str(child))
                safe[key_text] = f"••••{digits[-4:]}" if len(digits) >= 4 else "••••"
            else:
                safe[key_text] = redact_tool_output(child, key_text)
        return safe
    if isinstance(value, list):
        return [redact_tool_output(item, field_name) for item in value]
    if isinstance(value, tuple):
        return [redact_tool_output(item, field_name) for item in value]
    if isinstance(value, str):
        if field_name and re.search(r"amount|balance|fee|price|value", field_name, re.IGNORECASE):
            return value
        return redact_text(value)
    return value
