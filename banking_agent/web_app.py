"""Offline browser UI for the synthetic bank, using only the Python standard library."""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from decimal import Decimal
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import webbrowser

from .agent import BankingAgent
from .audit import AuditError
from .demo import AuthChallenge, DemoConversation, TransferLedger, build_demo_agent
from .models import Operation, OperationRequest, RiskLevel, TaskState, amount_as_decimal
from .privacy import redact_tool_output
from .llm_client import APIConfig, LLMError, OpenAICompatibleClient, PROVIDERS
from .llm_agent import ModelConversation


ASSET_DIR = Path(__file__).resolve().parent / "web"
WELCOME = "你好，欢迎来到星河。\n我可以帮你查询账单、办理模拟转账、管理订阅与卡片，也可以体验虚拟股票购买。小于3000元的普通即时转账会直接执行；3000元及以上先确认。你可以直接告诉我，或点击功能按钮开始。"


def valid_recipient_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[\u4e00-\u9fff·]{2,16}", name):
        return False
    if any(word in name for word in ("转账", "汇款", "付款", "账户", "账号", "收款", "取消", "和", "或", "与", "及", "他们", "她们")):
        return False
    return name not in {"某人", "对方", "别人", "朋友", "同学", "老板", "任何人", "随便谁", "不知道", "这个人", "那个人", "他", "她"}


def explicit_recipient_name(text):
    text = re.split(r"用途|备注|原因", text, maxsplit=1)[0]
    match = re.search(r"(?:转给|汇给|给|向|为)\s*([\u4e00-\u9fff·]{2,16}?)(?=\s*(?:转账|汇款|打款|付款|每月|每周|每年|尾号|账号|账户|[\d，,。；;]|$))", text)
    name = match.group(1) if match else None
    return name if valid_recipient_name(name) else None


class BrowserDemoAgent(BankingAgent):
    """The user-selected direct-transfer policy applies only to this local demo."""

    def _resolve_and_assess(self, task, selected_recipient_ref=None):
        if task.request.operation is Operation.TRANSFER and selected_recipient_ref is None:
            bank = self.tools.transfer_resolver
            payload = task.request.payload
            query = str(payload.get("recipient_query", "")).strip()
            exact = [(ref, row) for ref, row in bank.recipients.items() if row["name"] == query]
            candidate = None
            if valid_recipient_name(query) and (not exact or (len(exact) == 1 and not exact[0][1].get("known", True))):
                candidate = {"name": query, "existing_ref": exact[0][0] if exact else None}
            if candidate:
                task.internal_plan["recipient_identity"] = candidate
                task.state = TaskState.AWAITING_INFORMATION
                task.missing_information = ("recipient_identity",)
                task.error_code = "recipient_identity_required"
                self._audit(task, "recipient_identity_requested")
                return task
            if len(exact) == 1:
                task.request = replace(task.request, payload={**payload, "recipient_ref": exact[0][0]})
        return super()._resolve_and_assess(task, selected_recipient_ref)

    def _direct_transfer_authorized(self, task):
        if task.request.operation is not Operation.TRANSFER or not task.preview or not task.risk:
            return False
        payload, preview = task.request.payload, task.preview
        amount = amount_as_decimal(payload.get("amount"))
        quoted_amount = amount_as_decimal(preview.amount)
        fee = amount_as_decimal(preview.fee)
        return bool(
            amount is not None and amount == quoted_amount and Decimal("0") < amount < Decimal("3000")
            and amount.as_tuple().exponent >= -2
            and payload.get("currency") == "CNY" and preview.currency == "CNY" and fee == 0
            and payload.get("execution_time") == "now" and payload.get("frequency", "once") == "once"
            and task.prepared is not None and task.risk.level is not RiskLevel.R4
            and not task.risk.manual_review_required
        )


