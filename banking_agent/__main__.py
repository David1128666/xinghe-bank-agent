from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .audit import AuditLog
from .config import AgentConfig
from .agent import BankingAgent
from .models import OperationRequest, Operation
from .tools import ToolRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed AI banking agent core")
    parser.add_argument("--config", default="banking_agent_config.template.json")
    parser.add_argument("--audit", default="data/demo-audit.jsonl")
    parser.add_argument("--check", action="store_true", help="validate config and tool wiring")
    parser.add_argument("--demo", action="store_true", help="run the entirely virtual Chinese demo")
    args = parser.parse_args()

    if args.demo:
        from .demo import run_demo
        run_demo(args.audit)
        return 0

    config_path = Path(args.config)
    config = AgentConfig.from_file(config_path)
    hash_env_name = config.audit_hash_key_env
    key = os.environ.get(hash_env_name)
    if not key:
        print(json.dumps({"ready": False, "blocked_by": [f"environment:{hash_env_name}"]}, ensure_ascii=False))
        return 2

    audit = AuditLog(args.audit, key.encode("utf-8"))
    agent = BankingAgent(config, ToolRegistry(), audit)
    if args.check:
        blocked = config.missing_fields_for(Operation.TRANSFER)
        blocked.extend(agent._missing_tools(Operation.TRANSFER))
        print(json.dumps({"ready": not blocked, "blocked_by": blocked}, ensure_ascii=False))
        return 0 if not blocked else 2

    # No bank adapters are bundled. This request demonstrates fail-closed behavior.
    task = agent.submit(
        OperationRequest(
            operation=Operation.TRANSFER,
            user_id="local-demo-user",
            session_id="local-demo-session",
            summary="demo transfer request",
            payload={"amount": "1", "currency": "CONFIGURE_ME"},
        )
    )
    print(json.dumps(task.public_dict(), ensure_ascii=False, indent=2))
    return 0 if task.state.value != "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
