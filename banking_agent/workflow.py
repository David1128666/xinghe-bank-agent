from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from .agent import BankingAgent
from .models import AgentTask, OperationRequest, TaskState


@dataclass
class WorkflowStep:
    step_id: str
    request: OperationRequest = field(repr=False)
    depends_on: tuple[str, ...] = ()
    task_id: str | None = None
    state: str = "DRAFT"


class BankingWorkflow:
    """Compose existing operations without bypassing any operation's gates."""

    def __init__(self, agent: BankingAgent, requests: list[OperationRequest]):
        if not requests:
            raise ValueError("组合计划不能为空")
        owner = (requests[0].user_id, requests[0].session_id)
        if any((request.user_id, request.session_id) != owner for request in requests):
            raise ValueError("组合计划不能混用不同用户或会话")
        self.agent = agent
        self.user_id, self.session_id = owner
        self.plan_id = f"SIM-FLOW-{uuid.uuid4().hex[:10].upper()}"
        self.steps = [WorkflowStep(f"step-{index + 1}", request,
                                   (f"step-{index}",) if index else ())
                      for index, request in enumerate(requests)]
        self.state = TaskState.DRAFT
        self._event("plan_created")

    def _event(self, action: str, step_id: str = "workflow") -> None:
        self.agent.audit.append({
            "plan_id": self.plan_id, "step_id": step_id, "action": action,
            "user_id_hash": self.agent.audit.hash_identifier(self.user_id),
            "session_id": self.agent.audit.hash_identifier(self.session_id),
            "channel": self.steps[0].request.channel, "operation_type": "cross_scenario",
            "policy_version": self.agent.config.policy_version,
            "request_summary": "composed virtual operations", "result": self.state.value.lower(),
        })

    def advance(self, channel: str | None = None) -> AgentTask | None:
        if self.state is TaskState.CANCELLED:
            return None
        by_id = {step.step_id: step for step in self.steps}
        last = None
        for step in self.steps:
            if step.task_id:
                last = self.agent.get_task(step.task_id, self.user_id, self.session_id)
                step.state = last.state.value
                if last.state is TaskState.SUCCESS:
                    continue
                if last.state in {TaskState.FAILED, TaskState.CANCELLED, TaskState.PARTIAL_SUCCESS}:
                    for dependent in self.steps:
                        if dependent.task_id is None:
                            dependent.state = "NOT_EXECUTED"
                    self.state = TaskState.PARTIAL_SUCCESS if any(s.state == "SUCCESS" for s in self.steps) else last.state
                    self._event("stopped", step.step_id)
                    return last
                self.state = last.state
                return last
            if step.state == "NOT_EXECUTED":
                return last
            if any(by_id[dependency].state != "SUCCESS" for dependency in step.depends_on):
                return last
            self._event("step_started", step.step_id)
            if channel is not None:
                if channel not in {"app", "im", "other"}:
                    raise ValueError("无效渠道")
                step.request = replace(step.request, channel=channel)
            last = self.agent.submit(step.request)
            step.task_id = last.task_id
            step.state = last.state.value
            if last.state is not TaskState.SUCCESS:
                self.state = last.state
                return last
        self.state = TaskState.SUCCESS
        self._event("completed")
        return last

    def cancel(self) -> None:
        for step in self.steps:
            if step.task_id:
                task = self.agent.get_task(step.task_id, self.user_id, self.session_id)
                if task.state in {TaskState.DRAFT, TaskState.AWAITING_INFORMATION,
                                  TaskState.AWAITING_AUTHENTICATION, TaskState.AWAITING_CONFIRMATION,
                                  TaskState.UNDER_REVIEW}:
                    task = self.agent.cancel(task.task_id, self.user_id, self.session_id)
                step.state = task.state.value
            else:
                step.state = "NOT_EXECUTED"
        self.state = TaskState.CANCELLED
        self._event("cancelled")

    def public_dict(self) -> dict:
        for step in self.steps:
            if step.task_id:
                step.state = self.agent.get_task(step.task_id, self.user_id, self.session_id).state.value
        return {"plan_id": self.plan_id, "state": self.state.value,
                "steps": [{"step_id": step.step_id, "operation": step.request.operation.value,
                           "depends_on": list(step.depends_on), "state": step.state,
                           "task_id": step.task_id} for step in self.steps]}
