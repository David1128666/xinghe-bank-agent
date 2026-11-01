from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from banking_agent.demo import DemoConversation, build_demo_agent
from banking_agent.audit import AuditError
from banking_agent.models import Operation, TaskState


class VirtualDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent, self.bank = build_demo_agent(str(Path(self.temp.name) / "virtual-audit.jsonl"))
        self.chat = DemoConversation(self.agent, self.bank)

    def tearDown(self):
        self.temp.cleanup()

    def test_balance_is_synthetic_and_read_only(self):
        response = self.chat.handle("查余额")
        self.assertIn("纯虚拟模拟", response)
        self.assertIn("50000.00", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], 50000)

    def test_transfer_ambiguity_confirmation_and_simulated_execution(self):
        start_balance = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("现在从活期尾号1234转账50元给张伟用途AA")
        self.assertIn("多个模拟账户", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start_balance)

        response = self.chat.handle("2")
        self.assertIn("请核对操作摘要", response)
        self.assertIn("尾号 3344", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start_balance)

        response = self.chat.handle("确认转账")
        self.assertIn("模拟操作成功", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start_balance - 50)
        self.assertTrue(response.endswith("。"))

    def test_scam_like_transfer_is_reviewed_and_not_executed(self):
        start_balance = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("现在从活期尾号1234转账50元给小明用途安全账户")
        self.assertIn("请核对操作摘要", response)
        response = self.chat.handle("确认转账")
        self.assertIn("等待审核", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start_balance)
        review_ref = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session").transaction_reference
        self.assertIn("取消", self.chat.handle("取消"))
        self.assertEqual(self.bank._review_status[review_ref], "cancelled")
        self.assertEqual(self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session").state, TaskState.CANCELLED)

    def test_virtual_card_lock_requires_preview_confirmation(self):
        response = self.chat.handle("锁卡尾号9012")
        self.assertIn("请核对操作摘要", response)
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "active")
        response = self.chat.handle("确认卡片操作")
        self.assertIn("模拟操作成功", response)
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "temporarily_locked")

    def test_debit_stop_does_not_cancel_virtual_merchant_contract(self):
        response = self.chat.handle("停止代扣月光音乐")
        self.assertIn("请核对操作摘要", response)
        response = self.chat.handle("确认订阅操作")
        self.assertIn("模拟操作成功", response)
        self.assertEqual(self.bank.subscriptions["moon-music"]["bank_debit"], "stopped")
        self.assertEqual(self.bank.subscriptions["moon-music"]["merchant_contract"], "active")

    def test_budget_reminder_and_restored_debit_are_confirmed_without_immediate_charge(self):
        balance = self.bank.accounts["checking"]["balance"]
        self.assertIn("只设置支出目标标签", self.chat.handle("设置预算标签交通1000元"))
        self.assertEqual(self.bank.budget_labels, {})
        self.chat.handle("确认预算标签")
        self.assertEqual(len(self.bank.budget_labels), 1)
        self.chat.handle("创建提醒2026-11-01内容核对月度账单")
        self.assertEqual(self.bank.reminders, [])
        self.chat.handle("确认创建提醒")
        self.assertIn("核对月度账单", self.chat.handle("查看提醒"))
        self.bank.subscriptions["moon-music"]["bank_debit"] = "stopped"
        self.chat.handle("恢复代扣月光音乐")
        self.assertEqual(self.bank.subscriptions["moon-music"]["bank_debit"], "stopped")
        self.chat.handle("确认订阅操作")
        self.assertEqual(self.bank.subscriptions["moon-music"]["bank_debit"], "active")
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)

    def test_simulated_review_rejection_is_audited_without_debit(self):
        balance = self.bank.accounts["checking"]["balance"]
        self.chat.handle("现在从活期转账10000元给小明用途礼物")
        self.chat.handle("确认转账")
        self.assertIn("manual_review_declined", self.chat.handle("模拟审核拒绝"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)
        self.assertIn('"result":"declined"', self.agent.audit.path.read_text(encoding="utf-8"))

    def test_bill_period_anomalies_and_category_correction_are_simulated(self):
        response = self.chat.handle("分析2026-10账单")
        self.assertIn("SIM-TX-105", response)
        self.assertIn("需要核实的异常特征", response)
        self.assertIn("category_labels_are_estimates", response)
        response = self.chat.handle("把SIM-TX-105分类改为交通")
        self.assertIn("请核对操作摘要", response)
        self.assertIn("购物（推测） → 交通", response)
        self.chat.handle("确认修改分类")
        row = next(item for item in self.bank.transactions if item["id"] == "SIM-TX-105")
        self.assertEqual(row["category"], "交通")

    def test_scheduled_transfer_is_registered_without_debit_and_can_be_cancelled(self):
        start = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("定时转账2026-11-01从活期转账100元给小明用途房租")
        self.assertIn("登记模拟定时转账", response)
        self.chat.handle("确认转账")
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)
        self.assertIn("SCHEDULED", self.chat.handle("任务状态"))
        self.assertIn("SIM-SCH-0001", self.chat.handle("查看定时转账"))
        response = self.chat.handle("取消定时任务SIM-SCH-0001")
        self.assertIn("请核对操作摘要", response)
        self.chat.handle("确认计划操作")
        self.assertEqual(self.bank.scheduled_transfers["SIM-SCH-0001"]["status"], "cancelled")

    def test_recurring_transfer_requires_an_end_and_runs_only_when_manually_simulated(self):
        start = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("每月定时转账2026-11-01从活期转账100元给小明每月执行3次用途房租")
        self.assertIn("周期执行次数", response)
        self.chat.handle("确认转账")
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)
        response = self.chat.handle("模拟运行计划SIM-SCH-0001")
        self.assertIn("手动模拟触发", response)
        self.chat.handle("确认计划操作")
        self.assertEqual(self.bank.accounts["checking"]["balance"], start - 100)
        self.assertEqual(self.bank.scheduled_transfers["SIM-SCH-0001"]["runs"], 1)
        self.assertEqual(self.bank.scheduled_transfers["SIM-SCH-0001"]["status"], "scheduled")

    def test_aa_request_never_debits_other_person_and_payment_requires_confirmation(self):
        recipient_balance = self.bank.recipients["sim-recipient-xm"]["balance"]
        response = self.chat.handle("AA收款50元给小明用途晚餐")
        self.assertIn("不会扣取对方资金", response)
        self.chat.handle("确认AA操作")
        self.assertEqual(self.bank.recipients["sim-recipient-xm"]["balance"], recipient_balance)
        self.assertIn("SIM-AA-0001", self.chat.handle("查看AA请求列表"))

        account_balance = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("支付AA请求SIM-AA-0000")
        self.assertIn("请核对操作摘要", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], account_balance)
        self.chat.handle("确认AA操作")
        self.assertEqual(self.bank.accounts["checking"]["balance"], account_balance - 20)
        self.assertEqual(self.bank.aa_requests["SIM-AA-0000"]["status"], "paid")

    def test_phone_resolution_and_new_recipient_review(self):
        response = self.chat.handle("现在从活期转账25元给13900000001用途午餐")
        self.assertIn("小明", response)
        self.assertIn("请核对操作摘要", response)
        self.chat.handle("确认转账")

        response = self.chat.handle("现在从活期转账25元给林女士用途礼物")
        self.assertIn("请核对操作摘要", response)
        response = self.chat.handle("确认转账")
        self.assertIn("等待审核", response)

    def test_investment_profile_product_matching_purchase_redemption_and_progress(self):
        response = self.chat.handle("风险测评 地区：虚构区，目标：长期增长，期限：3年，流动性：中，R2，经验：新手，金额：5000元，适当性限制：无")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(task.result["data"]["suitable_product_codes"], ["P-BOND-SIM"])
        start = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("申购P-BOND-SIM 100元")
        self.assertIn("请核对操作摘要", response)
        self.assertIn("处理中", self.chat.handle("确认投资操作"))
        order_id = self.bank.investment_orders.keys().__iter__().__next__()
        self.assertEqual(self.bank.investment_orders[order_id]["status"], "processing")
        self.assertLess(self.bank.accounts["checking"]["balance"], start)
        self.assertIn(order_id, self.chat.handle(f"投资进度{order_id}"))
        self.assertNotIn("P-BOND-SIM", self.bank.holdings)
        self.assertIn("手动模拟结算", self.chat.handle(f"模拟结算投资{order_id}"))
        self.chat.handle("确认投资操作")
        self.assertEqual(self.bank.holdings["P-BOND-SIM"], 100)

        before_redemption = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("赎回P-BOND-SIM 50元")
        self.assertIn("模拟赎回", response)
        self.chat.handle("确认投资操作")
        self.assertEqual(self.bank.holdings["P-BOND-SIM"], 50)
        self.assertEqual(self.bank.accounts["checking"]["balance"], before_redemption)
        self.chat.handle("模拟结算投资SIM-INV-0002")
        self.chat.handle("确认投资操作")
        self.assertEqual(self.bank.accounts["checking"]["balance"], before_redemption + Decimal("49.85"))

    def test_card_application_controls_subscription_alerts_and_price_change_query(self):
        response = self.chat.handle("申请卡")
        self.assertIn("新的虚拟演示卡", response)
        self.chat.handle("确认卡片操作")
        self.assertIn("等待虚拟制卡", self.chat.handle("查询申请进度"))

        response = self.chat.handle("关闭线上交易尾号9012")
        self.assertIn("目标限制", response)
        self.chat.handle("确认卡片操作")
        self.assertFalse(self.bank.cards["sim-card-1"]["controls"]["online"])

        response = self.chat.handle("开启月光音乐价格变化提醒")
        self.assertIn("请核对操作摘要", response)
        self.chat.handle("确认订阅操作")
        self.assertTrue(self.bank.subscriptions["moon-music"]["price_alert"])
        query = self.chat.handle("查看订阅列表")
        self.assertIn("active_monthly_total", query)
        self.assertIn("price_change_alerts", query)

    def test_bounded_cross_scenario_plan_reserves_budget_and_obeys_scope(self):
        start = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle(
            "创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额600元"
            "虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A"
        )
        self.assertIn("请核对操作摘要", response)
        self.assertIn("600.00 CNY", response)
        response = self.chat.handle("确认本次计划")
        self.assertIn("SCHEDULED", response)
        plan_id = "SIM-PLAN-0001"
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)
        self.assertEqual(self.bank.reservations["checking"], 1000)

        response = self.chat.handle(f"模拟到期执行计划{plan_id}商户虚拟花店商品鲜花和蛋糕总价500元")
        self.assertIn("模拟操作成功", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start - 500)
        self.assertEqual(self.bank.reservations, {})
        self.assertEqual(self.bank.cross_plans[plan_id]["status"], "completed")

    def test_daily_limit_counts_aa_schedules_and_all_accounts_only_for_today(self):
        self.agent.config = replace(self.agent.config, daily_transfer_limit="200")
        self.bank.config = self.agent.config
        today = self.bank._today()
        self.bank.transfer_totals[(today - timedelta(days=1), "checking")] = Decimal("1000")
        self.bank.transfer_totals[(today, "savings")] = Decimal("50")
        self.chat.handle("现在从活期转账25元给小明用途午餐")
        self.chat.handle("确认转账")
        self.chat.handle("AA收款50元给小明用途晚餐")
        self.chat.handle("确认AA操作")
        self.assertEqual(self.bank._daily_transfer_total(), 75)
        self.chat.handle("支付AA请求SIM-AA-0000")
        self.chat.handle("确认AA操作")
        self.chat.handle("定时转账2026-11-01从活期转账100元给小明用途房租")
        self.chat.handle("确认转账")
        self.assertEqual(self.bank._daily_transfer_total(), 95)
        self.chat.handle("模拟运行计划SIM-SCH-0001")
        self.chat.handle("确认计划操作")
        self.assertEqual(self.bank._daily_transfer_total(), 195)
        balance = self.bank.accounts["checking"]["balance"]
        self.chat.handle("现在从活期转账5元给小明用途午餐")
        self.assertIn("等待审核", self.chat.handle("确认转账"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)

    def test_savings_reservation_is_visible_and_blocks_both_transfer_paths(self):
        balance = self.bank.accounts["savings"]["balance"]
        self.bank.reservations["savings"] = balance - 10
        self.chat.handle("查余额")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        savings = next(row for row in task.result["data"]["accounts"] if row["account_last4"] == "5678")
        self.assertEqual(savings["available_balance"], "10.00")
        self.chat.handle("现在从储蓄转账20元给小明用途午餐")
        self.assertIn("SIM_INSUFFICIENT_FUNDS", self.chat.handle("确认转账"))
        self.chat.handle("定时转账2026-11-01从储蓄转账20元给小明用途房租")
        self.chat.handle("确认转账")
        self.chat.handle("模拟运行计划SIM-SCH-0001")
        self.assertIn("SIM_SCHEDULE_NOT_RUNNABLE", self.chat.handle("确认计划操作"))
        self.assertEqual(self.bank.accounts["savings"]["balance"], balance)

    def test_schedule_rechecks_changed_recipient_risk_before_debit(self):
        self.chat.handle("定时转账2026-11-01从活期转账100元给小明用途房租")
        self.chat.handle("确认转账")
        self.bank.recipients["sim-recipient-xm"]["known"] = False
        balance = self.bank.accounts["checking"]["balance"]
        self.chat.handle("模拟运行计划SIM-SCH-0001")
        self.assertIn("等待审核", self.chat.handle("确认计划操作"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)
        self.assertEqual(self.bank.scheduled_transfers["SIM-SCH-0001"]["runs"], 0)

    def test_annual_and_custom_bill_ranges_do_not_leak_other_dates(self):
        self.chat.handle("看2026年账单")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertTrue(task.result["data"]["recurring_expenses"])
        self.assertTrue(all(row["date"].startswith("2026") for row in task.result["data"]["transactions"]))
        self.chat.handle("分析2026-9-1到2026-9-30账单")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(task.result["data"]["total_spend"], "72.00")
        self.assertEqual(len(task.result["data"]["transactions"]), 3)
        self.assertIsNotNone(task.result["data_as_of"])
        self.assertFalse(task.result["includes_pending"])

    def test_permanent_loss_cannot_be_undone_through_temporary_lock_or_unlock(self):
        self.chat.handle("关闭无卡交易尾号9012")
        self.chat.handle("确认卡片操作")
        self.assertFalse(self.bank.cards["sim-card-1"]["controls"]["card_not_present"])
        response = self.chat.handle("挂失尾号9012")
        self.assertIn("原卡不可恢复", response)
        self.chat.handle("确认卡片操作")
        self.assertIn("操作未完成", self.chat.handle("解锁尾号9012"))
        self.assertIn("操作未完成", self.chat.handle("临时锁卡尾号9012"))
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "permanently_blocked")

    def test_dispute_submission_is_confirmed_and_does_not_refund_or_change_card(self):
        balance = self.bank.accounts["checking"]["balance"]
        self.assertIn("仅提交审核", self.chat.handle("提交争议SIM-TX-105原因不认识这笔交易"))
        self.assertEqual(self.bank.disputes, {})
        self.assertIn("处理中", self.chat.handle("确认提交争议"))
        self.assertEqual(self.bank.disputes["SIM-DSP-0001"]["refund_status"], "not_refunded")
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "active")
        self.assertIn("SIM-DSP-0001", self.chat.handle("查询争议进度"))

    def test_duplicate_order_lines_cannot_bypass_category_budget(self):
        self.chat.handle("创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额700元虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A")
        self.chat.handle("确认本次计划")
        balance = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("模拟到期执行计划SIM-PLAN-0001鲜花300元@虚拟花店鲜花100元@虚拟花店")
        self.assertIn("请核对操作摘要", response)
        self.assertIn("SIM_SCOPE_BOUNDARY_EXCEEDED", self.chat.handle("确认本次计划"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)

    def test_scope_authorization_never_overrides_new_fraud_signal(self):
        self.chat.handle("创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额600元虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A")
        self.chat.handle("确认本次计划")
        balance = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle("模拟到期执行计划SIM-PLAN-0001商户虚拟花店商品鲜花总价200元，对方催促我付款")
        self.assertIn("请核对操作摘要", response)
        self.assertIn("等待审核", self.chat.handle("确认本次计划"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)

    def test_suitability_treats_months_as_months_and_obeys_investable_amount(self):
        self.chat.handle("风险测评 地区：虚构区，目标：长期增长，期限：6个月，流动性：中，R2，经验：新手，金额：50元，适当性限制：无")
        task = self.agent.get_task(self.chat.pending_task_id, "demo-user", "demo-session")
        self.assertEqual(task.result["data"]["suitable_product_codes"], [])
        self.chat.handle("风险测评 地区：虚构区，目标：长期增长，期限：3年，流动性：中，R2，经验：新手，金额：50元，适当性限制：无")
        balance = self.bank.accounts["checking"]["balance"]
        self.chat.handle("申购P-BOND-SIM 100元")
        self.assertIn("等待审核", self.chat.handle("确认投资操作"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)

    def test_future_budget_is_only_reserved_after_explicit_virtual_activation(self):
        self.chat.handle("创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额600元虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A，预留开始2026-11-01")
        self.chat.handle("确认本次计划")
        self.assertEqual(self.bank.reservations, {})
        self.assertIn("手动模拟激活", self.chat.handle("模拟预留计划SIM-PLAN-0001"))
        self.chat.handle("确认本次计划")
        self.assertEqual(self.bank.reservations["checking"], 1000)

    def test_continuous_grant_keeps_remaining_budget_until_its_confirmed_count(self):
        self.chat.handle("创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元持续授权最大金额600元最多执行2次虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A")
        self.chat.handle("确认本次计划")
        balance = self.bank.accounts["checking"]["balance"]
        self.assertIn("模拟操作成功", self.chat.handle("模拟到期执行计划SIM-PLAN-0001商户虚拟花店商品鲜花总价200元"))
        self.assertEqual(self.bank.cross_plans["SIM-PLAN-0001"]["status"], "scheduled")
        self.assertEqual(self.bank.reservations["checking"], 800)
        self.assertIn("模拟操作成功", self.chat.handle("模拟到期执行计划SIM-PLAN-0001商户虚拟花店商品鲜花总价150元"))
        self.assertEqual(self.bank.cross_plans["SIM-PLAN-0001"]["status"], "completed")
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance - 350)
        self.assertEqual(self.bank.reservations, {})

    def test_order_audit_failure_stops_later_order_without_resubmitting_first(self):
        self.chat.handle("创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额700元虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A")
        self.chat.handle("确认本次计划")
        balance = self.bank.accounts["checking"]["balance"]
        audit = self.agent.audit

        class RejectSecondOrderAudit:
            hash_identifier = audit.hash_identifier

            def append(self, event):
                if event.get("step_id") == "order-2" and event.get("action") == "tool_called":
                    raise AuditError("simulated per-order audit outage")
                return audit.append(event)

        self.bank.audit = RejectSecondOrderAudit()
        response = self.chat.handle("模拟到期执行计划SIM-PLAN-0001鲜花300元@虚拟花店蛋糕200元@虚拟蛋糕店")
        self.assertIn("处理中", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance - 300)
        self.assertEqual(len(self.bank.cross_plans["SIM-PLAN-0001"]["purchase_results"]), 1)
        self.chat.handle("刷新状态")
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance - 300)

    def test_composed_workflow_keeps_confirmation_and_blocks_dependent_changes_on_failure(self):
        response = self.chat.handle("组合计划：查余额；现在从活期转账25元给小明用途午餐；临时锁卡尾号9012")
        self.assertIn("请核对操作摘要", response)
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "active")
        self.chat.handle("确认转账")
        self.assertIn("请核对操作摘要", self.chat.handle("继续组合计划"))
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "active")
        self.chat.handle("确认卡片操作")
        self.chat.handle("继续组合计划")
        self.assertEqual(self.chat.workflow.state, TaskState.SUCCESS)

        self.bank.accounts["checking"]["balance"] = Decimal("1")
        self.chat.handle("组合计划：查余额；现在从活期转账25元给小明用途午餐；解锁尾号9012")
        self.chat.handle("确认转账")
        self.chat.handle("继续组合计划")
        self.assertEqual(self.chat.workflow.state, TaskState.PARTIAL_SUCCESS)
        self.assertEqual(self.chat.workflow.steps[-1].state, "NOT_EXECUTED")
        self.assertEqual(self.bank.cards["sim-card-1"]["status"], "temporarily_locked")

    def test_sensitive_read_requires_app_auth_and_invalid_input_never_executes(self):
        self.chat.handle("切换到即时通讯")
        self.assertIn("认证级别不足", self.chat.handle("新设备查看流水"))
        # A chat sentence can no longer raise the authentication level.
        self.assertIn("不能只靠一句指令", self.chat.handle("切换到APP"))
        code = self.chat.auth_challenge.code
        self.assertIn("查询完成", self.chat.handle(f"认证 {code}"))
        balance = self.bank.accounts["checking"]["balance"]
        self.assertIn("格式无效", self.chat.handle("现在从活期转账-50元给小明用途午餐"))
        self.assertIn("格式无效", self.chat.handle("定时转账2026-02-30从活期转账50元给小明用途午餐"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], balance)

    def test_scope_overrun_is_never_executed_and_channel_switch_invalidates_confirmation(self):
        start = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle(
            "创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额600元"
            "虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点A"
        )
        self.assertIn("请核对操作摘要", response)
        self.chat.handle("确认本次计划")
        response = self.chat.handle("模拟到期执行计划SIM-PLAN-0001商户虚拟花店商品鲜花和蛋糕总价700元")
        self.assertIn("请核对操作摘要", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)
        self.chat.handle("确认本次计划")
        self.assertIn("SIM_SCOPE_BOUNDARY_EXCEEDED", self.chat.handle("任务状态"))
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)

        transfer = self.chat.handle("现在从活期转账25元给小明用途礼物")
        self.assertIn("请核对操作摘要", transfer)
        response = self.chat.handle("切换到即时通讯")
        self.assertIn("认证级别不足", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)
        response = self.chat.handle(f"认证 {self.chat.auth_challenge.code}")
        self.assertIn("请核对操作摘要", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start)
        self.chat.handle("确认转账")
        self.assertEqual(self.bank.accounts["checking"]["balance"], start - 25)

    def test_calendar_access_requires_explicit_consent_and_never_guesses_relationship(self):
        response = self.chat.handle("查看模拟日历")
        self.assertIn("authorization_required", response)
        self.assertNotIn("SIM-CAL-001", response)
        consent = self.chat.handle("授权读取模拟日历")
        self.assertIn("已记录", consent)
        response = self.chat.handle("查看模拟日历")
        self.assertIn("SIM-CAL-001", response)
        self.assertIn("不代表任何亲属身份", response)
        self.assertIn("calendar_access_authorized", self.agent.audit.path.read_text(encoding="utf-8"))

    def test_cross_scenario_partial_orders_are_recorded_without_auto_retry(self):
        start = self.bank.accounts["checking"]["balance"]
        response = self.chat.handle(
            "创建生日计划2026-11-10活期预留1000元鲜花350元蛋糕200元范围授权最大金额700元"
            "虚拟花店虚拟缺货商户鲜花蛋糕虚拟配送点A"
        )
        self.assertIn("请核对操作摘要", response)
        self.chat.handle("确认本次计划")
        response = self.chat.handle(
            "模拟到期执行计划SIM-PLAN-0001鲜花300元@虚拟花店蛋糕200元@虚拟缺货商户"
        )
        self.assertIn("部分完成", response)
        self.assertIn("未完成步骤未自动重试", response)
        self.assertEqual(self.bank.accounts["checking"]["balance"], start - 300)
        self.assertEqual(self.bank.cross_plans["SIM-PLAN-0001"]["status"], "partial_success")
        self.assertEqual(self.bank.cross_plans["SIM-PLAN-0001"]["purchase_results"][1]["status"], "failed")
        self.assertEqual(self.bank.reservations["checking"], 700)
        response = self.chat.handle("取消计划SIM-PLAN-0001")
        self.assertIn("700.00 CNY", response)
        self.chat.handle("确认本次计划")
        self.assertEqual(self.bank.reservations, {})


if __name__ == "__main__":
    unittest.main()
