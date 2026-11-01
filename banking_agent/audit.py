from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class AuditError(RuntimeError):
    pass


class AuditLog:
    """Append-only JSONL audit writer with a tamper-evident SHA-256 chain."""

    def __init__(self, path: str | Path, user_hash_key: bytes):
        if not user_hash_key:
            raise ValueError("A dedicated audit hash key is required")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = user_hash_key
        self._lock = threading.RLock()
        self._last_hash = self.verify()

    def hash_identifier(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self) -> str:
        previous = "0" * 64
        if not self.path.exists():
            return previous
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    event_mac = entry.pop("event_mac", None)
                    event_hash = entry.pop("event_hash")
                    if entry.get("prev_hash") != previous:
                        raise AuditError(f"Audit chain mismatch on line {line_number}")
                    expected = self._digest(entry)
                    if not hmac.compare_digest(event_hash, expected):
                        raise AuditError(f"Audit hash mismatch on line {line_number}")
                    if event_mac is not None and not hmac.compare_digest(event_mac, self._mac(event_hash)):
                        raise AuditError(f"Audit event signature mismatch on line {line_number}")
                    previous = event_hash
        except AuditError:
            raise
        except Exception as exc:
            raise AuditError("Audit log is unreadable or malformed") from exc
        return previous

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = dict(event)
            entry.setdefault("event_id", str(uuid.uuid4()))
            entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            entry["prev_hash"] = self._last_hash
            event_hash = self._digest(entry)
            entry["event_hash"] = event_hash
            entry["event_mac"] = self._mac(event_hash)
            line = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except Exception as exc:
                raise AuditError("Could not durably append audit event") from exc
            self._last_hash = event_hash
            return entry

    def _mac(self, event_hash: str) -> str:
        """Authenticate the event so a rewritten chain is detectable without the key."""
        return hmac.new(self._key, event_hash.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _digest(entry: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
