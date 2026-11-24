from __future__ import annotations

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ai_quant.chanlun import POLICY_VERSION, AcceptanceResult
from ai_quant.cli import (
    _chanlun_acceptance_path,
    _chanlun_reference_passed,
    _chanlun_policy_sha256,
    _file_sha256,
    _symbol_signature,
    command_chanlun_scan,
    command_chanlun_validate,
)
from ai_quant.config import Settings


def _rows(count: int = 8) -> list[dict[str, object]]:
    start = date(2026, 8, 10)
    return [
        {
            "trade_date": (start + timedelta(days=index)).isoformat(),
            "open": 10 + index, "high": 10.5 + index,
            "low": 9.5 + index, "close": 10 + index, "amount": 1_000,
        }
        for index in range(count)
    ]


class _MarketStore:
    def bars(self, symbol, adjustment, limit, end_date=None):
        rows = _rows()
        return [row for row in rows if end_date is None or row["trade_date"] <= end_date.isoformat()]


class _Bridge:
    def scan_recent_prefixes(self, bars_by_symbol, as_of, *, sessions):
        return {(symbol, as_of): [] for symbol in bars_by_symbol}

    def scan(self, bars_by_symbol):
        return {symbol: [] for symbol in bars_by_symbol}

    def replay_batches(self, bars_by_symbol):
        return {symbol: [] for symbol in bars_by_symbol}


class ChanlunCliTests(unittest.TestCase):
    def test_scan_is_local_only_and_never_needs_an_agent_runner(self):
        settings = Settings(Path("."), Path("runtime"), Path("runtime/audit.sqlite3"))
        output = io.StringIO()
        with (
            patch("ai_quant.cli._settings", return_value=settings),
            patch("ai_quant.cli._market_store", return_value=_MarketStore()),
            patch("ai_quant.cli._chanlun_bridge", return_value=_Bridge()),
            redirect_stdout(output),
        ):
            code = command_chanlun_scan(Namespace(
                symbols=["600000"], as_of="2026-08-17", limit=100,
                valid_sessions=5, max_candidates=20,
            ))
        result = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(result["local_only"])
        self.assertEqual([], result["candidates"])
        self.assertIn("未调用 Agent", result["note"])

    def test_validate_labels_insufficient_sample_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root, root / "runtime", root / "runtime/audit.sqlite3")
            insufficient = AcceptanceResult(
                annualized_return=0.1, maximum_drawdown=-0.1, completed_trades=2,
                history_days=20, sufficient_sample=False, passed=False,
                trades=(), equity_curve=(),
            )
            output = io.StringIO()
            with (
                patch("ai_quant.cli._settings", return_value=settings),
                patch("ai_quant.cli._market_store", return_value=_MarketStore()),
                patch("ai_quant.cli._chanlun_bridge", return_value=_Bridge()),
                patch("ai_quant.cli.run_acceptance", return_value=insufficient),
                redirect_stdout(output),
            ):
                code = command_chanlun_validate(Namespace(
                    symbols=["600000"], all=False, start=None, end=None, limit=100,
                ))
            result = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertEqual("insufficient_sample", result["status"])
            self.assertTrue(result["reference_only"])
            self.assertFalse(result["reference_criteria_met"])
            self.assertTrue(Path(result["acceptance_receipt"]).is_file())
            self.assertIn("样本不足", result["note"])

    def test_settings_resolves_default_config_and_environment_bridge_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "system.json").write_text(
                json.dumps({"chanlun_bridge_path": "custom/bridge.exe"}), encoding="utf-8"
            )
            self.assertEqual(root / "custom/bridge.exe", Settings.load(root).chanlun_bridge_path)
            with patch.dict("os.environ", {"AI_QUANT_CHANLUN_BRIDGE_PATH": "env/bridge.exe"}):
                settings = Settings.load(root)
        self.assertEqual(root / "env/bridge.exe", settings.chanlun_bridge_path)
        self.assertEqual("shadow", settings.chanlun_mode)

    def test_active_mode_requires_matching_full_universe_receipt(self):
        class Store:
            def security_master_rows(self, *, active_only=True):
                return [{"symbol": "600000"}, {"symbol": "000001"}]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "bridge.exe"
            bridge.write_bytes(b"version-one")
            settings = Settings(
                root, root / "runtime", root / "runtime/audit.sqlite3",
                chanlun_bridge_path=bridge, chanlun_mode="active",
            )
            self.assertFalse(_chanlun_reference_passed(settings, Store()))
            receipt_path = _chanlun_acceptance_path(settings)
            receipt_path.parent.mkdir(parents=True)
            policy_dir = root / "src/ai_quant"
            policy_dir.mkdir(parents=True)
            (policy_dir / "chanlun.py").write_text("policy", encoding="utf-8")
            (policy_dir / "features.py").write_text("features", encoding="utf-8")
            receipt_path.write_text(json.dumps({
                "schema_version": "chanlun-acceptance/v1",
                "reference_criteria_met": True,
                "algorithm": {
                    "bridge_sha256": _file_sha256(bridge),
                    "policy_version": POLICY_VERSION,
                    "policy_sha256": _chanlun_policy_sha256(settings),
                },
                "universe": {
                    "kind": "active_security_master",
                    "symbol_signature": _symbol_signature(["000001", "600000"]),
                },
                "result": {"passed": True, "sufficient_sample": True},
            }), encoding="utf-8")
            self.assertTrue(_chanlun_reference_passed(settings, Store()))
            (policy_dir / "features.py").write_text("changed", encoding="utf-8")
            self.assertFalse(_chanlun_reference_passed(settings, Store()))
            (policy_dir / "features.py").write_text("features", encoding="utf-8")
            bridge.write_bytes(b"version-two")
            self.assertFalse(_chanlun_reference_passed(settings, Store()))