class BrowserDemoConversation(DemoConversation):
    def _find_recipient(self, text):
        name = explicit_recipient_name(text)
        if name:
            return name
        recipient_text = re.split(r"用途|备注|原因", text, maxsplit=1)[0]
        phone = re.search(r"(?<!\d)1\d{10}(?!\d)", recipient_text)
        if phone:
            return phone.group(0)
        if re.search(r"(?:转给|汇给|给|向|为)", recipient_text):
            return None
        # A full name boundary prevents 张小明 from resolving to known 小明.
        names = sorted({row["name"] for row in self.bank.recipients.values()}, key=len, reverse=True)
        for known_name in names:
            if re.search(r"(?:^|[^\u4e00-\u9fff·])" + re.escape(known_name) + r"(?=$|[^\u4e00-\u9fff·]|转账|汇款|付款|尾号)", recipient_text):
                return known_name
        return self._extract_named_value(text, ("历史备注", "收款备注"))

    def _parse(self, text):
        request = super()._parse(text)
        if request is None or request.operation is not Operation.TRANSFER:
            return request
        payload = dict(request.payload)
        # Do not retain the base parser's greedy name fallback (e.g. 张三转账).
        payload["recipient_query"] = self._find_recipient(text) or ""
        # Omitted fields use visible demo defaults. Explicit unknown accounts or
        # future timing are never silently converted to an immediate transfer.
        if not payload.get("source_account_ref") and not re.search(r"尾号|信用卡|钱包|第三方|账户|银行卡", text):
            payload["source_account_ref"] = "checking"
        if not payload.get("execution_time") and not re.search(r"明天|后天|明早|今晚|下周|下月|预约|定时|每月|每周|每年|未来|稍后|日期", text):
            payload["execution_time"] = "now"
        if not payload.get("purpose"):
            payload["purpose"] = "日常转账"
        return replace(request, payload=payload)

    def _supplement(self, missing, text):
        fields = super()._supplement(missing, text)
        if "recipient" in missing:
            name = explicit_recipient_name(text)
            if name is None and valid_recipient_name(text.strip()):
                name = text.strip()
            fields["recipient_query"] = name or self._find_recipient(text) or ""
        return fields


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class BrowserSession:
    audit_dir: Path
    ledger: TransferLedger | None = None
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    revision: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    requests: OrderedDict = field(default_factory=OrderedDict)
    pending_recipient: dict[str, Any] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    model_config: APIConfig | None = field(default=None, repr=False)
    model_chat: ModelConversation | None = field(default=None, repr=False)
    model_requests: OrderedDict = field(default_factory=OrderedDict, repr=False)
    session_id: str = field(default_factory=lambda: "web-" + secrets.token_hex(8))
    auth: AuthChallenge = field(default_factory=AuthChallenge)
    seen_at: float = field(default_factory=time.monotonic)
    max_messages: int = 60
    max_requests: int = 2000

    def __post_init__(self):
        self.reset()

    def reset(self):
        standard_agent, self.bank = build_demo_agent(str(self.audit_dir / f"web-{secrets.token_hex(12)}.jsonl"))
        self.bank.bind_identity("demo-user", self.session_id)
        self.bank.bind_channel_source(lambda: self.chat.channel)
        if self.ledger is not None:
            self.bank.ledger = self.ledger
        self.agent = BrowserDemoAgent(standard_agent.config, standard_agent.tools, standard_agent.audit)
        self.chat = BrowserDemoConversation(self.agent, self.bank, user_id="demo-user",
                                            session_id=self.session_id, channel="app",
                                            auth_challenge=self.auth)
        self.model_chat = ModelConversation(self.chat, OpenAICompatibleClient(self.model_config)) if self.model_config else None
        self.pending_recipient = None
        self.messages = [{"role": "assistant", "text": WELCOME, "task": None, "id": secrets.token_hex(8)}]
        self.revision += 1

    def task(self):
        if self.chat.pending_task_id:
            return self.agent.get_task(self.chat.pending_task_id, self.chat.user_id, self.chat.session_id)
        return None

    def task_public(self, selected_task=None):
        if self.pending_recipient:
            return None
        task = selected_task or self.task()
        if not task:
            return None
        result = redact_tool_output(task.public_dict())
        result["confirmation_policy"] = task.internal_plan.get("confirmation_policy")
        return result

    def query(self, operation: Operation, payload=None):
        request = OperationRequest(operation, self.chat.user_id, self.chat.session_id, "网页本地概览", payload or {}, channel=self.chat.channel)
        # Overview reads use the same authentication and query tools as the CLI.
        task = self.agent.submit(request)
        if task.state is TaskState.SUCCESS:
            return (task.result or {}).get("data", {})
        return {}

    def confirmation_token(self):
        """Single-use random nonce bound to the pending task; the digest is still re-checked on confirm."""
        visible = self.task_public()
        if not visible or visible["state"] != "AWAITING_CONFIRMATION":
            return None
        task = self.task()
        if not task:
            return None
        nonce = task.internal_plan.get("confirmation_nonce")
        if not isinstance(nonce, str) or not nonce:
            nonce = secrets.token_urlsafe(24)
            task.internal_plan["confirmation_nonce"] = nonce
        return nonce

    def apply_authentication(self, factor) -> str:
        """Enhanced authentication requires the one-time demonstration code, not a chat phrase."""
        if self.chat.channel == "app":
            return "当前已在 APP 安全界面（增强认证），无需重复认证。"
        if not isinstance(factor, str) or not re.fullmatch(r"[0-9]{6}", factor):
            raise ApiError(400, "请输入页面显示的 6 位演示安全码。")
        note = self.chat._strengthen_authentication(factor)
        if self.chat.channel != "app":
            raise ApiError(400, note)
        return note

    def workflow_public(self):
        if not self.chat.workflow:
            return None
        result = self.chat.workflow.public_dict()
        for public, step in zip(result["steps"], self.chat.workflow.steps):
            public["payload"] = redact_tool_output({key: value for key, value in step.request.payload.items()
                                                    if key not in {"recipient_ref", "opaque_reference", "idempotency_key"}})
        return result

    def snapshot(self):
        accounts = self.query(Operation.BALANCE).get("accounts", [])
        return {
            "csrf": self.csrf, "revision": self.revision,
            "llm": ({**self.model_config.public_dict(), "enabled": True} if self.model_config else
                    {"enabled": False, "configured": False, "provider": "deepseek", "base_url": "", "model": ""}),
            "providers": [{"id": key, **values} for key, values in PROVIDERS.items()],
            "messages": self.messages, "task": self.task_public(),
            "confirmation_token": self.confirmation_token(),
            "auth": {**self.auth.public(), "channel": self.chat.channel,
                     "enhanced": self.chat.channel == "app"},
            "accounts": accounts, "channel": self.chat.channel,
            "today": self.bank._today().isoformat(),
            "workflow": self.workflow_public(),
            "recipients": [{"name": row["name"], "account_last4": row["last4"],
                            "bank_name": row.get("bank_name", "星河模拟银行"),
                            "identity_confirmed": bool(row.get("known", True))}
                           for row in self.bank.recipients.values()],
            "pending_recipient": ({key: value for key, value in self.pending_recipient.items()
                                   if key not in {"task_id", "existing_ref"}}
                                  if self.pending_recipient else None),
            "transfer_policy": {"direct_below": "3000.00", "currency": "CNY", "default_account": "checking", "default_execution": "now", "default_purpose": "日常转账", "scheduled_requires_confirmation": True},
        }

    def send(self, body):
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ApiError(400, "请求编号无效，请刷新页面后重试。")
        fingerprint = json.dumps(body, sort_keys=True, ensure_ascii=False)
        if request_id in self.requests:
            if self.requests[request_id] != fingerprint:
                raise ApiError(409, "该请求编号已用于其他操作。")
            return self.snapshot()
        if type(body.get("revision")) is not int or body["revision"] != self.revision:
            raise ApiError(409, "当前会话已更新，请核对最新内容后再操作。")
        if body.get("action") == "reset":
            self.reset()
        elif body.get("action") == "authenticate":
            note = self.apply_authentication(body.get("factor"))
            self.messages.append({"role": "user", "text": "使用演示安全码完成增强认证", "id": secrets.token_hex(8)})
            self.messages.append({"role": "assistant", "text": note, "task": self.task_public(), "id": secrets.token_hex(8)})
            self.messages = self.messages[-self.max_messages:]
            self.revision += 1
        elif body.get("action") in {"confirm_recipient", "cancel_recipient"}:
            self.handle_recipient_identity(body)
        else:
            if self.pending_recipient:
                raise ApiError(409, "请先确认或取消当前收款人的身份核对。")
            message = body.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 3000:
                raise ApiError(400, "请输入 1 至 3000 字的指令。")
            message = message.strip()
            task = self.task()
            if task and task.state is TaskState.AWAITING_CONFIRMATION and message == task.confirmation_phrase:
                expected = self.confirmation_token() or ""
                if not secrets.compare_digest(str(body.get("confirmation_token", "")).encode(), expected.encode()):
                    raise ApiError(409, "确认摘要已更新，请核对当前摘要后重新确认。")
                # Single use: a replayed confirmation cannot reuse this nonce.
                task.internal_plan.pop("confirmation_nonce", None)
            use_model = bool(self.model_chat and body.get("source") == "chat" and not
                             (task and task.state is TaskState.AWAITING_CONFIRMATION and message == task.confirmation_phrase))
            if use_model:
                try:
                    response = self.model_chat.handle(message, self.messages)
                except LLMError as error:
                    response = f"AI 服务暂未完成本次请求：{error}。未自动改用演示关键词办理。"
            else:
                response = self.chat.handle(message)
            if self.stage_recipient_identity():
                response = "首次向这个虚构收款人付款，请在弹窗核对姓名、模拟银行和账户尾号。确认身份后会保存到本轮收款人列表，再按金额规则办理。"
            rendered_task = self.task_public()
            if use_model:
                selected_task = self.model_chat.last_task
                rendered_task = self.task_public(selected_task) if selected_task else None
            if response.startswith(("这是纯虚拟演示。", "【纯虚拟模拟】已记录你对本地虚构日历")):
                rendered_task = None
            self.messages.append({"role": "user", "text": message.strip(), "id": secrets.token_hex(8)})
            self.messages.append({"role": "assistant", "text": response, "task": rendered_task, "source": "model" if use_model else "tool", "id": secrets.token_hex(8)})
            self.messages = self.messages[-self.max_messages:]
            self.revision += 1
        self.requests[request_id] = fingerprint
        while len(self.requests) > self.max_requests:
            self.requests.popitem(last=False)
        return self.snapshot()

    def configure_model(self, body):
        """Credentials stay in this server session; request caches contain only digests."""
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ApiError(400, "请求编号无效，请刷新页面。")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = self.model_requests.get(request_id)
        if previous:
            if previous[0] != digest:
                raise ApiError(409, "该请求编号已用于其他设置。")
            return {**self.snapshot(), **previous[1]}
        if type(body.get("revision")) is not int or body["revision"] != self.revision:
            raise ApiError(409, "会话已更新，请重新保存模型设置。")
        action = body.get("action")
        extra = {}
        try:
            if action == "configure":
                values = body.get("payload")
                if not isinstance(values, dict) or set(values) - {"provider", "base_url", "model", "api_key"}:
                    raise ApiError(400, "模型设置格式无效。")
                provider = values.get("provider", "custom")
                if not isinstance(provider, str) or provider not in PROVIDERS:
                    raise ApiError(400, "请选择有效的服务商。")
                base_url = values.get("base_url", "")
                key = values.get("api_key", "")
                if not key and self.model_config and provider == self.model_config.provider and isinstance(base_url, str) and base_url.rstrip("/") == self.model_config.base_url:
                    key = self.model_config.api_key
                config = APIConfig(provider, base_url, values.get("model", ""), key)
                self.model_config = config
                self.model_chat = ModelConversation(self.chat, OpenAICompatibleClient(config))
            elif action == "disconnect":
                self.model_chat = None
                self.model_config = None
            elif action == "test":
                if not self.model_config:
                    raise ApiError(400, "请先填写并保存模型设置。")
                result = OpenAICompatibleClient(self.model_config).test_connection()
                # A provider response is untrusted text and could echo a credential.
                extra["model_test"] = "连接成功，模型已正常响应。" if result else "模型已响应。"
            else:
                raise ApiError(400, "不支持的模型设置操作。")
        except LLMError as error:
            raise ApiError(400 if action == "configure" else 502, str(error)) from None
        self.revision += 1
        self.model_requests[request_id] = (digest, extra)
        return {**self.snapshot(), **extra}

    def stage_recipient_identity(self):
        task = self.task()
        if not task or task.error_code != "recipient_identity_required":
            return False
        candidate = task.internal_plan["recipient_identity"]
        existing = self.bank.recipients.get(candidate["existing_ref"], {})
        tail = existing.get("last4") or str(1000 + int(hashlib.sha256(candidate["name"].encode()).hexdigest()[:8], 16) % 9000)
        account = self.bank.accounts.get(task.request.payload.get("source_account_ref"), {})
        self.pending_recipient = {"token": secrets.token_urlsafe(24), "task_id": task.task_id,
            "name": candidate["name"], "existing_ref": candidate["existing_ref"],
            "bank_name": "星河模拟银行", "account_last4": tail,
            "amount": task.request.payload.get("amount"), "currency": "CNY",
            "payer_account_last4": account.get("last4", ""),
            "execution_time": task.request.payload.get("execution_time"),
            "purpose": task.request.payload.get("purpose")}
        return True

    def handle_recipient_identity(self, body):
        pending = self.pending_recipient
        if not pending or not secrets.compare_digest(str(body.get("recipient_token", "")).encode(), pending["token"].encode()):
            raise ApiError(409, "收款人核对已更新或结束，请查看当前内容。")
        task = self.task()
        if not task or task.task_id != pending["task_id"] or task.state is not TaskState.AWAITING_INFORMATION:
            raise ApiError(409, "待办理转账已更新，请重新核对。")
        if body["action"] == "cancel_recipient":
            self.agent.cancel(task.task_id, self.chat.user_id, self.chat.session_id)
            response = "已取消首次收款人核对，没有付款，也没有新增收款人。"
        else:
            values = body.get("payload")
            if not isinstance(values, dict):
                raise ApiError(400, "请填写收款人姓名和虚构账户尾号。")
            name, tail = values.get("name"), values.get("account_last4")
            if not valid_recipient_name(name) or not isinstance(tail, str) or not re.fullmatch(r"\d{4}", tail):
                raise ApiError(400, "请填写明确的中文姓名（2至16字）及4位数字的虚构账户尾号。")
            matches = [(ref, row) for ref, row in self.bank.recipients.items() if row["name"] == name]
            target = next(((ref, row) for ref, row in matches if row["last4"] == tail), None)
            if matches and target is None:
                raise ApiError(400, "该姓名已有演示账户，填写的尾号与列表不符，请重新核对。")
            # Record identity approval before creating a contact or submitting a payment.
            self.agent._audit(task, "recipient_identity_confirmed", result="verified", confirmation={
                "required": True, "received": True, "timestamp": self.bank._now(),
                "summary_hash": hashlib.sha256((pending["token"] + name + tail).encode()).hexdigest(),
                "expires_at": None})
            if target:
                ref, contact = target
            else:
                ref = f"sim-browser-recipient-{secrets.token_hex(8)}"
                contact = {"name": name, "last4": tail, "phone": "", "history_note": "本轮核对的虚构收款人", "balance": Decimal("0.00")}
            contact.update(known=True, bank_name="星河模拟银行")
            self.bank.recipients[ref] = contact
            try:
                task = self.agent.provide_information(task.task_id, self.chat.user_id, self.chat.session_id,
                                                     {"recipient_query": name, "recipient_ref": ref})
                response = "已确认虚构收款人身份并保存到本轮列表。\n" + self.chat._render(task)
            except AuditError:
                task.state = TaskState.FAILED
                task.error_code = "audit_unavailable"
                response = "收款人身份已记录，但审计不可用，付款已暂停。请查询当前任务状态。"
        self.pending_recipient = None
        self.messages.append({"role": "user", "text": "取消收款人核对" if body["action"] == "cancel_recipient" else "确认虚构收款人身份", "id": secrets.token_hex(8)})
        self.messages.append({"role": "assistant", "text": response, "task": self.task_public(), "id": secrets.token_hex(8)})
        self.messages = self.messages[-self.max_messages:]
        self.revision += 1


class LocalBankServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address=("127.0.0.1", 0), audit_dir="data", *,
                 max_sessions=30, idle_seconds=1800, max_workers=24):
        if address[0] != "127.0.0.1":
            raise ValueError("网页演示仅允许绑定本机 127.0.0.1。")
        self.audit_dir = Path(audit_dir)
        self.sessions: dict[str, BrowserSession] = {}
        self.sessions_lock = threading.RLock()
        self.ledger = TransferLedger()
        self.max_sessions = max_sessions
        self.idle_seconds = idle_seconds
        self._slots = threading.BoundedSemaphore(max_workers)
        super().__init__(address, LocalBankHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def sweep_sessions(self, now: float) -> None:
        """Expire idle sessions, then evict least-recently-used ones to stay within the cap."""
        for key in [key for key, session in self.sessions.items()
                    if now - session.seen_at > self.idle_seconds]:
            self.sessions.pop(key, None)
        while self.sessions and len(self.sessions) >= self.max_sessions:
            oldest = min(self.sessions.items(), key=lambda row: row[1].seen_at)[0]
            self.sessions.pop(oldest, None)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                try:
                    request.close()
                except OSError:
                    pass
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class LocalBankHandler(BaseHTTPRequestHandler):
    server: LocalBankServer
    server_version = "XingheBankDemo"
    sys_version = ""
    timeout = 10

    def log_message(self, format, *args):
        pass

    def reply(self, status, content, content_type="application/json; charset=utf-8", cookie=None):
        payload = json.dumps(content, ensure_ascii=False).encode("utf-8") if isinstance(content, dict) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", f"bank_session={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")
        self.end_headers()
        self.wfile.write(payload)

    def validate_host(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            raise ApiError(403, "仅支持通过本机演示地址访问。")

    def session(self, create=False):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            raise ApiError(400, "浏览器会话无效，请重新打开演示页面。")
        key = cookies["bank_session"].value if "bank_session" in cookies else ""
        now = time.monotonic()
        with self.server.sessions_lock:
            self.server.sweep_sessions(now)
            session = self.server.sessions.get(key)
            if session is not None:
                session.seen_at = now
                return session, None
            if not create:
                raise ApiError(403, "请刷新页面，重新建立演示会话。")
            key = secrets.token_urlsafe(32)
            self.server.sessions[key] = BrowserSession(self.server.audit_dir, ledger=self.server.ledger)
            return self.server.sessions[key], key

    def send_error(self, code, message=None, explain=None):
        try:
            self.reply(code, {"error": "请求方式或路径不受支持。"})
        except Exception:
            pass

    def do_HEAD(self):
        self.send_error(405)

    def do_PUT(self):
        self.send_error(405)

    def do_DELETE(self):
        self.send_error(405)

    def do_OPTIONS(self):
        self.send_error(405)

    def do_GET(self):
        try:
            self.validate_host()
            path = urlsplit(self.path).path
            if path == "/api/state":
                session, cookie = self.session(create=True)
                with session.lock:
                    self.reply(200, session.snapshot(), cookie=cookie)
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/favicon.svg": ("favicon.svg", "image/svg+xml"),
            }
            if path not in assets:
                raise ApiError(404, "页面不存在。")
            name, mime = assets[path]
            self.reply(200, (ASSET_DIR / name).read_bytes(), mime)
        except ApiError as error:
            self.reply(error.status, {"error": str(error)})
        except Exception:
            self.reply(500, {"error": "本地服务暂不可用，请查看启动窗口。"})

    def do_POST(self):
        try:
            self.validate_host()
            route = urlsplit(self.path).path
            if route not in {"/api/message", "/api/model"}:
                raise ApiError(404, "接口不存在。")
            if self.headers.get("Origin") != self.server.origin:
                raise ApiError(403, "请在本地演示页面内操作。")
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ApiError(415, "请求格式无效。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError(400, "请求长度无效。")
            if not 0 < length <= 20000:
                raise ApiError(413, "请求内容过长或为空。")
            self.connection.settimeout(8)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise ApiError(400, "请求内容不是有效 JSON。")
            if not isinstance(body, dict):
                raise ApiError(400, "请求内容格式无效。")
            session, _ = self.session()
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session.csrf):
                raise ApiError(403, "会话校验失败，请刷新页面。")
            with session.lock:
                self.reply(200, session.configure_model(body) if route == "/api/model" else session.send(body))
        except ApiError as error:
            self.reply(error.status, {"error": str(error)})
        except Exception:
            self.reply(500, {"error": "操作结果暂不明确，请刷新查看当前状态，不要重复提交资金操作。"})


def main():
    parser = argparse.ArgumentParser(description="星河虚拟银行网页演示")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--audit-dir", default="data")
    args = parser.parse_args()
    server = LocalBankServer(("127.0.0.1", args.port), args.audit_dir)
    print("\n星河虚拟银行 · 网页演示已启动", flush=True)
    print(f"\n请在浏览器打开：{server.origin}\n", flush=True)
    print("这是本机虚构演示，不连接真实银行。使用期间请保留本窗口。", flush=True)
    print("退出时关闭本窗口或按 Ctrl+C；业务数据将在服务重启后重置。\n", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(server.origin)
        except Exception:
            print("未能自动打开浏览器，请复制上面的地址手动打开。", flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        print("\n网页演示已结束。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
