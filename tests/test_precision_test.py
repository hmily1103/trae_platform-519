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
@patch("modules.precision_test.views.call_llm", return_value="")
def test_analyze_falls_back_when_model_returns_empty(_mock_llm, mock_store):
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
    assert payload["analysis"]["analysis_source"] == "rules_fallback"
    assert len(payload["analysis"]["test_points"]) >= 10
    assert payload["analysis_id"].startswith("pt_")


@patch("modules.precision_test.views.get_precision_test_store")
@patch(
    "modules.precision_test.views.call_llm",
    return_value='{"change_summary":"收藏幂等优化","risks":[{"category":"点歌","title":"重复收藏","priority":"P0","impact_type":"direct","evidence":["song.py"],"confidence":"high"}]}',
)
def test_analyze_returns_structured_model(_mock_llm, mock_store):
    mock_store.return_value = MagicMock()
    response = _client().post(
        "/precision_test/api/analyze",
        json={"project_type": "vod", "code_diff": "+favorite(song_id)"},
    )
    payload = response.get_json()["data"]
    assert response.status_code == 200
    assert payload["analysis"]["analysis_source"] == "llm"
    assert payload["analysis"]["risks"][0]["priority"] == "P0"
    assert "impact_report" in payload
