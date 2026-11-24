import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_quant.account import AccountLedger, build_fill_payload, record_confirmed_fill
from ai_quant.rules import Side
from ai_quant.securities import SecurityType
from ai_quant.store import AuditStore


class AccountLedgerTests(unittest.TestCase):
    def initial(self, cash=10000):
        return [{
            "event_id": "init-v1", "event_type": "account_initialized",
            "payload": {"version": "v1", "capital_budget": 50000, "cash": cash,
                        "positions": []},
        }]

    def test_buy_is_t_plus_one_and_updates_cash_and_cost(self):
        fill = build_fill_payload(
            symbol="563020", name="红利低波ETF易方", security_type=SecurityType.ETF,
            side=Side.BUY, quantity=100, price=Decimal("1.168"),
        )
        state = AccountLedger.reduce(self.initial() + [{
            "event_id": "fill-1", "event_type": "fill", "payload": fill,
        }])
        self.assertAlmostEqual(9882.2, state["cash"])
        self.assertEqual(100, state["positions"][0]["quantity"])
        self.assertEqual(0, state["positions"][0]["available_quantity"])
        self.assertAlmostEqual(1.178, state["positions"][0]["cost_price"])

    def test_unknown_cash_stays_unknown_but_position_changes(self):
        fill = build_fill_payload(
            symbol="600000", name="浦发银行", security_type=SecurityType.STOCK,
            side=Side.BUY, quantity=100, price=Decimal("10"),
        )
        state = AccountLedger.reduce(self.initial(None) + [{
            "event_id": "fill-1", "event_type": "fill", "payload": fill,
        }])
        self.assertIsNone(state["cash"])
        self.assertEqual(100, state["positions"][0]["quantity"])

    def test_sell_rejects_more_than_available(self):
        events = self.initial()
        events[0]["payload"]["positions"] = [{
            "symbol": "563020", "name": "ETF", "security_type": "etf",
            "quantity": 100, "available_quantity": 0, "cost_price": 1.0,
        }]
        fill = build_fill_payload(
            symbol="563020", name="ETF", security_type=SecurityType.ETF,
            side=Side.SELL, quantity=100, price=Decimal("1.1"),
        )
        with self.assertRaisesRegex(ValueError, "超过可用数量"):
            AccountLedger.reduce(events + [{
                "event_id": "fill-1", "event_type": "fill", "payload": fill,
            }])

    def test_partial_sell_and_broker_reconciliation(self):
        events = self.initial(1000)
        events[0]["payload"]["positions"] = [{
            "symbol": "600000", "name": "浦发银行", "security_type": "stock",
            "quantity": 200, "available_quantity": 200, "cost_price": 9.0,
        }]
        sell = build_fill_payload(
            symbol="600000", name="浦发银行", security_type=SecurityType.STOCK,
            side=Side.SELL, quantity=100, price=Decimal("10"),
        )
        state = AccountLedger.reduce(events + [
            {"event_id": "sell-1", "event_type": "fill", "payload": sell},
            {"event_id": "reconcile-1", "event_type": "position_reconciled", "payload": {
                "symbol": "600000", "name": "浦发银行", "security_type": "stock",
                "quantity": 100, "available_quantity": 90, "frozen_quantity": 10,
                "cost_price": 9.0,
            }},
        ])
        self.assertEqual(100, state["positions"][0]["quantity"])
        self.assertEqual(90, state["positions"][0]["available_quantity"])
        self.assertAlmostEqual(1998.5, state["cash"])

    def test_linked_partial_fills_update_order_atomically_and_charge_one_commission(self):
        with TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.db")
            store.initialize()
            store.append_account_event(
                "init-v1", "account_initialized", "2026-08-24T09:00:00+08:00",
                {"version": "v1", "capital_budget": 50000, "cash": 10000,
                 "positions": []},
            )
            store.create_run(
                "run-1", "2026-08-24T09:00:00+08:00", "snapshot-hash",
            )
            store.record_order_plan({
                "plan_id": "plan-1", "run_id": "run-1", "account_version": "v1",
                "status": "approved", "orders": [{
                    "order_id": "order-1", "symbol": "600000", "side": "buy",
                    "quantity": 100,
                }],
            })
            store.append_order_state_event(
                "state-0", "order-1", "plan-1", None, "submitted",
                "2026-08-24T09:30:00+08:00", "用户确认已提交",
            )

            first = build_fill_payload(
                symbol="600000", name="浦发银行", security_type=SecurityType.STOCK,
                side=Side.BUY, quantity=40, price=Decimal("10"),
            )
            state = record_confirmed_fill(
                store, account_event_id="fill-1", state_event_id="state-1",
                event_time="2026-08-24T09:31:00+08:00", payload=first,
                order_id="order-1",
            )
            self.assertEqual("partially_filled", store.current_order_state("order-1")["to_state"])
            self.assertEqual(40, store.linked_filled_quantity("order-1"))
            self.assertEqual(9599.0, state["cash"])

            second = build_fill_payload(
                symbol="600000", name="浦发银行", security_type=SecurityType.STOCK,
                side=Side.BUY, quantity=60, price=Decimal("10"),
            )
            state = record_confirmed_fill(
                store, account_event_id="fill-2", state_event_id="state-2",
                event_time="2026-08-24T09:32:00+08:00", payload=second,
                order_id="order-1",
            )
            self.assertEqual("filled", store.current_order_state("order-1")["to_state"])
            self.assertEqual(100, store.linked_filled_quantity("order-1"))
            self.assertEqual(8999.0, state["cash"])
            self.assertAlmostEqual(10.01, state["positions"][0]["cost_price"])
            fills = [event for event in store.list_account_events()
                     if event["event_type"] == "fill"]
            self.assertEqual("1", fills[0]["payload"]["commission"])
            self.assertEqual("0", fills[1]["payload"]["commission"])

    def test_linked_fill_rejects_overfill_without_writing_event(self):
        with TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.db")
            store.initialize()
            store.append_account_event(
                "init-v1", "account_initialized", "2026-08-24T09:00:00+08:00",
                {"version": "v1", "capital_budget": 50000, "cash": 10000,
                 "positions": []},
            )
            store.create_run("run-1", "2026-08-24T09:00:00+08:00", "hash")
            store.record_order_plan({
                "plan_id": "plan-1", "run_id": "run-1", "account_version": "v1",
                "status": "approved", "orders": [{
                    "order_id": "order-1", "symbol": "600000", "side": "buy",
                    "quantity": 100,
                }],
            })
            store.append_order_state_event(
                "state-0", "order-1", "plan-1", None, "submitted",
                "2026-08-24T09:30:00+08:00", "用户确认已提交",
            )
            fill = build_fill_payload(
                symbol="600000", name="浦发银行", security_type=SecurityType.STOCK,
                side=Side.BUY, quantity=101, price=Decimal("10"),
            )
            with self.assertRaisesRegex(ValueError, "超过订单剩余数量"):
                record_confirmed_fill(
                    store, account_event_id="fill-1", state_event_id="state-1",
                    event_time="2026-08-24T09:31:00+08:00", payload=fill,
                    order_id="order-1",
                )
            self.assertEqual(1, len(store.list_account_events()))
            self.assertEqual("submitted", store.current_order_state("order-1")["to_state"])


if __name__ == "__main__":
    unittest.main()
