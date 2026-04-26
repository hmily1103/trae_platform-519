"""
一键任务 hooks 测试：on_unified_monkey_finished（Monkey 完成后联动结束性能监控）。

用 mock 隔离 performance_monitor 与 report_store，只测 hooks 逻辑。
运行：pytest tests/test_unified_hooks.py -v
"""

import pytest
from unittest.mock import patch, MagicMock


class TestOnUnifiedMonkeyFinished:
    """on_unified_monkey_finished 行为"""

    def test_no_op_when_run_id_empty(self):
        """run_id 为空时直接返回，不抛错"""
        from modules.unified.hooks import on_unified_monkey_finished
        on_unified_monkey_finished("")
        on_unified_monkey_finished(None)

    def test_no_op_when_run_not_found(self):
        """run 不存在时直接返回"""
        from modules.unified.hooks import on_unified_monkey_finished
        on_unified_monkey_finished("nonexistent_run_xyz")

    def test_no_op_when_no_performance_monitor_child(self):
        """run 不包含 performance_monitor 子任务时不调用 set_child 更新 perf"""
        from shared.unified.orchestrator import create_run, get_run, set_child
        from modules.unified.hooks import on_unified_monkey_finished

        run_id = create_run({"modules": ["monkey"]})
        set_child(run_id, "monkey", {"device_key": "192.168.1.1:8787", "report_id": "r1"})
        children_before = dict((get_run(run_id) or {}).get("children") or {})

        on_unified_monkey_finished(run_id)

        children_after = (get_run(run_id) or {}).get("children") or {}
        assert "performance_monitor" not in children_after

    def test_updates_performance_monitor_child_when_present(self):
        """run 包含 performance_monitor 时，调用后该 child 被标记 finished_by_unified"""
        from shared.unified.orchestrator import create_run, get_run, set_child
        from modules.unified.hooks import on_unified_monkey_finished

        run_id = create_run({"modules": ["monkey", "performance_monitor"]})
        set_child(run_id, "monkey", {"report_id": "m1"})
        set_child(run_id, "performance_monitor", {"task_id": "perf_1", "session_id": "s1"})

        with patch("modules.performance_monitor.views.PERFORMANCE_SERVICE_LOCK"):
            with patch("modules.performance_monitor.views.PERFORMANCE_SERVICE") as mock_svc:
                mock_svc.stop_monitoring.return_value = True
                mock_svc.storage.get_session.return_value = {"metadata": {"device_id": "d1", "package_name": "p1"}}
                mock_svc.storage.get_statistics.return_value = {"snapshot_count": 0}
                with patch("modules.unified.hooks.get_unified_report_store") as mock_store:
                    mock_store.return_value.save_report.return_value = f"perf_{run_id}_perf_1"
                    on_unified_monkey_finished(run_id)

        run = get_run(run_id)
        perf = (run.get("children") or {}).get("performance_monitor")
        assert perf is not None
        assert perf.get("finished_by_unified") is True
        assert perf.get("unified_report_id") is not None
