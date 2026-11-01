from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from banking_agent.audit import AuditError
from banking_agent.demo import DemoConversation, build_demo_agent
from banking_agent.models import Operation, OperationRequest, TaskState


class SyntheticStockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent, self.bank = build_demo_agent(str(Path(self.temp.name) / "stocks.jsonl"))
        self.chat = DemoConversation(self.agent, self.bank)

    def tearDown(self):
        self.temp.cleanup()

    def buy_and_confirm(self, request):
        task = self.agent.submit(request)
        if task.state is TaskState.AWAITING_CONFIRMATION:
            task = self.agent.confirm(task.task_id, request.user_id, request.session_id, "确认投资操作")
        return task

    def buy_request(self, **overrides):
        payload = {"action": "buy_stock", "stock_code": "XH001", "shares": 100,
                   "currency": "CNY", "source_account_ref": "checking", "amount": "1280.00"}
        payload.update(overrides)
        return OperationRequest(Operation.INVESTMENT, "demo-user", "demo-session",
                                "一键买入虚构股票 XH001 100股", payload,
                                request_id="stock-client-0001", channel="app")

    def test_catalog_buy_fill_fees_and_holdings_are_consistent(self):
        self.chat.handle("查看股票行情")
        catalog_task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        first = catalog_task.result["data"]["stocks"][0]
        self.assertEqual((first["stock_code"], first["lot_amount"], first["lot_fee"], first["lot_total"]),
                         ("XH001", "1280.00", "1.28", "1281.28"))
        self.chat.handle("一键买入 XH001 100股")
        pending = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(pending.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.chat.handle("确认投资操作")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(task.state, TaskState.SUCCESS)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("48718.72"))
        self.assertEqual(task.result["stock_order"]["status"], "filled")
        self.assertEqual(task.result["stock_order"]["holding_shares"], 100)
        self.chat.handle("股票持仓")
        holding_task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        holding = holding_task.result["data"]["stock_holdings"][0]
        self.assertEqual((holding["shares"], holding["cost_basis"], holding["market_value"], holding["unrealized_pnl"]),
                         (100, "1281.28", "1280.00", "-1.28"))
        self.chat.handle("投资持仓")
        fund_task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(fund_task.result["data"]["holdings"], [])

    def test_replay_uses_existing_order_and_two_distinct_buys_accumulate(self):
        request = self.buy_request()
        first = self.agent.submit(request)
        self.assertEqual(first.state, TaskState.AWAITING_CONFIRMATION)
        replay = self.agent.submit(request)
        self.assertEqual(replay.task_id, first.task_id)
        self.assertEqual(len(self.bank.stock_market.orders), 0)
        first = self.agent.confirm(first.task_id, "demo-user", "demo-session", "确认投资操作")
        self.assertEqual(first.state, TaskState.SUCCESS)
        self.assertEqual(len(self.bank.stock_market.orders), 1)
        second = self.buy_and_confirm(replace(request, request_id="stock-client-0002"))
        self.assertEqual(second.state, TaskState.SUCCESS)
        self.assertEqual(len(self.bank.stock_market.orders), 2)
        self.assertEqual(self.bank.stock_market.positions["XH001"]["shares"], 200)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("47437.44"))

    def test_reservations_insufficient_cash_and_invalid_parameters_do_not_create_orders(self):
        self.bank.reservations["checking"] = Decimal("49000")
        task = self.buy_and_confirm(self.buy_request())
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error_code, "SIM_INSUFFICIENT_FUNDS")
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.bank.reservations.clear()
        cases = [{"stock_code": "XH999"}, {"shares": 0}, {"shares": -100}, {"shares": "100.5"},
                 {"shares": 10}, {"shares": True}, {"source_account_ref": "savings"}, {"amount": "1"}]
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                request = replace(self.buy_request(**fields), request_id=f"invalid-stock-{index}")
                task = self.buy_and_confirm(request)
                self.assertNotEqual(task.state, TaskState.SUCCESS)
                self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.assertEqual(self.bank.stock_market.orders, [])
        self.assertEqual(self.bank.stock_market.positions, {})

    def test_authentication_and_risk_review_cannot_be_skipped_by_one_click(self):
        task = self.agent.submit(replace(self.buy_request(), channel="im"))
        self.assertEqual(task.state, TaskState.AWAITING_AUTHENTICATION)
        risky = replace(self.buy_request(), request_id="stock-risk-0001", summary="投资群催促一键买入 XH001 100股")
        task = self.agent.submit(risky)
        self.assertEqual(task.state, TaskState.AWAITING_CONFIRMATION)
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        task = self.agent.confirm(task.task_id, "demo-user", "demo-session", "确认投资操作")
        self.assertEqual(task.state, TaskState.UNDER_REVIEW)
        self.assertEqual(self.bank.stock_market.orders, [])

    def test_audit_failure_prevents_stock_trade(self):
        original_append = self.agent.audit.append
        def fail_on_execution(event):
            if event.get("tool_name") == "execution":
                raise AuditError("test audit failure")
            return original_append(event)
        self.agent.audit.append = fail_on_execution
        task = self.buy_and_confirm(self.buy_request())
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.error_code, "audit_unavailable")
        self.assertEqual(self.bank.accounts["checking"]["balance"], Decimal("50000"))
        self.assertEqual(self.bank.stock_market.orders, [])


if __name__ == "__main__":
    unittest.main()
