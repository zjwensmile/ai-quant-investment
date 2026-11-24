import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_quant.cli import build_parser, command_history_sync
from ai_quant.config import Settings
from ai_quant.tasks import TaskController


class HistoryPerformanceTests(unittest.TestCase):
    def test_history_configuration_is_independent_of_model_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config' / 'system.json').write_text(json.dumps({
                'max_parallel_agents': 1, 'history_workers': 4,
                'history_request_delay_seconds': 0.25,
            }), encoding='utf-8')
            settings = Settings.load(root)
            self.assertEqual(1, settings.max_parallel_agents)
            self.assertEqual(4, settings.history_workers)
            self.assertEqual(0.25, settings.history_request_delay_seconds)
            controller = TaskController(settings, Mock(), Mock())
            plan = {'needed': [{'symbol': '600000', 'bootstrap': False, 'start': '2026-09-03'}],
                    'through': '2026-09-04', 'unavailable': []}
            remaining = {**plan, 'needed': []}
            snapshot = {'securities': [{'symbol': '600000'}]}
            with patch.object(controller, '_history_plan', side_effect=[(root, snapshot, plan), (root, snapshot, remaining)]), patch.object(
                controller, '_run_workflow_command', return_value=subprocess.CompletedProcess([], 0, 'ok', '')
            ) as run:
                result = controller._refresh_history('offline')
            self.assertEqual(0, result.returncode)
            command = run.call_args.args[0]
            self.assertEqual('4', command[command.index('--workers') + 1])
            self.assertEqual('0.25', command[command.index('--delay') + 1])
            self.assertIn('--incremental-window', command)
            with patch('ai_quant.tasks._latest', return_value=root / 'snapshot.json'):
                standalone = controller._command('history')
            self.assertEqual('4', standalone[standalone.index('--workers') + 1])
            self.assertEqual('0.25', standalone[standalone.index('--delay') + 1])

    def test_cli_accepts_four_workers_and_rejects_invalid_delay_before_storage(self):
        parser = build_parser()
        args = parser.parse_args(['history-sync', '--snapshot', 'unused', '--workers', '4', '--delay', '-1'])
        with patch('ai_quant.cli._settings', return_value=Settings(Path('.'), Path('.'), Path('unused'))), patch(
            'ai_quant.cli._market_store'
        ) as store, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, command_history_sync(args))
        store.assert_not_called()

    def test_load_rejects_unbounded_history_concurrency_or_invalid_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'config').mkdir()
            for value in ({'history_workers': 0}, {'history_workers': 5},
                          {'history_request_delay_seconds': -1}, {'history_request_delay_seconds': float('inf')}):
                with self.subTest(value=value):
                    (root / 'config' / 'system.json').write_text(json.dumps(value), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        Settings.load(root)
