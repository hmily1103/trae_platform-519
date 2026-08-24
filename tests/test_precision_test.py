from unittest.mock import MagicMock, patch

from flask import Flask

from modules.precision_test import precision_test_bp
from modules.precision_test.engine import (
    apply_execution_results,
    calculate_quality_gate,
    extract_json_object,
    normalize_analysis,
)
from modules.precision_test.store import PrecisionTestStore
from modules.precision_test.git_source import (
    detect_release_baseline,
    normalize_repository_url,
    parse_repository_input,
    validate_ref,
)
from modules.precision_test.views import _extract_diff_summary


def _client():
    app = Flask(__name__)
    app.register_blueprint(precision_test_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _model():
    diff = "--- a/services/song.py\n+++ b/services/song.py\n-old()\n+new_queue()\n"
    return normalize_analysis(
        {},
        requirement="新增点歌队列逻辑，重复点歌必须幂等",
        code_diff=diff,
        project_type="vod",
        version="V1.0",
        summary=_extract_diff_summary(diff),
        environment={"stb_model": "X9"},
    )


def test_extract_diff_summary_collects_vod_risks():
    diff = """--- a/app/player.py
+++ b/app/player.py
@@ -1,2 +1,3 @@
-return old_song
+return playback_queue
+raise TimeoutError()
"""
    summary = _extract_diff_summary(diff)
    assert summary["files"] == ["app/player.py"]
    assert summary["additions"] == 2
    assert summary["deletions"] == 1
    labels = {item["label"] for item in summary["risk_signals"]}
    assert "点歌与队列" in labels
    assert "播放链路" in labels
    assert "未发现测试代码变更" in labels


def test_extract_diff_summary_classifies_ai_tooling_as_no_runtime():
    diff = """--- /dev/null
+++ b/.claude/hooks/hooks.json
@@ -0,0 +1,3 @@
+{"hooks":[]}
--- /dev/null
+++ b/.claude/README.md
@@ -0,0 +1,2 @@
+AI collaboration notes
"""
    summary = _extract_diff_summary(diff)
    assert summary["change_type"] == "ai_tooling"
    assert summary["runtime_impact"] == "none"
    assert "未发现测试代码变更" not in {item["label"] for item in summary["risk_signals"]}


def test_extract_json_object_handles_fenced_json():
    assert extract_json_object('```json\n{"risks": []}\n```') == {"risks": []}


def test_git_url_normalization_accepts_gitlab_tree_url():
    raw = (
        "https://g.ktvsky.com/vod/ThunderNetVod/-/tree/dev-%CF%802-rk3576-standard-x9"
    )
    url = normalize_repository_url(raw)
    assert url == "https://g.ktvsky.com/vod/ThunderNetVod.git"
    assert parse_repository_input(raw)["target_ref"] == "dev-π2-rk3576-standard-x9"
    assert validate_ref("dev-π2-rk3576-standard-x9", "目标分支") == "dev-π2-rk3576-standard-x9"


def test_git_url_parses_markdown_link():
    parsed = parse_repository_input(
        "[分支](https://g.ktvsky.com/vod/ThunderNetVod/-/tree/dev-main)"
    )
    assert parsed["repository_url"].endswith("/ThunderNetVod.git")
    assert parsed["target_ref"] == "dev-main"


def test_git_url_rejects_embedded_credentials():
    try:
        normalize_repository_url("https://user:pass@g.ktvsky.com/vod/repo.git")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "不能包含账号或密码" in str(exc)


@patch("modules.precision_test.git_source._fetch_ref")
@patch("modules.precision_test.git_source._ensure_cache", return_value="cache")
@patch("modules.precision_test.git_source._run_git")
def test_detect_release_baseline_selects_latest_reachable_release(mock_run, _mock_cache, _mock_fetch):
    mock_run.side_effect = [
        "",
        "target-sha\n",
        (
            "2025-04-15\tstable_V5.1.1.6110\tbase-new\n"
            "2024-12-26\tstable_V5.1.1.6102\tbase-old\n"
        ),
    ]
    result = detect_release_baseline("https://example.com/team/repo.git", "dev-x9")
    assert result["base_commit"] == "base-new"
    assert result["tag"] == "stable_V5.1.1.6110"
    assert result["confidence"] == "medium"


@patch("modules.precision_test.git_source._fetch_ref")
@patch("modules.precision_test.git_source._ensure_cache", return_value="cache")
@patch("modules.precision_test.git_source._run_git")
def test_build_git_diff_latest_commit_uses_first_parent(mock_run, _mock_cache, _mock_fetch):
    from modules.precision_test.git_source import build_git_diff

    mock_run.side_effect = [
        "target-sha\n",
        "parent-sha\n",
        "target-sha\tdev\t2026-06-12\tfix player\n",
        "abc\tdev\tfix player\n",
        "M\tsrc/player.py\n",
        "diff --git a/src/player.py b/src/player.py\n+fixed\n",
    ]
    result = build_git_diff(
        "https://example.com/team/repo.git",
        "feature",
        comparison_mode="latest_commit",
    )
    assert result["base_sha"] == "parent-sha"
    assert result["comparison_mode"] == "latest_commit"
    assert result["included_files"] == ["src/player.py"]


def test_normalize_analysis_builds_minimum_vod_test_set_and_executors():
    model = _model()
    assert 10 <= len(model["test_points"]) <= 20
    assert len(model["execution_plan"]) == len(model["test_points"])
    assert model["change"]["environment"]["stb_model"] == "X9"
    assert all(item["executor"] for item in model["execution_plan"])
    assert model["quality_gate"]["decision"] == "BLOCKED"
    assert model["quality_gate"]["release_blocked"] is False


def test_normalize_analysis_shrinks_non_runtime_changes():
    diff = """--- /dev/null
+++ b/.claude/PLAN.md
@@ -0,0 +1,2 @@
+Claude Code support plan
"""
    summary = _extract_diff_summary(diff)
    model = normalize_analysis(
        {"risks": [{"category": "播放", "title": "AI误判播放风险", "priority": "P0"}]},
        requirement="新增 AI 协作配置",
        code_diff=diff,
        project_type="vod",
        version="V1.0",
        summary=summary,
    )
    assert len(model["risks"]) == 1
    assert model["risks"][0]["priority"] == "P2"
    assert len(model["test_points"]) == 3
    assert all(item["executor"] == "manual" for item in model["execution_plan"])


def test_tester_brief_explains_pad_play_control_and_box_model_risks():
    diff = """--- a/lib_main/PlayControlBarViewFactory.java
+++ b/lib_main/PlayControlBarViewFactory.java
@@ -1,2 +1,2 @@
-if (isRk3576Chip()) return new MultiModePlayControlBarView();
+if (isRk3576Chip() || isPadChip()) return new MultiModePlayControlBarView();
--- a/lib_common/AddressManager.java
+++ b/lib_common/AddressManager.java
@@ -1,2 +1,2 @@
-return KvManager.preferences().getString(MAIN_BOX_MODEL, "TS_KTV_X9");
+return "TS_KTV_X9";
"""
    summary = _extract_diff_summary(diff)
    model = normalize_analysis(
        {"risks": [{
            "category": "播放",
            "title": "Pad设备播控栏切换为MultiModePlayControlBarView",
            "priority": "P0",
            "impact_type": "direct",
            "scope": "Pad设备播放控制栏",
            "evidence": ["PlayControlBarViewFactory.create()", "getMainBoxModel() 固定返回 TS_KTV_X9"],
            "confidence": "high",
        }]},
        requirement="Pad 播控栏适配 X9",
        code_diff=diff,
        project_type="vod",
        version="V1.0",
        summary=summary,
    )
    brief = model["tester_brief"]
    assert any("播放控制栏" in item for item in brief["plain_summary"])
    assert any("固定返回 TS_KTV_X9" in item for item in brief["must_confirm"])
    assert any("退出" in item and "遮挡" in item for item in brief["verification_focus"])


def test_execution_routing_uses_fine_grained_keywords_and_evidence():
    diff = """--- a/lib_main/PlayControlBarViewFactory.java
+++ b/lib_main/PlayControlBarViewFactory.java
+return new MultiModePlayControlBarView();
"""
    summary = _extract_diff_summary(diff)
    model = normalize_analysis(
        {"risks": [{
            "category": "播放",
            "title": "Pad播控栏图层遮挡退出弹框",
            "priority": "P0",
            "impact_type": "direct",
            "scope": "Pad 播放页退出弹框",
            "evidence": ["PlayControlBarViewFactory.create()", "MultiModePlayControlBarView"],
            "confidence": "high",
        }], "test_points": [{
            "risk_ids": ["R01"],
            "priority": "P0",
            "type": "UI交互",
            "title": "验证播放页退出弹框不被三方应用图层遮挡",
            "steps": "播放中打开退出弹框并切换三方应用图层",
            "expected": "退出弹框始终在最上层且按钮可点击",
        }]},
        requirement="Pad 播控栏适配",
        code_diff=diff,
        project_type="vod",
        version="V1.0",
        summary=summary,
    )
    first = model["execution_plan"][0]
    assert first["executor"] in {"player_stress", "ui_automation"}
    assert "映射" not in first["routing_reason"]
    assert first["routing_reason"]


def test_execution_routing_prefers_api_stress_for_idempotent_service_cases():
    diff = """--- a/server/SongController.java
+++ b/server/SongController.java
+favoriteSong(songId);
"""
    summary = _extract_diff_summary(diff)
    model = normalize_analysis(
        {"risks": [{
            "category": "服务端",
            "title": "收藏接口重复请求导致幂等异常",
            "priority": "P1",
            "impact_type": "direct",
            "scope": "收藏接口",
            "evidence": ["SongController.favoriteSong"],
            "confidence": "high",
        }], "test_points": [{
            "risk_ids": ["R01"],
            "priority": "P1",
            "type": "接口场景",
            "title": "验证收藏接口重复请求幂等",
            "steps": "并发重复请求收藏接口",
            "expected": "无重复写入且返回码正确",
        }]},
        requirement="收藏接口幂等优化",
        code_diff=diff,
        project_type="vod",
        version="V1.0",
        summary=summary,
    )
    assert model["execution_plan"][0]["executor"] == "api_stress"
    assert "API 压测" in model["execution_plan"][0]["routing_reason"]


def test_gate_distinguishes_product_failure_and_environment_error():
    risks = [{"id": "R01", "priority": "P0"}]
    base = [{
        "id": "E01",
        "risk_ids": ["R01"],
        "status": "FAIL",
        "workaround": False,
    }]
    failed = calculate_quality_gate(base, risks, "enforce")
    assert failed["decision"] == "BLOCKED"
    assert failed["release_blocked"] is True

    base[0]["status"] = "ENV_ERROR"
    env_error = calculate_quality_gate(base, risks, "enforce")
    assert env_error["decision"] == "REVIEW_REQUIRED"
    assert env_error["release_blocked"] is False


def test_p1_failure_with_workaround_is_conditional_pass():
    risks = [{"id": "R01", "priority": "P1"}]
    execution = [{"id": "E01", "risk_ids": ["R01"], "status": "FAIL", "workaround": True}]
    gate = calculate_quality_gate(execution, risks, "observe")
    assert gate["decision"] == "CONDITIONAL_PASS"


def test_gate_truth_table_priority_for_key_combinations():
    cases = [
        (
            [{"id": "E01", "risk_ids": ["R01"], "status": "PENDING", "workaround": False},
             {"id": "E02", "risk_ids": ["R02"], "status": "ENV_ERROR", "workaround": False}],
            [{"id": "R01", "priority": "P0"}, {"id": "R02", "priority": "P2"}],
            [],
            "BLOCKED",
        ),
        (
            [{"id": "E01", "risk_ids": ["R01"], "status": "FAIL", "workaround": False},
             {"id": "E02", "risk_ids": ["R02"], "status": "ENV_ERROR", "workaround": False}],
            [{"id": "R01", "priority": "P1"}, {"id": "R02", "priority": "P2"}],
            [],
            "BLOCKED",
        ),
        (
            [{"id": "E01", "risk_ids": ["R01"], "status": "PASS", "workaround": False}],
            [{"id": "R01", "priority": "P0"}],
            [{"id": "C01", "priority": "P0", "status": "OPEN"}],
            "REVIEW_REQUIRED",
        ),
        (
            [{"id": "E01", "risk_ids": ["R01"], "status": "PASS", "workaround": False}],
            [{"id": "R01", "priority": "P0"}],
            [{"id": "C01", "priority": "P0", "status": "CONFIRMED"}],
            "PASS",
        ),
    ]
    for execution, risks, confirmations, expected in cases:
        gate = calculate_quality_gate(execution, risks, "observe", confirmations)
        assert gate["decision"] == expected


def test_apply_execution_results_rejects_unknown_status():
    model = _model()
    try:
        apply_execution_results(
            model,
            [{"execution_id": model["execution_plan"][0]["id"], "status": "BROKEN"}],
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "不支持的执行状态" in str(exc)


def test_apply_execution_results_records_audit_log():
    model = _model()
    first = model["execution_plan"][0]
    apply_execution_results(
        model,
        [{
            "execution_id": first["id"],
            "status": "PASS",
            "note": "人工验证通过",
            "source": "manual",
            "actor": "qa_user",
        }],
    )
    assert model["audit_log"][-1]["actor"] == "qa_user"
    assert model["audit_log"][-1]["before"]["status"] == "PENDING"
    assert model["audit_log"][-1]["after"]["status"] == "PASS"
    assert model["quality_gate"]["rule_version"] == "precision_gate_v1"


@patch("modules.precision_test.views.get_precision_test_store")
def test_finalize_blocks_open_high_risk_confirmations(mock_store):
    model = _model()
    model["confirmations"] = [{
        "id": "C01",
        "priority": "P0",
        "status": "OPEN",
        "title": "确认主盒型号硬编码",
    }]
    store = MagicMock()
    store.get.return_value = model
    mock_store.return_value = store

    response = _client().post(f"/precision_test/api/analyses/{model['analysis_id']}/finalize")

    assert response.status_code == 409
    assert "未关闭" in response.get_json()["message"]


@patch("modules.precision_test.views.get_precision_test_store")
def test_confirmation_update_recalculates_gate_and_records_audit(mock_store):
    model = _model()
    for item in model["execution_plan"]:
        item["status"] = "PASS"
    model["confirmations"] = [{
        "id": "C01",
        "priority": "P0",
        "status": "OPEN",
        "title": "确认主盒型号硬编码",
        "response": "",
        "evidence": [],
    }]
    model["quality_gate"] = calculate_quality_gate(
        model["execution_plan"], model["risks"], "observe", model["confirmations"]
    )
    store = MagicMock()
    store.get.return_value = model
    mock_store.return_value = store

    response = _client().post(
        f"/precision_test/api/analyses/{model['analysis_id']}/confirmations",
        json={
            "confirmation_id": "C01",
            "status": "CONFIRMED",
            "response": "研发确认该逻辑符合需求",
            "actor": "qa_lead",
        },
    )
    payload = response.get_json()["data"]

    assert response.status_code == 200
    assert payload["confirmations"][0]["status"] == "CONFIRMED"
    assert payload["audit_log"][-1]["action"] == "confirmation_update"
    assert payload["quality_gate"]["open_confirmations"] == 0


def test_precision_store_round_trip(tmp_path):
    store = PrecisionTestStore(str(tmp_path))
    model = _model()
    store.save(model)
    loaded = store.get(model["analysis_id"])
    assert loaded["analysis_id"] == model["analysis_id"]
    assert store.list()[0]["decision"] == "BLOCKED"


def test_analyze_rejects_invalid_project_type():
    response = _client().post(
        "/precision_test/api/analyze",
        json={"code_diff": "+ changed", "project_type": "desktop"},
    )
    assert response.status_code == 400


@patch("modules.precision_test.views.get_precision_test_store")
@patch("modules.precision_test.agent.run_risk_agent", return_value={"risks": [], "test_points": [], "agent_trace": [], "agent_error": "empty"})
def test_analyze_falls_back_when_agent_returns_empty(_mock_agent, mock_store):
    mock_store.return_value = MagicMock()
    response = _client().post(
        "/precision_test/api/analyze",
        json={
            "version": "V1.2",
            "requirement": "新增收藏歌曲能力",
            "project_type": "vod",
            "code_diff": "--- a/song.py\n+++ b/song.py\n+favorite(song_id)\n",
        },
    )
    payload = response.get_json()["data"]
    assert response.status_code == 200
    assert payload["analysis"]["analysis_source"] == "agent"
    assert len(payload["analysis"]["test_points"]) >= 10
    assert payload["analysis_id"].startswith("pt_")


@patch("modules.precision_test.views.get_precision_test_store")
@patch("modules.precision_test.agent.run_risk_agent")
def test_analyze_returns_structured_model(mock_agent, mock_store):
    mock_agent.return_value = {
        "risks": [{
            "category": "点歌",
            "title": "重复收藏",
            "priority": "P0",
            "impact_type": "direct",
            "evidence": ["song.py"],
            "confidence": "high",
        }],
        "test_points": [],
        "agent_trace": [{"step": "candidate", "status": "ok"}],
        "agent_error": "",
    }
    mock_store.return_value = MagicMock()
    response = _client().post(
        "/precision_test/api/analyze",
        json={"project_type": "vod", "code_diff": "+favorite(song_id)"},
    )
    payload = response.get_json()["data"]
    assert response.status_code == 200
    assert payload["analysis"]["analysis_source"] == "agent"
    assert payload["analysis"]["risks"][0]["priority"] == "P0"
    assert "impact_report" in payload


@patch("modules.precision_test.views.get_precision_test_store")
def test_sync_results_only_updates_bound_executor_reports(mock_store):
    app = Flask(__name__)
    app.register_blueprint(precision_test_bp)

    @app.route("/ui_automation/api/reports")
    def _ui_reports():
        return {
            "success": True,
            "data": {
                "reports": [{
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][0]["id"],
                    "status": "success",
                    "summary": "UI用例通过",
                    "report_url": "/ui_automation/report/1",
                }, {
                    "status": "success",
                    "summary": "未绑定报告不能自动回填",
                }]
            },
        }

    app.config["TESTING"] = True
    model = _model()
    for item in model["execution_plan"]:
        item["executor"] = "ui_automation"
        item["executor_name"] = "UI 自动化"
    store = MagicMock()
    store.get.return_value = model
    mock_store.return_value = store

    response = app.test_client().post(f"/precision_test/api/analyses/{model['analysis_id']}/sync_results")
    payload = response.get_json()["data"]

    assert response.status_code == 200
    assert payload["execution_plan"][0]["status"] == "PASS"
    assert payload["execution_plan"][0]["note"] == "UI用例通过"
    assert payload["sync_report"]["updated_count"] == 1


@patch("modules.precision_test.views.get_precision_test_store")
def test_sync_results_updates_bound_song_order_and_combined_reports(mock_store):
    app = Flask(__name__)
    app.register_blueprint(precision_test_bp)

    model = _model()
    model["execution_plan"][0]["executor"] = "song_order"
    model["execution_plan"][0]["executor_name"] = "点歌与搜索"
    model["execution_plan"][1]["executor"] = "combined_test"
    model["execution_plan"][1]["executor_name"] = "组合测试"

    @app.route("/song_order/api/history")
    def _song_history():
        return {
            "ok": True,
            "data": {
                "entries": [{
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][0]["id"],
                    "success": True,
                    "summary": "点歌请求成功",
                    "musicno": "10001",
                }]
            },
        }

    @app.route("/combined_test/api/reports")
    def _combined_reports():
        return {
            "success": True,
            "data": {
                "reports": [{
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][1]["id"],
                    "success": False,
                    "summary": "组合测试失败",
                    "report_url": "/combined_test/reports/report_demo.html",
                    "pipeline_id": "pipe01",
                }]
            },
        }

    app.config["TESTING"] = True
    store = MagicMock()
    store.get.return_value = model
    mock_store.return_value = store

    response = app.test_client().post(f"/precision_test/api/analyses/{model['analysis_id']}/sync_results")
    payload = response.get_json()["data"]

    assert response.status_code == 200
    assert payload["execution_plan"][0]["status"] == "PASS"
    assert payload["execution_plan"][1]["status"] == "FAIL"
    assert payload["sync_report"]["updated_count"] == 2


