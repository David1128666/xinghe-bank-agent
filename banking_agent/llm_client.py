"""Small, dependency-free transport for OpenAI-compatible chat APIs.

Credentials deliberately live in this object only. Neither provider responses nor
transport exceptions are suitable for showing verbatim in the local web page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.client import HTTPException
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "openai": {"name": "OpenAI / ChatGPT", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "custom": {"name": "其他兼容接口", "base_url": "", "model": ""},
}

MAX_RESPONSE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 512_000


class LLMError(RuntimeError):
    """A fixed, redacted message that is safe to display to the user."""


def _no_constants(_value):
    raise ValueError("non-finite JSON number")


def _json_loads(value):
    return json.loads(value, parse_constant=_no_constants)


def _has_controls(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _scrub_secret(value, secret):
    """A provider can echo the bearer token; never pass that echo to the UI."""
    if isinstance(value, str):
        return value.replace(secret, "[已隐藏]")
    if isinstance(value, list):
        return [_scrub_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {_scrub_secret(key, secret): _scrub_secret(item, secret) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class APIConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float = 30

    def __post_init__(self):
        if not isinstance(self.provider, str) or self.provider not in PROVIDERS:
            raise LLMError("请选择 DeepSeek、通义千问、OpenAI 或自定义兼容接口。")
        if not isinstance(self.base_url, str) or len(self.base_url) > 2048:
            raise LLMError("API 地址无效，请填写服务商的 Base URL。")
        address = self.base_url.strip().rstrip("/")
        if _has_controls(address) or "\\" in address or re.search(r"\s", address):
            raise LLMError("API 地址不能含有空格或控制字符。")
        try:
            parsed = urlsplit(address)
            host, port = parsed.hostname, parsed.port
            if not host or parsed.username is not None or parsed.password is not None:
                raise ValueError("invalid authority")
            if parsed.query or parsed.fragment:
                raise ValueError("query or fragment")
            # Canonical host syntax also rules out encoded header separators.
            ascii_host = host.encode("idna").decode("ascii")
            if not re.fullmatch(r"[A-Za-z0-9.:-]+", ascii_host):
                raise ValueError("invalid hostname")
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("invalid port")
            loopback = host.lower() == "localhost"
            literal = None
            try:
                literal = ipaddress.ip_address(host)
                loopback = loopback or literal.is_loopback
            except ValueError:
                pass
            # Only loopback (local model servers) or public addresses may be used.
            if literal is not None and not (literal.is_private is False or literal.is_loopback):
                raise ValueError("private address")
            if literal is None and re.search(r"\.(?:local|internal|lan|home|corp|intranet)$", host.lower()):
                raise ValueError("private hostname")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
                raise ValueError("insecure URL")
            if "%" in parsed.netloc or "%0a" in parsed.path.lower() or "%0d" in parsed.path.lower():
                raise ValueError("invalid encoding")
        except (ValueError, UnicodeError):
            raise LLMError("API 地址需使用公网 HTTPS 域名；仅本机回环地址可使用 HTTP。"
                          "内网、保留地址与带账号/查询参数/片段的地址一律拒绝。") from None
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200 or _has_controls(self.model):
            raise LLMError("请填写有效的模型名称（不超过 200 字符）。")
        if not isinstance(self.api_key, str) or not self.api_key.strip() or len(self.api_key) > 4096 or _has_controls(self.api_key):
            raise LLMError("请填写有效的 API Key，不能包含换行或控制字符。")
        if not self.api_key.isascii():
            raise LLMError("API Key 只能包含英文字符，请从服务商控制台完整复制。")
        if type(self.timeout) not in (int, float) or not 1 <= self.timeout <= 120:
            raise LLMError("模型请求超时需设置在 1 至 120 秒之间。")
        object.__setattr__(self, "base_url", address)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "api_key", self.api_key.strip())

    @property
    def endpoint(self):
        parsed = urlsplit(self.base_url)
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def public_dict(self):
        return {"provider": self.provider, "base_url": self.base_url,
                "model": self.model, "configured": bool(self.api_key)}


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an Authorization header to a redirected destination.
        return None


class OpenAICompatibleClient:
    def __init__(self, config: APIConfig):
        self.config = config
        self._opener = build_opener(_RejectRedirect())

    def complete(self, messages: list[dict[str, Any]], tools=None) -> dict[str, Any]:
        if not isinstance(messages, list) or not messages:
            raise LLMError("模型上下文为空，未发起 API 请求。")
        payload = {"model": self.config.model, "messages": messages, "stream": False}
        if tools:
            if not isinstance(tools, list) or len(tools) > 64:
                raise LLMError("模型工具配置无效，未发起 API 请求。")
            payload.update({"tools": tools, "tool_choice": "auto"})
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise LLMError("模型上下文格式无效，未发起 API 请求。") from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise LLMError("本轮上下文过长，请开启新一轮对话后重试。")
        request = Request(self.config.endpoint, data=encoded, method="POST", headers={
            "Authorization": "Bearer " + self.config.api_key,
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "XingheBankAgent/1.0",
        })
        try:
            with self._opener.open(request, timeout=self.config.timeout) as response:
                if response.status != 200:
                    raise LLMError("模型接口未返回成功状态，本轮未办理操作。")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            messages_by_status = {
                401: "API 鉴权失败，请检查 API Key 是否正确、是否已开通对应服务。",
                403: "API 访问被拒绝，请检查模型权限、服务地域及账户状态。",
                404: "模型或 API 路径不存在，请检查 Base URL 和模型名称。",
                408: "模型服务请求超时，本轮未办理操作。请稍后再试。",
                429: "模型接口限流或额度不足，请稍后重试或检查服务商账户。",
            }
            if 300 <= status < 400:
                reason = "API 地址发生重定向，已停止连接以保护密钥。请填写服务商的最终 API 地址。"
            elif status >= 500:
                reason = "模型服务暂时不可用，本轮未办理操作。请稍后再试。"
            else:
                reason = messages_by_status.get(status, "模型接口拒绝了请求，请检查模型是否支持 Chat Completions 和工具调用。")
            raise LLMError(reason) from None
        except (TimeoutError, socket.timeout):
            raise LLMError("模型请求超时，本轮未办理操作。请检查网络或稍后再试。") from None
        except (URLError, OSError, HTTPException, UnicodeError, ValueError):
            raise LLMError("无法连接模型接口，请检查网络、API 地址和 HTTPS 证书。") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LLMError("模型返回内容过长，已停止处理，本轮未办理操作。")
        try:
            result = _scrub_secret(_json_loads(raw.decode("utf-8")), self.config.api_key)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError("invalid result")
            choice = result["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ValueError("incomplete result")
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise ValueError("invalid message")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("invalid content")
            validated = {"role": "assistant", "content": content}
            calls = message.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list) or len(calls) > 16:
                    raise ValueError("invalid tools")
                seen = set()
                clean_calls = []
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise ValueError("invalid tool")
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 200 or _has_controls(call_id) or call_id in seen:
                        raise ValueError("invalid call ID")
                    seen.add(call_id)
                    function = call.get("function")
                    if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", function["name"]):
                        raise ValueError("invalid tool name")
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str) or len(arguments) > 32000 or not isinstance(_json_loads(arguments), dict):
                        raise ValueError("invalid arguments")
                    clean_arguments = _scrub_secret(_json_loads(arguments), self.config.api_key)
                    clean_calls.append({"id": call_id, "type": "function", "function": {
                        "name": function["name"],
                        "arguments": json.dumps(clean_arguments, ensure_ascii=False, allow_nan=False),
                    }})
                validated["tool_calls"] = clean_calls
            if not content and not calls:
                raise ValueError("empty message")
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str):
                # Some compatible services require this field when returning a
                # tool result in the same inference turn. Do not display it.
                validated["reasoning_content"] = reasoning
            return validated
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, UnicodeError, RecursionError):
            raise LLMError("模型返回格式不完整或不兼容，本轮未办理操作。请选择支持工具调用的对话模型。") from None

    def test_connection(self) -> str:
        # The check never includes conversation history, accounts or recipients.
        answer = self.complete([{"role": "user", "content": "这是一条 API 连接测试。请只回复：连接成功。"}])
        if not answer.get("content") or answer.get("tool_calls"):
            raise LLMError("模型已响应，但未返回测试文本。请检查模型是否支持普通对话。")
        return "连接成功，模型接口已正常响应。"
