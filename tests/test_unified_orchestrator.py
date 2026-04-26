"""
一键任务编排器（orchestrator）TDD 风格测试。

用法：先写这里的测试描述「期望行为」，再在 shared/unified/orchestrator 中实现（红→绿→重构）。
运行：pytest tests/test_unified_orchestrator.py -v
"""

import pytest


class TestOrchestratorCreateAndGet:
    """create_run / get_run 行为"""

    def test_create_run_returns_non_empty_run_id(self):
        """create_run 返回非空 run_id"""
        from shared.unified.orchestrator import create_run
        run_id = create_run({"modules": ["monkey"]})
        assert run_id
        assert isinstance(run_id, str)
        assert run_id.startswith("unified_")

    def test_get_run_returns_run_after_create(self):
        """create 后 get_run 能拿到同一份 run"""
        from shared.unified.orchestrator import create_run, get_run
        payload = {"modules": ["monkey"], "device_id": "192.168.1.1:8787"}
        run_id = create_run(payload)
        run = get_run(run_id)
        assert run is not None
        assert run["run_id"] == run_id
        assert run.get("request") == payload
        assert run.get("status") == "running"
        assert run.get("children") == {}

    def test_get_run_returns_none_for_unknown_id(self):
        """未知 run_id 时 get_run 返回 None"""
        from shared.unified.orchestrator import get_run
        assert get_run("nonexistent_id_12345") is None


class TestOrchestratorSetChild:
    """set_child 行为"""

    def test_set_child_stores_child_metadata(self):
        """set_child 后 children 中能拿到该模块信息"""
        from shared.unified.orchestrator import create_run, get_run, set_child
        run_id = create_run({"modules": ["monkey"]})
        set_child(run_id, "monkey", {"device_key": "192.168.1.1:8787", "report_id": "r1"})
        run = get_run(run_id)
        assert run["children"]["monkey"] == {"device_key": "192.168.1.1:8787", "report_id": "r1"}

    def test_set_child_overwrites_same_module(self):
        """同一 module 再次 set_child 会覆盖"""
        from shared.unified.orchestrator import create_run, get_run, set_child
        run_id = create_run({})
        set_child(run_id, "monkey", {"report_id": "r1"})
        set_child(run_id, "monkey", {"report_id": "r2"})
        run = get_run(run_id)
        assert run["children"]["monkey"]["report_id"] == "r2"


class TestOrchestratorUpdateRun:
    """update_run 行为"""

    def test_update_run_updates_status(self):
        """update_run 可更新 status"""
        from shared.unified.orchestrator import create_run, get_run, update_run
        run_id = create_run({})
        update_run(run_id, status="finished")
        run = get_run(run_id)
        assert run["status"] == "finished"


class TestOrchestratorRemoveRun:
    """remove_run 行为"""

    def test_remove_run_deletes_run(self):
        """remove_run 后 get_run 返回 None"""
        from shared.unified.orchestrator import create_run, get_run, remove_run
        run_id = create_run({})
        assert get_run(run_id) is not None
        removed = remove_run(run_id)
        assert removed is True
        assert get_run(run_id) is None

    def test_remove_run_returns_false_for_unknown_id(self):
        """未知 run_id 时 remove_run 返回 False"""
        from shared.unified.orchestrator import remove_run
        assert remove_run("nonexistent_xyz") is False


class TestOrchestratorAddError:
    """add_error 行为"""

    def test_add_error_appends_to_errors(self):
        """add_error 会在 run 的 errors 中追加一条"""
        from shared.unified.orchestrator import create_run, get_run, add_error
        run_id = create_run({})
        add_error(run_id, "monkey", "device not connected")
        run = get_run(run_id)
        assert len(run["errors"]) == 1
        assert run["errors"][0]["module"] == "monkey"
        assert run["errors"][0]["error"] == "device not connected"


class TestRunHasPerformanceMonitor:
    """run_has_performance_monitor 辅助函数（TDD：先测后实现）"""

    def test_run_has_performance_monitor_when_child_present(self):
        """run 包含 performance_monitor 子任务时返回 True"""
        from shared.unified.orchestrator import create_run, set_child, run_has_performance_monitor
        run_id = create_run({"modules": ["monkey", "performance_monitor"]})
        set_child(run_id, "performance_monitor", {"task_id": "perf_1", "session_id": "s1"})
        assert run_has_performance_monitor(run_id) is True

    def test_run_has_performance_monitor_when_absent(self):
        """run 不包含 performance_monitor 时返回 False"""
        from shared.unified.orchestrator import create_run, run_has_performance_monitor
        run_id = create_run({"modules": ["monkey"]})
        assert run_has_performance_monitor(run_id) is False

    def test_run_has_performance_monitor_when_run_missing(self):
        """run_id 不存在时返回 False"""
        from shared.unified.orchestrator import run_has_performance_monitor
        assert run_has_performance_monitor("nonexistent_run_xyz") is False