@patch("modules.precision_test.views.get_precision_test_store")
def test_sync_results_ignores_stale_and_uses_latest_bound_report(mock_store):
    app = Flask(__name__)
    app.register_blueprint(precision_test_bp)

    model = _model()
    model["created_at"] = 1000
    for item in model["execution_plan"]:
        item["executor"] = "ui_automation"
        item["executor_name"] = "UI 自动化"

    @app.route("/ui_automation/api/reports")
    def _ui_reports():
        return {
            "success": True,
            "data": {
                "reports": [{
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][0]["id"],
                    "status": "success",
                    "summary": "旧报告不应回填",
                    "timestamp": 100,
                }, {
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][0]["id"],
                    "status": "failed",
                    "summary": "较早失败",
                    "timestamp": 1100,
                }, {
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][0]["id"],
                    "status": "success",
                    "summary": "最新通过",
                    "timestamp": 1200,
                }]
            },
        }

    app.config["TESTING"] = True
    store = MagicMock()
    store.get.return_value = model
    mock_store.return_value = store

    response = app.test_client().post(f"/precision_test/api/analyses/{model['analysis_id']}/sync_results")
    payload = response.get_json()["data"]

    assert response.status_code == 200
    assert payload["execution_plan"][0]["status"] == "PASS"
    assert payload["execution_plan"][0]["note"] == "最新通过"
    assert payload["sync_report"]["ignored_stale"] == 1
    assert payload["sync_report"]["conflict_count"] == 1


