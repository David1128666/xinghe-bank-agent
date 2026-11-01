"""Fictional fixed-price shares for the local teaching demo only."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class SyntheticStockMarket:
    def __init__(self):
        self.catalog = {
            "XH001": {"stock_name": "星河科技（虚构）", "price": Decimal("12.80"), "sector": "虚构科技"},
            "XH002": {"stock_name": "云朵能源（虚构）", "price": Decimal("26.50"), "sector": "虚构能源"},
            "XH003": {"stock_name": "青禾生活（虚构）", "price": Decimal("8.60"), "sector": "虚构消费"},
        }
        self.fee_rate = Decimal("0.001")
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []

    def quote(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        code = str(payload.get("stock_code", "")).upper()
        if code not in self.catalog:
            raise ValueError("仅支持内置虚构股票 XH001、XH002、XH003")
        raw_shares = payload.get("shares")
        if isinstance(raw_shares, bool) or not str(raw_shares).isdigit():
            raise ValueError("买入股数必须是 100 的正整数倍")
        shares = int(raw_shares)
        if shares <= 0 or shares > 1_000_000 or shares % 100:
            raise ValueError("买入股数必须是 100 的正整数倍，且不超过 100 万股")
        if payload.get("source_account_ref", "checking") != "checking":
            raise ValueError("股票演示使用活期账户尾号 1234")
        stock = self.catalog[code]
        principal = stock["price"] * shares
        fee = (principal * self.fee_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if payload.get("amount") not in (None, "") and Decimal(str(payload["amount"])) != principal:
            raise ValueError("请求金额与虚构股票报价不一致")
        return {"stock_code": code, "stock_name": stock["stock_name"], "shares": shares,
                "price": money(stock["price"]), "amount": money(principal), "fee": money(fee),
                "total": money(principal + fee), "currency": "CNY"}

    def listings(self) -> list[dict[str, Any]]:
        rows = []
        for code, stock in self.catalog.items():
            quote = self.quote({"stock_code": code, "shares": 100})
            rows.append({"stock_code": code, "stock_name": stock["stock_name"],
                         "price": quote["price"], "sector": stock["sector"], "lot_size": 100,
                         "fee_rate": str(self.fee_rate), "lot_amount": quote["amount"],
                         "lot_fee": quote["fee"], "lot_total": quote["total"],
                         "quote_kind": "固定虚构样例价，不是实时行情"})
        return rows

    def fill(self, quote: dict[str, Any]) -> dict[str, Any]:
        code = quote["stock_code"]
        position = self.positions.setdefault(code, {"shares": 0, "cost_basis": Decimal("0")})
        position["shares"] += quote["shares"]
        position["cost_basis"] += Decimal(quote["total"])
        order = {**quote, "order_id": f"SIM-STK-{len(self.orders) + 1:04d}",
                 "holding_shares": position["shares"], "status": "filled",
                 "environment": "纯虚构教学成交"}
        self.orders.append(order)
        return dict(order)

    def holdings(self) -> list[dict[str, Any]]:
        rows = []
        for code, position in self.positions.items():
            market_value = self.catalog[code]["price"] * position["shares"]
            rows.append({"stock_code": code, "stock_name": self.catalog[code]["stock_name"],
                         "shares": position["shares"], "average_cost": money(position["cost_basis"] / position["shares"]),
                         "cost_basis": money(position["cost_basis"]), "market_value": money(market_value),
                         "unrealized_pnl": money(market_value - position["cost_basis"])})
        return rows
