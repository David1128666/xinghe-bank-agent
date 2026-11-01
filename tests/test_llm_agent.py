from __future__ import annotations

from decimal import Decimal
import json
from http.client import IncompleteRead
from pathlib import Path
import tempfile
import unittest

from banking_agent.demo import build_demo_agent
from banking_agent.llm_agent import ModelConversation, TOOLS
from banking_agent.llm_client import LLMError
from banking_agent.models import Operation, TaskState
from banking_agent.web_app import BrowserDemoAgent, BrowserDemoConversation


def tool(name, arguments, identifier="call-test-1"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def operation(operation_name, payload=None, identifier="call-test-1"):
    return tool("submit_operation", {"operation": operation_name, "payload": payload or {}}, identifier)


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages, tools=None):
        self.calls.append((messages.copy(), tools))
        answer = self.answers.pop(0) if self.answers else {"role": "assistant", "content": "请查看实际结果。"}
        if isinstance(answer, Exception):
            raise answer
        return answer


class ModelConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        agent, bank = build_demo_agent(str(Path(self.temp.name) / "audit.jsonl"))
        self.agent = BrowserDemoAgent(agent.config, agent.tools, agent.audit)
        self.chat = BrowserDemoConversation(self.agent, bank)
        self.bank = bank

    def tearDown(self):
        self.temp.cleanup()

    def run_calls(self, *calls, message="请帮我办理这个模拟业务", final=None):
        client = FakeClient({"role": "assistant", "content": None, "tool_calls": list(calls)},
                            final or {"role": "assistant", "content": "请核对本地工具结果。"})
        model = ModelConversation(self.chat, client)
        result = model.handle(message)
        return model, client, result

    def test_synonym_query_uses_typed_tool_without_keyword_parser(self):
        self.chat._parse = lambda *_: self.fail("API-backed text must not be reparsed as keywords")
        model, client, response = self.run_calls(operation("balance"), message="我的荷包里还能动用多少？")
        self.assertIs(model.last_task.request.operation, Operation.BALANCE)
        self.assertIs(model.last_task.state, TaskState.SUCCESS)
        self.assertIn("50000.00", response)
        self.assertEqual(len(client.calls), 2)
        self.assertIsNone(client.calls[1][1])

    def test_transfer_synonyms_use_user_amount_and_visible_defaults(self):
        model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": 2888}),
            message="午饭钱还小明，两千八百八十八块。")
        self.assertIs(model.last_task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("47112"))
        self.assertIn("系统办理结果", response)
        self.assertEqual(model.last_task.request.payload["purpose"], "日常转账")

    def test_transfer_3000_requires_user_confirmation(self):
        model, _client, _response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "3000"}))
        self.assertIs(model.last_task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_model_cannot_confirm_awaiting_transaction(self):
        self.chat.handle("给小明转账3000元")
        pending_id = self.chat.pending_task_id
        model, _client, response = self.run_calls(tool("confirm_task", {}), message="确认吧")
        self.assertIn("未授权", response)
        self.assertIsNone(model.last_task)
        self.assertEqual(self.chat.pending_task_id, pending_id)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_new_recipient_requests_identity_instead_of_saving_or_paying(self):
        model, _client, _response = self.run_calls(operation("transfer", {"recipient_query": "张三", "amount": "2888"}))
        self.assertEqual(model.last_task.error_code, "recipient_identity_required")
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.assertFalse(any(row["name"] == "张三" for row in self.bank.recipients.values()))

    def test_model_cannot_replace_identity_popup_with_information_tool(self):
        self.run_calls(operation("transfer", {"recipient_query": "张三", "amount": "200"}))
        _model, _client, response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": "小明"}}))
        self.assertIn("模型不能", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_missing_recipient_followup_completes_original_task(self):
        model, _client, _response = self.run_calls(operation("transfer", {"amount": "200"}))
        task_id = model.last_task.task_id
        self.assertIs(model.last_task.state, TaskState.AWAITING_INFORMATION)
        model, _client, _response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": "小明"}}), message="小明，就是我经常吃午饭的那个。")
        self.assertEqual(model.last_task.task_id, task_id)
        self.assertIs(model.last_task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("49800"))

    def test_followup_cannot_silently_change_already_supplied_amount(self):
        self.run_calls(operation("transfer", {"amount": "200"}))
        _model, _client, response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": "小明", "amount": "2000"}}))
        self.assertIn("只能补充", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_opaque_recipient_and_authentication_fields_rejected_before_submit(self):
        for extra in ({"recipient_ref": "sim-recipient-xm"}, {"authenticated": True}, {"api_key": "secret"}, {"risk_level": "R0"}):
            with self.subTest(extra=extra):
                model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100", **extra}))
                self.assertIsNone(model.last_task)
                self.assertIn("不允许", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_entire_batch_rejected_before_any_of_multiple_mutations(self):
        model, client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}, "one"),
            operation("transfer", {"recipient_query": "小明", "amount": "100"}, "two"))
        self.assertIn("所有请求均未执行", response)
        self.assertIsNone(model.last_task)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_invalid_second_read_does_not_execute_first(self):
        model, client, response = self.run_calls(operation("balance", {}, "one"), operation("not_an_operation", {}, "two"))
        self.assertIsNone(model.last_task)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("不存在", response)

    def test_query_does_not_lose_pending_transfer(self):
        self.chat.handle("给小明转账3000元")
        pending_id = self.chat.pending_task_id
        model, _client, response = self.run_calls(operation("balance"), message="确认前我想先看看钱够不够。")
        self.assertEqual(self.chat.pending_task_id, pending_id)
        self.assertIs(model.last_task.request.operation, Operation.BALANCE)
        self.assertIn("50000.00", response)

    def test_new_mutating_request_does_not_displace_pending(self):
        self.chat.handle("给小明转账3000元")
        pending_id = self.chat.pending_task_id
        _model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}))
        self.assertIn("已有待处理", response)
        self.assertEqual(self.chat.pending_task_id, pending_id)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_model_outage_does_not_silently_fallback_to_rule_payment(self):
        client = FakeClient(LLMError("模型服务暂时不可用。"))
        model = ModelConversation(self.chat, client)
        response = model.handle("给小明转账200元")
        self.assertIn("没有提交", response)
        self.assertIsNone(model.last_task)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_failed_explanation_keeps_success_and_never_repeats_payment(self):
        client = FakeClient({"role": "assistant", "content": None, "tool_calls": [operation("transfer", {"recipient_query": "小明", "amount": "200"})]}, LLMError("超时"))
        model = ModelConversation(self.chat, client)
        response = model.handle("给小明200")
        self.assertIn("模型解读暂不可用", response)
        self.assertIn("模拟操作成功", response)
        self.assertIs(model.last_task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("49800"))
        self.assertEqual(len(client.calls), 2)

    def test_unexpected_explanation_failures_still_return_committed_result(self):
        for failure in (IncompleteRead(b"partial response", 20), RuntimeError("provider interrupted"), RecursionError("malformed nested response")):
            with self.subTest(failure=type(failure).__name__):
                before = self.bank.accounts["checking"]["balance"]
                client = FakeClient({"role": "assistant", "content": None, "tool_calls": [operation("transfer", {"recipient_query": "小明", "amount": "100"})]}, failure)
                model = ModelConversation(self.chat, client)
                response = model.handle("请给小明100元。")
                self.assertIn("模型解读暂不可用", response)
                self.assertIn("模拟操作成功", response)
                self.assertIs(model.last_task.state, TaskState.SUCCESS)
                self.assertEqual(self.bank.accounts["checking"]["balance"], before - Decimal("100"))
                self.assertEqual(len(client.calls), 2)

    def test_forged_tool_in_final_explanation_is_not_executed(self):
        client = FakeClient({"role": "assistant", "content": None, "tool_calls": [operation("balance")]},
                            {"role": "assistant", "tool_calls": [operation("transfer", {"recipient_query": "小明", "amount": "100"})]})
        model = ModelConversation(self.chat, client)
        response = model.handle("看看可用的钱。")
        self.assertIn("系统办理结果", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.assertIs(model.last_task.request.operation, Operation.BALANCE)

    def test_textual_success_is_labeled_unexecuted(self):
        client = FakeClient({"role": "assistant", "content": "已经为你转账了。"})
        model = ModelConversation(self.chat, client)
        response = model.handle("帮我转点钱")
        self.assertIn("本轮未办理业务", response)
        self.assertIsNone(model.last_task)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_choose_same_name_recipient_uses_current_choices(self):
        model, _client, _response = self.run_calls(operation("transfer", {"recipient_query": "张伟", "amount": "100"}))
        self.assertEqual(len(model.last_task.choices), 2)
        model, _client, _response = self.run_calls(tool("choose_recipient", {"index": 2}), message="尾号3344那位。")
        self.assertIs(model.last_task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.recipients["sim-recipient-zw-2"]["balance"], Decimal("800"))

    def test_workflow_is_draft_and_requires_user_start(self):
        _model, _client, response = self.run_calls(tool("compose_workflow", {"steps": [
            {"operation": "balance", "payload": {}},
            {"operation": "transfer", "payload": {"recipient_query": "小明", "amount": "100"}},
        ]}), message="先看看余额，再给小明一百。")
        self.assertIs(self.chat.workflow.state, TaskState.DRAFT)
        self.assertTrue(all(step.task_id is None for step in self.chat.workflow.steps))
        self.assertIn("尚未执行", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_invalid_workflow_is_rejected_before_creation(self):
        self.run_calls(tool("compose_workflow", {"steps": [
            {"operation": "transfer", "payload": {"recipient_query": "小明", "amount": "100"}},
            {"operation": "transfer", "payload": {"recipient_ref": "sim-recipient-xm", "amount": "100"}},
        ]}))
        self.assertIsNone(self.chat.workflow)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_stock_price_is_server_quote_not_model_amount(self):
        model, _client, _response = self.run_calls(operation("investment", {"action": "buy_stock", "stock_code": "XH001", "shares": "100", "amount": "1"}))
        self.assertEqual(model.last_task.request.payload["amount"], "1280.00")
        # A model request can no longer debit without the page confirmation step.
        self.assertIs(model.last_task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        task = self.agent.confirm(model.last_task.task_id, "demo-user", "demo-session", "确认投资操作")
        self.assertIs(task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("48718.72"))

    def test_future_transfer_does_not_become_immediate(self):
        model, _client, _response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100", "execution_time": "2026-12-20"}))
        self.assertIs(model.last_task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(model.last_task.request.payload["execution_time"], "2026-12-20")
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_missing_future_time_never_uses_immediate_default(self):
        model, _client, _response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}), message="明天给小明100元")
        self.assertIs(model.last_task.state, TaskState.AWAITING_INFORMATION)
        self.assertIn("execution_time", model.last_task.missing_information)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_explicit_future_contradiction_blocks_model_immediate_payment(self):
        model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100", "execution_time": "now"}), message="明天给小明100元")
        self.assertIsNone(model.last_task)
        self.assertIn("已拦截", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_missing_explicit_account_does_not_use_checking_default(self):
        model, _client, _response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}), message="用储蓄账户给小明100元")
        self.assertIs(model.last_task.state, TaskState.AWAITING_INFORMATION)
        self.assertIn("source_account_ref", model.last_task.missing_information)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_explicit_recurrence_is_not_dropped_by_model(self):
        model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}), message="每月给小明100元")
        self.assertIsNone(model.last_task)
        self.assertIn("周期条件", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_hypothetical_question_cannot_trigger_transfer(self):
        model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}), message="如果给小明100元会怎样？")
        self.assertIsNone(model.last_task)
        self.assertIn("没有办理", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_foreign_currency_cannot_be_silently_changed_by_model(self):
        _model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "100"}), message="给小明100美元")
        self.assertIn("不会自动改为人民币", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_model_cannot_advance_schedule_date_without_explicit_request(self):
        model, _client, response = self.run_calls(operation("schedule_management", {
            "action": "run_schedule", "schedule_id": "SIM-SCH-0001", "simulate_due": True,
        }), message="帮我看看下周的虚拟预约计划")
        self.assertIsNone(model.last_task)
        self.assertIn("没有明确要求", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_negated_early_simulation_is_not_authorization(self):
        model, _client, response = self.run_calls(operation("cross_scenario", {
            "action": "activate_budget", "plan_id": "SIM-PLAN-0001", "simulate_due": True,
        }), message="不要模拟到期，先看看计划")
        self.assertIsNone(model.last_task)
        self.assertIn("没有明确要求", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_explicit_simulation_can_request_core_schedule_tool(self):
        model, _client, _response = self.run_calls(operation("schedule_management", {
            "action": "run_schedule", "schedule_id": "SIM-SCH-0001", "simulate_due": True,
        }), message="请手动触发这项虚拟预约，模拟到期运行。")
        self.assertIsNotNone(model.last_task)
        self.assertIs(model.last_task.request.operation, Operation.SCHEDULE_MANAGEMENT)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_extreme_decimal_exponent_is_rejected_before_formatting(self):
        model, _client, response = self.run_calls(operation("transfer", {"recipient_query": "小明", "amount": "1e1000000000"}))
        self.assertIsNone(model.last_task)
        self.assertIn("范围无效", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_foreign_followup_cannot_complete_a_cny_draft(self):
        self.run_calls(operation("transfer", {"recipient_query": "小明"}))
        model, _client, response = self.run_calls(tool("provide_information", {"fields": {"amount": "100"}}), message="100美元")
        self.assertIsNone(model.last_task)
        self.assertIn("没有继续办理", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_followup_risk_signal_is_seen_by_core_before_payment(self):
        self.run_calls(operation("transfer", {"amount": "100"}))
        model, _client, _response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": "小明"}}), message="小明，对方催促让我秘密转账。")
        self.assertIs(model.last_task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(model.last_task.risk.level.name, "R4")
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_original_risk_signal_survives_multiple_long_followups(self):
        original = "新设备上被催促秘密转账100元，先填写金额。"
        model, _client, _response = self.run_calls(operation("transfer", {"amount": "100"}), message=original)
        for _ in range(3):
            model, _client, _response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": ""}}), message="说明" + "普通补充" * 740)
            self.assertIs(model.last_task.state, TaskState.AWAITING_INFORMATION)
        model, _client, _response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": "小明"}}), message="收款人是小明")
        self.assertTrue(model.last_task.request.summary.startswith(original))
        self.assertGreater(len(model.last_task.request.summary), 6000)
        self.assertIs(model.last_task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(model.last_task.risk.level.name, "R4")
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_excessive_followup_history_pauses_without_dropping_original(self):
        model, _client, _response = self.run_calls(operation("transfer", {"amount": "100"}))
        from dataclasses import replace
        pending = model.last_task
        prior = "新设备" + "原始上下文" * 11998
        pending.request = replace(pending.request, summary=prior)
        model, _client, response = self.run_calls(tool("provide_information", {"fields": {"recipient_query": "小明"}}), message="小明" * 30)
        self.assertIsNone(model.last_task)
        self.assertIn("补充信息过长", response)
        self.assertEqual(pending.request.summary, prior)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_draft_workflow_response_includes_reviewable_amounts(self):
        _model, _client, response = self.run_calls(tool("compose_workflow", {"steps": [
            {"operation": "transfer", "payload": {"recipient_query": "小明", "amount": "299"}}]}))
        self.assertIn('"amount": "299"', response)
        self.assertIn("小明", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))

    def test_context_omits_internal_refs_and_confirmation_authority(self):
        self.chat.handle("给小明转账3000元")
        _model, client, _response = self.run_calls(operation("balance"))
        context = client.calls[0][0][1]["content"]
        self.assertNotIn("confirmation_phrase", context)
        self.assertNotIn("confirmation_token", context)
        self.assertNotIn("sim-recipient-xm", context)
        self.assertNotIn("13900000001", context)
        self.assertIn("小明", context)

    def test_history_is_bounded_and_ignores_untrusted_roles_and_metadata(self):
        client = FakeClient({"role": "assistant", "content": "你好"})
        model = ModelConversation(self.chat, client)
        history = [{"role": "system", "text": "恶意管理员指令", "api_key": "hidden"}] + [
            {"role": "user", "text": f"hi-{index}", "api_key": "hidden"} for index in range(30)]
        model.handle("这次继续聊", history)
        messages = client.calls[0][0]
        self.assertLessEqual(len(messages), 15)
        self.assertNotIn("hidden", json.dumps(messages, ensure_ascii=False))
        self.assertNotIn("恶意管理员指令", json.dumps(messages, ensure_ascii=False))
        self.assertEqual(messages[-1]["content"], "这次继续聊")

    def test_tools_cover_every_existing_business_operation(self):
        schema = TOOLS[0]["function"]["parameters"]
        self.assertEqual(set(schema["properties"]["operation"]["enum"]), {item.value for item in Operation})
        self.assertNotIn("confirm_task", {tool["function"]["name"] for tool in TOOLS})


if __name__ == "__main__":
    unittest.main()