@patch("modules.precision_test.views.get_precision_test_store")
def test_sync_results_does_not_overwrite_manual_conflict(mock_store):
    app = Flask(__name__)
    app.register_blueprint(precision_test_bp)

    model = _model()
    model["execution_plan"][0]["executor"] = "ui_automation"
    model["execution_plan"][0]["executor_name"] = "UI 自动化"
    model["execution_plan"][0]["status"] = "FAIL"
    model["audit_log"] = [{
        "action": "execution_result_update",
        "source": "manual",
        "execution_id": model["execution_plan"][0]["id"],
    }]

    @app.route("/ui_automation/api/reports")
    def _ui_reports():
        return {
            "success": True,
            "data": {
                "reports": [{
                    "precision_analysis_id": model["analysis_id"],
                    "precision_execution_id": model["execution_plan"][0]["id"],
                    "status": "success",
                    "summary": "自动报告通过",
                    "timestamp": model["created_at"] + 1,
                }]
            },
        }

    app.config["TESTING"] = True
    store = MagicMock()
    store.get.return_value = model
    mock_store.return_value = store

    response = app.test_client().post(f"/precision_test/api/analyses/{model['analysis_id']}/sync_results")
    payload = response.get_json()["data"]

    assert response.status_code == 200
    assert payload["execution_plan"][0]["status"] == "FAIL"
    assert payload["sync_report"]["updated_count"] == 0
    assert payload["sync_report"]["manual_conflict_count"] == 1
