import json
import logging
import os
import re
import time
import urllib.request

from flask import current_app, render_template, request

from . import precision_test_bp
from .engine import (
    apply_execution_results,
    build_report_markdown,
    calculate_quality_gate,
    normalize_analysis,
)
from .store import get_precision_test_store
from .git_source import build_git_diff, detect_release_baseline
from utils.response import error_response, success_response

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 100_000
MAX_REQUIREMENT_CHARS = 30_000
PROJECT_TYPES = {
    "vod": "雷石 KTV VOD 全链路",
    "backend": "后端服务",
    "frontend": "前端工程",
    "mobile": "移动端",
    "fullstack": "全栈工程",
}

RISK_RULES = (
    ("数据库变更", "high", re.compile(r"(migration|schema|ALTER\s+TABLE|CREATE\s+TABLE)", re.I)),
    ("鉴权或权限", "high", re.compile(r"(auth|permission|role|token|login|鉴权|权限)", re.I)),
    ("点歌与队列", "high", re.compile(r"(song|music|favorite|collect|queue|order|点歌|歌曲|收藏|队列|切歌)", re.I)),
    ("播放链路", "high", re.compile(r"(player|playback|codec|decode|audio|video|播放|起播|卡顿|黑屏|无声|解码)", re.I)),
    ("并发或异步", "medium", re.compile(r"(async|await|thread|lock|queue|并发|异步|锁)", re.I)),
    ("缓存变更", "medium", re.compile(r"(cache|redis|缓存)", re.I)),
    ("接口契约", "medium", re.compile(r"(route|endpoint|controller|request|response|api)", re.I)),
    ("异常恢复", "medium", re.compile(r"(except|catch|raise|throw|timeout|retry|offline|error|异常|超时|重试|断网)", re.I)),
)

NON_RUNTIME_PATH_RULES = (
    ("ai_tooling", re.compile(r"^\.claude/|^\.codex/|(^|/)CLAUDE\.md$", re.I)),
    ("documentation", re.compile(r"(^|/)(docs?|README|CHANGELOG|CONTRIBUTING)(/|\.|$)|\.(md|rst|adoc|txt)$", re.I)),
    ("test_only", re.compile(r"(^|/)(tests?|__tests__)/|(^|/).*[\._-](test|spec)\.", re.I)),
)

PRODUCT_FILE_PATTERN = re.compile(
    r"\.(java|kt|xml|gradle|properties|aidl|c|cc|cpp|h|hpp|py|js|ts|vue|go|sql|proto|sh)$",
    re.I,
)


def _classify_change_type(files):
    file_list = [str(path or "").strip() for path in files or [] if str(path or "").strip()]
    if not file_list:
        return {
            "change_type": "unknown",
            "runtime_impact": "unknown",
            "reason": "未能从 Diff 中解析到文件路径",
        }

    labels = set()
    for path in file_list:
        matched = False
        for label, pattern in NON_RUNTIME_PATH_RULES:
            if pattern.search(path):
                labels.add(label)
                matched = True
                break
        if matched:
            continue
        if PRODUCT_FILE_PATTERN.search(path):
            labels.add("product_runtime")
        else:
            labels.add("other_config")

    if labels <= {"ai_tooling", "documentation"}:
        return {
            "change_type": "ai_tooling" if "ai_tooling" in labels else "documentation",
            "runtime_impact": "none",
            "reason": "仅改动 AI 协作/文档类文件，不进入 VOD 编译产物和运行时链路",
        }
    if labels == {"test_only"}:
        return {
            "change_type": "test_only",
            "runtime_impact": "none",
            "reason": "仅改动测试代码或测试数据，不直接影响产品运行时",
        }
    if "product_runtime" in labels:
        return {
            "change_type": "product_runtime",
            "runtime_impact": "yes",
            "reason": "包含产品源码、资源、构建或服务端运行时文件",
        }
    return {
        "change_type": "config_or_assets",
        "runtime_impact": "review",
        "reason": "未命中明确产品源码，但可能包含配置或资源变更，需要人工确认是否进入产物",
    }


def _extract_diff_summary(code_diff):
    files = []
    seen = set()
    additions = 0
    deletions = 0
    for line in code_diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            path = re.sub(r"^[ab]/", "", line[4:].strip())
            if path != "/dev/null" and path not in seen:
                seen.add(path)
                files.append(path)
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    risks = [
        {"label": label, "level": level}
        for label, level, pattern in RISK_RULES
        if pattern.search(code_diff)
    ]
    has_test_changes = any(
        re.search(r"(^|/)(tests?|__tests__)/|(^|/).*[\._-](test|spec)\.", path, re.I)
        for path in files
    )
    if files and not has_test_changes:
        risks.append({"label": "未发现测试代码变更", "level": "medium"})
    change_classification = _classify_change_type(files)
    if change_classification["runtime_impact"] == "none":
        risks = [
            risk for risk in risks
            if risk["label"] not in {"未发现测试代码变更"}
        ]
    return {
        "files": files[:50],
        "file_count": len(files),
        "additions": additions,
        "deletions": deletions,
        "has_test_changes": has_test_changes,
        "risk_signals": risks,
        **change_classification,
    }




def _analysis_markdown(model):
    lines = [
        "### 变更摘要",
        model["change"]["summary"],
        "",
        "### 风险模型",
        "",
        "| 编号 | 等级 | 分类 | 影响 | 风险 | 证据 |",
        "|---|---|---|---|---|---|",
    ]
    for risk in model["risks"]:
        lines.append(
            f"| {risk['id']} | {risk['priority']} | {risk['category']} | "
            f"{'直接' if risk['impact_type'] == 'direct' else '间接'} | "
            f"{risk['title']} | {'；'.join(risk['evidence'])} |"
        )
    lines.extend([
        "",
        "### 最小验证集",
        "",
        "| 编号 | 等级 | 测试点 | 执行方式 | 执行器 |",
        "|---|---|---|---|---|",
    ])
    execution_map = {item["test_point_id"]: item for item in model["execution_plan"]}
    for point in model["test_points"]:
        execution = execution_map[point["id"]]
        lines.append(
            f"| {point['id']} | {point['priority']} | {point['title']} | "
            f"{execution['mode']} | {execution['executor_name']} |"
        )
    lines.extend([
        "",
        "### 初始质量门禁",
        f"- 结论：**{model['quality_gate']['decision']}**",
        "- 当前为观察模式，不实际阻断发布。",
    ])
    return "\n".join(lines)


def _load_feishu_webhook():
    env_value = str(os.environ.get("PRECISION_TEST_FEISHU_WEBHOOK") or "").strip()
    if env_value:
        return env_value
    config_path = os.environ.get("LLM_CONFIG_PATH") or os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "llm_config.json"
    ))
    try:
        with open(os.path.abspath(config_path), "r", encoding="utf-8") as file:
            config = json.load(file)
        return str(config.get("precision_test_feishu_webhook") or config.get("feishu_webhook") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


def _post_feishu(webhook_url, title, content):
    if "open.feishu.cn" not in webhook_url and "open.larksuite.com" not in webhook_url:
        raise ValueError("飞书 Webhook 域名不合法")
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red" if "BLOCKED" in content else "green",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content[:6000]}},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": time.strftime("发送时间：%Y-%m-%d %H:%M:%S")}
                ]},
            ],
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        response.read()


def _save_unified_report(model):
    try:
        from shared.unified.report_store import get_unified_report_store
        gate = model["quality_gate"]
        get_unified_report_store().save_report(
            unified_id=model["analysis_id"],
            module="precision_test",
            kind="vod_regression",
            status=gate["decision"].lower(),
            summary={
                "version": model["change"]["version"],
                "decision": gate["decision"],
                "coverage_rate": gate["coverage_rate"],
                "adoption_rate": model.get("metrics", {}).get("adoption_rate", 0),
                "risk_count": len(model["risks"]),
                "test_point_count": len(model["test_points"]),
            },
            details=model,
        )
    except Exception as exc:
        logger.warning("保存精准回归统一报告失败: %s", exc)


@precision_test_bp.route("/")
def index():
    return render_template("precision_test_index.html")


@precision_test_bp.route("/api/status", methods=["GET"])
def module_status():
    return success_response({
        "state": "idle",
        "module": "precision_test",
        "gate_default": "observe",
    })


@precision_test_bp.route("/api/git/preview", methods=["POST"])
def preview_git_change():
    try:
        data = request.get_json(silent=True) or {}
        result = build_git_diff(
            data.get("repository_url"),
            data.get("target_ref"),
            data.get("base_commit"),
            data.get("comparison_mode") or "release",
        )
        return success_response(result)
    except ValueError as exc:
        return error_response(str(exc))
    except RuntimeError as exc:
        logger.warning("只读拉取 Git 变更失败: %s", exc)
        return error_response(f"读取 Git 仓库失败: {exc}", status_code=502)
    except Exception as exc:
        logger.exception("只读拉取 Git 变更异常: %s", exc)
        return error_response("读取 Git 仓库失败", status_code=500)


@precision_test_bp.route("/api/git/detect_baseline", methods=["POST"])
def detect_git_baseline():
    try:
        data = request.get_json(silent=True) or {}
        result = detect_release_baseline(
            data.get("repository_url"),
            data.get("target_ref"),
        )
        return success_response(result)
    except ValueError as exc:
        return error_response(str(exc))
    except RuntimeError as exc:
        logger.warning("自动识别发布基线失败: %s", exc)
        return error_response(str(exc), status_code=422)
    except Exception as exc:
        logger.exception("自动识别发布基线异常: %s", exc)
        return error_response("自动识别发布基线失败", status_code=500)


@precision_test_bp.route("/api/analyze", methods=["POST"])
def analyze_code_diff():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return error_response("请求体必须是 JSON 对象")
        code_diff = str(data.get("code_diff") or "").strip()
        requirement = str(data.get("requirement") or "").strip()
        project_type = str(data.get("project_type") or "vod").strip().lower()
        version = str(data.get("version") or "").strip()
        environment = data.get("environment") if isinstance(data.get("environment"), dict) else {}
        git_source = data.get("git_source") if isinstance(data.get("git_source"), dict) else {}
        if not code_diff:
            return error_response("代码 Diff 不能为空")
        if project_type not in PROJECT_TYPES:
            return error_response("不支持的项目类型")
        if len(code_diff) > MAX_DIFF_CHARS:
            return error_response(f"代码 Diff 不能超过 {MAX_DIFF_CHARS:,} 个字符")
        if len(requirement) > MAX_REQUIREMENT_CHARS:
            return error_response(f"需求说明不能超过 {MAX_REQUIREMENT_CHARS:,} 个字符")

        started_at = time.time()
        summary = _extract_diff_summary(code_diff)

        # ---- CodeGraph 代码影响透视层（前置计算，作为 Agent 证据）----
        # 把「代码改动」转成「影响范围」：受影响测试文件 + 改动符号影响链。
        # 默认关闭（codegraph_config.json enabled=false 或无被测仓库）；
        # 工具不可用/异常时静默跳过，不影响主流程（架构不变）。
        codegraph_impact = {
            "available": False, "enabled": False,
            "changed_files": [], "affected_tests": [], "impact_symbols": [], "error": "",
        }
        try:
            from .codegraph_client import analyze_diff_impact, load_config
            _cg_cfg = load_config()
            if _cg_cfg.get("enabled") and _cg_cfg.get("repo_path"):
                codegraph_impact = analyze_diff_impact(
                    _cg_cfg["repo_path"], code_diff, _cg_cfg.get("test_glob"),
                )
                logger.info("CodeGraph 影响分析: 受影响测试 %d, 影响符号 %d",
                            len(codegraph_impact.get("affected_tests", [])),
                            len(codegraph_impact.get("impact_symbols", [])))
        except Exception as _cg_exc:
            logger.warning("CodeGraph 影响分析跳过: %s", _cg_exc)
            codegraph_impact["error"] = str(_cg_exc)

        # ---- 多步风险分析 Agent（替代单次 LLM 黑盒调用）----
        # 粗分 -> 细化 -> 校验 三步可追溯编排；LLM 不可用时自动降级到规则风险。
        # CodeGraph 影响结果作为硬证据喂入，闭合「代码影响 -> 风险推导」链路。
        from .agent import run_risk_agent
        agent_result = run_risk_agent(code_diff, requirement, project_type, summary, codegraph_impact)

        model = normalize_analysis(
            agent_result,
            requirement=requirement,
            code_diff=code_diff,
            project_type=project_type,
            version=version,
            summary=summary,
            git_source={
                "repository_url": str(git_source.get("repository_url") or "").strip(),
                "target_ref": str(git_source.get("target_ref") or "").strip(),
                "target_sha": str(git_source.get("target_sha") or "").strip(),
                "base_commit": str(git_source.get("base_commit") or "").strip(),
                "base_sha": str(git_source.get("base_sha") or "").strip(),
            },
            environment={
                "device_id": str(environment.get("device_id") or "").strip(),
                "stb_model": str(environment.get("stb_model") or "").strip(),
                "firmware": str(environment.get("firmware") or "").strip(),
                "vod_version": str(environment.get("vod_version") or version).strip(),
                "server": str(environment.get("server") or "").strip(),
                "network": str(environment.get("network") or "实验室正常网络").strip(),
            },
        )
        model["analysis_source"] = agent_result.get("analysis_source") or "agent"
        model["llm_error"] = agent_result.get("agent_error", "")
        model["agent_trace"] = agent_result.get("agent_trace", [])
        model["codegraph_impact"] = codegraph_impact
        if codegraph_impact.get("affected_tests"):
            model["codegraph_affected_tests"] = codegraph_impact["affected_tests"]
        # 变更摘要：Agent 未单列生成，用需求或文件数兜底，避免空摘要
        model["change"]["summary"] = (
            requirement.strip()
            or f"本次改动涉及 {summary.get('file_count', 0)} 个文件，已生成精准回归方案"
        )

        # Mastra 多步分析已在 agent.run_risk_agent 内可选委托（配置 MASTRA_ANALYSIS_URL 时）；
        # 未配置或不可达时 agent 静默回退本地三步，mastra_analysis 为 None。
        model["mastra_analysis"] = agent_result.get("mastra_analysis")

        model["metrics"]["analysis_duration_ms"] = int((time.time() - started_at) * 1000)
        model["impact_report"] = _analysis_markdown(model)
        get_precision_test_store().save(model)
        return success_response({
            "analysis_id": model["analysis_id"],
            "analysis": model,
            "impact_report": model["impact_report"],
            "diff_summary": summary,
        })
    except Exception as exc:
        logger.exception("分析代码变更失败: %s", exc)
        return error_response("精准回归分析失败，请稍后重试", status_code=500)


@precision_test_bp.route("/api/analyses", methods=["GET"])
def list_analyses():
    return success_response({"items": get_precision_test_store().list(request.args.get("limit", 50))})


@precision_test_bp.route("/api/analyses/<analysis_id>", methods=["GET"])
def get_analysis(analysis_id):
    model = get_precision_test_store().get(analysis_id)
    if not model:
        return error_response("分析任务不存在", status_code=404)
    return success_response(model)


@precision_test_bp.route("/api/analyses/<analysis_id>/results", methods=["POST"])
def update_results(analysis_id):
    try:
        model = get_precision_test_store().get(analysis_id)
        if not model:
            return error_response("分析任务不存在", status_code=404)
        data = request.get_json(silent=True) or {}
        results = data.get("results")
        if not isinstance(results, list):
            return error_response("results 必须是数组")
        actor = str(data.get("actor") or request.headers.get("X-User") or "manual_user").strip()[:80]
        for item in results:
            if isinstance(item, dict):
                item.setdefault("source", "manual")
                item.setdefault("actor", actor)
        model = apply_execution_results(model, results, data.get("gate_mode"))
        model["report_markdown"] = build_report_markdown(model)
        get_precision_test_store().save(model)
        return success_response(model)
    except ValueError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.exception("保存精准回归执行结果失败: %s", exc)
        return error_response("保存执行结果失败", status_code=500)


@precision_test_bp.route("/api/analyses/<analysis_id>/confirmations", methods=["POST"])
def update_confirmation(analysis_id):
    try:
        model = get_precision_test_store().get(analysis_id)
        if not model:
            return error_response("分析任务不存在", status_code=404)
        data = request.get_json(silent=True) or {}
        confirmation_id = str(data.get("confirmation_id") or "").strip()
        status = str(data.get("status") or "").strip().upper()
        if status not in {"CONFIRMED", "REJECTED", "OPEN"}:
            return error_response("确认状态必须是 OPEN / CONFIRMED / REJECTED")
        actor = str(data.get("actor") or request.headers.get("X-User") or "manual_user").strip()[:80]
        response = str(data.get("response") or "").strip()
        evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
        target = None
        for item in model.get("confirmations") or []:
            if item.get("id") == confirmation_id:
                target = item
                break
        if not target:
            return error_response("确认项不存在", status_code=404)
        before = dict(target)
        target["status"] = status
        target["response"] = response
        target["confirmed_by"] = actor if status != "OPEN" else ""
        target["confirmed_at"] = int(time.time()) if status != "OPEN" else 0
        target["evidence"] = [str(value).strip() for value in evidence if str(value).strip()][:10]
        model.setdefault("audit_log", []).append({
            "ts": int(time.time()),
            "action": "confirmation_update",
            "source": "manual",
            "actor": actor,
            "confirmation_id": confirmation_id,
            "before": before,
            "after": dict(target),
        })
        model["updated_at"] = int(time.time())
        model["quality_gate"] = calculate_quality_gate(
            model.get("execution_plan", []),
            model.get("risks", []),
            model.get("gate_mode", "observe"),
            model.get("confirmations"),
        )
        model["report_markdown"] = build_report_markdown(model) if model.get("report_markdown") else ""
        get_precision_test_store().save(model)
        return success_response(model)
    except Exception as exc:
        logger.exception("更新精准回归确认项失败: %s", exc)
        return error_response("更新确认项失败", status_code=500)


@precision_test_bp.route("/api/analyses/<analysis_id>/finalize", methods=["POST"])
def finalize_analysis(analysis_id):
    model = get_precision_test_store().get(analysis_id)
    if not model:
        return error_response("分析任务不存在", status_code=404)
    open_confirmations = [
        item for item in model.get("confirmations") or []
        if item.get("status") == "OPEN" and item.get("priority") in {"P0", "P1"}
    ]
    if open_confirmations:
        return error_response("存在未关闭的高风险研发确认项，暂不能生成正式报告", status_code=409)
    model["report_markdown"] = build_report_markdown(model)
    model["finalized_at"] = int(time.time())
    get_precision_test_store().save(model)
    _save_unified_report(model)
    return success_response({
        "analysis_id": analysis_id,
        "quality_gate": model["quality_gate"],
        "report_markdown": model["report_markdown"],
        "report_url": f"/unified/report/{analysis_id}",
    })


def _ts(value):
    """解析时间戳为 epoch 秒；失败返回 0。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else 0
    try:
        import datetime
        s = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.datetime.strptime(s[:26], fmt).timestamp()
            except ValueError:
                continue
        return 0
    except Exception:
        return 0


def _parse_executor_recent(reports, executor_id):
    """从执行器报告列表解析最近运行状态：pass/fail/running/none/unknown。

    仅做保守判定，避免误判：
    - monkey：status=finished 且无 crash/anr → pass；failed 或 crash/anr>0 → fail；running → running
    - ui_automation：status=success 或 passed>0 且无 failed → pass；failed>0 → fail；否则 unknown
    - combined_test 等只返回文件名无结构化结果 → 调用方标记 unavailable
    """
    if not reports:
        return "none"
    try:
        latest = max(reports, key=lambda r: _ts(r.get("created_at") or r.get("timestamp") or r.get("mtime") or 0))
    except Exception:
        latest = reports[0]
    if executor_id == "monkey":
        st = str(latest.get("status") or "").lower()
        crash = int(latest.get("crash_count", 0) or 0)
        anr = int(latest.get("anr_count", 0) or 0)
        if st == "running":
            return "running"
        if st in ("finished", "complete", "completed") and crash == 0 and anr == 0:
            return "pass"
        if st in ("failed", "error") or crash > 0 or anr > 0:
            return "fail"
        return "unknown"
    if executor_id == "song_order":
        if bool(latest.get("success")):
            return "pass"
        if latest.get("success") is False:
            return "fail"
        return "unknown"
    if executor_id == "ui_automation":
        st = str(latest.get("status") or "").lower()
        passed = int(latest.get("passed") or latest.get("passed_cases") or 0)
        failed = int(latest.get("failed") or latest.get("failed_cases") or 0)
        if st in ("success", "passed") or (passed and not failed):
            return "pass"
        if st in ("failed", "error") or failed > 0:
            return "fail"
        return "unknown"
    return "unknown"


def _deep_get(mapping, *paths):
    """从嵌套 dict 中按多个候选路径取第一个非空值。"""
    for path in paths:
        current = mapping
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return current
    return ""


def _normalize_executor_status(report, executor_id):
    """把不同执行器报告归一成精准回归门禁状态。"""
    raw = str(_deep_get(report, "precision_status", "result", "status", "meta.status") or "").strip().lower()
    text = " ".join(
        str(value or "")
        for value in (
            raw,
            _deep_get(report, "status_reason", "summary", "message", "error", "meta.summary"),
        )
    ).lower()
    if any(token in text for token in ("adb offline", "device offline", "device not connected", "设备未连接", "设备离线", "adb异常", "adb 异常")):
        return "ENV_ERROR"
    if raw in {"pass", "passed", "success", "successful", "ok", "finished_success"}:
        return "PASS"
    if raw in {"fail", "failed", "failure", "error", "crash", "anr"}:
        return "FAIL"
    if raw in {"running", "pending", "queued", "in_progress"}:
        return "PENDING"
    if raw in {"blocked", "skip", "skipped", "env_error"}:
        return "SKIPPED" if raw in {"skip", "skipped"} else raw.upper()

    if executor_id == "monkey":
        crash = int(report.get("crash_count", 0) or 0)
        anr = int(report.get("anr_count", 0) or 0)
        if crash or anr:
            return "FAIL"
        if str(report.get("status") or "").upper() == "SUCCESS":
            return "PASS"
    if executor_id == "ui_automation":
        failed = int(report.get("failed") or report.get("failed_cases") or 0)
        passed = int(report.get("passed") or report.get("passed_cases") or 0)
        if failed:
            return "FAIL"
        if passed:
            return "PASS"
    if executor_id == "song_order":
        if bool(report.get("success")):
            return "PASS"
        if report.get("success") is False:
            return "FAIL"
    if executor_id == "combined_test":
        if bool(report.get("success")):
            return "PASS"
        if report.get("success") is False:
            return "FAIL"
    if executor_id == "player_stress":
        meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
        decision = str(_deep_get(meta, "decision", "verdict", "result", "status") or "").lower()
        if decision in {"pass", "passed", "success", "ok"}:
            return "PASS"
        if decision in {"fail", "failed", "blocked", "error"}:
            return "FAIL"
    if executor_id == "log_monitor":
        severity = str(report.get("severity") or report.get("level") or "").lower()
        if severity in {"critical", "error", "high", "p0", "p1"}:
            return "FAIL"
    return ""


def _report_binding(report):
    """提取执行报告中的精准回归绑定字段。没有绑定就不自动同步。"""
    analysis_id = str(_deep_get(
        report,
        "analysis_id",
        "precision_analysis_id",
        "meta.analysis_id",
        "meta.precision_analysis_id",
        "context.analysis_id",
        "context.precision_analysis_id",
    ) or "").strip()
    execution_id = str(_deep_get(
        report,
        "execution_id",
        "precision_execution_id",
        "meta.execution_id",
        "meta.precision_execution_id",
        "context.execution_id",
        "context.precision_execution_id",
    ) or "").strip()
    test_point_id = str(_deep_get(
        report,
        "test_point_id",
        "precision_test_point_id",
        "meta.test_point_id",
        "meta.precision_test_point_id",
        "context.test_point_id",
        "context.precision_test_point_id",
    ) or "").strip()
    return analysis_id, execution_id, test_point_id


def _report_evidence(report, executor_id):
    evidence = []
    for path in ("report_url", "url", "html_url", "download_url", "image_url", "screenshot_url"):
        value = _deep_get(report, path, f"meta.{path}")
        if value:
            evidence.append(str(value))
    for key in ("report_id", "job_id", "runtime_id", "prefix", "pipeline_id", "musicno", "musicname"):
        value = report.get(key)
        if value:
            evidence.append(f"{executor_id}:{key}={value}")
    return evidence[:10]


def _report_summary(report):
    return str(_deep_get(
        report,
        "summary",
        "message",
        "status_reason",
        "conclusion",
        "meta.summary",
        "meta.conclusion",
    ) or "执行器已回传结构化结果").strip()


def _report_time(report):
    return _ts(_deep_get(
        report,
        "created_at",
        "timestamp",
        "mtime",
        "ts",
        "end_ts",
        "meta.created_at",
        "meta.timestamp",
    ) or 0)


def _last_execution_audit(model, execution_id):
    for item in reversed(model.get("audit_log") or []):
        if item.get("execution_id") == execution_id:
            return item
    return {}


# 各执行器「最近运行」GET 端点（同进程 test_client 调用，避免网络依赖）。
_EXECUTOR_REPORT_ENDPOINTS = {
    "song_order": "/song_order/api/history",
    "monkey": "/monkey/api/reports",
    "ui_automation": "/ui_automation/api/reports",
    "player_stress": "/player_stress/api/reports",
    "log_monitor": "/log_monitor/api/alerts",
    "combined_test": "/combined_test/api/reports",
}


def _load_executor_reports(client, executor_id):
    endpoint = _EXECUTOR_REPORT_ENDPOINTS.get(executor_id)
    if not endpoint:
        return []
    resp = client.get(endpoint)
    data = resp.get_json(silent=True) or {}
    payload = data.get("data") or {}
    if executor_id == "log_monitor":
        return payload.get("alerts") or []
    if executor_id == "song_order":
        return payload.get("entries") or []
    return payload.get("reports") or []


@precision_test_bp.route("/api/analyses/<analysis_id>/collect", methods=["POST"])
def collect_executor_status(analysis_id):
    """采集各执行器最近运行状态快照，辅助人工回写（不直接改 execution 状态，防误判）。

    设计取舍：执行器报告是「一次运行」，无法可靠关联到精准回归里的具体测试点，
    强行自动改 PASS/FAIL 会误判，违背「Agent 不自动改/人工在环」红线。
    因此本接口只采集快照（pass/fail/running/none/unavailable），由前端对明确
    通过的执行器组提供「人工一键采纳」，最终判定权留给测试同学。
    """
    model = get_precision_test_store().get(analysis_id)
    if not model:
        return error_response("分析任务不存在", status_code=404)
    environment = model.get("change", {}).get("environment", {}) or {}
    device_id = str(environment.get("device_id") or "").strip()
    since_ts = model.get("created_at", 0)

    executors = sorted({item["executor"] for item in model.get("execution_plan", [])})
    client = current_app.test_client()
    per_executor = {}
    for exec_id in executors:
        if exec_id == "manual":
            per_executor[exec_id] = {"status": "manual", "note": "人工验证，无自动执行器"}
            continue
        endpoint = _EXECUTOR_REPORT_ENDPOINTS.get(exec_id)
        if not endpoint:
            per_executor[exec_id] = {"status": "unavailable", "note": "该执行器无结构化运行报告，需人工回写"}
            continue
        try:
            reports = _load_executor_reports(client, exec_id)
            recent = _parse_executor_recent(reports, exec_id)
            per_executor[exec_id] = {
                "status": recent,
                "device_id": device_id,
                "report_count": len(reports),
                "note": "已采集最近运行快照" if recent in ("pass", "fail", "running") else "无最近运行记录",
            }
        except Exception as exc:
            logger.warning("采集执行器 %s 状态失败: %s", exec_id, exc)
            per_executor[exec_id] = {"status": "unavailable", "note": f"采集失败：{exc}"}

    model["collect_report"] = {
        "collected_at": int(time.time()),
        "device_id": device_id,
        "since_ts": since_ts,
        "per_executor": per_executor,
    }
    get_precision_test_store().save(model)
    return success_response(model)


@precision_test_bp.route("/api/analyses/<analysis_id>/sync_results", methods=["POST"])
def sync_executor_results(analysis_id):
    """按 analysis_id + execution_id/test_point_id 自动同步执行器结果。

    只同步明确绑定到本次精准回归的结构化报告；未绑定的“最近一次运行”仍只作为采集快照，
    避免把别的任务结果误算进上线门禁。
    """
    model = get_precision_test_store().get(analysis_id)
    if not model:
        return error_response("分析任务不存在", status_code=404)
    client = current_app.test_client()
    execution_by_id = {item["id"]: item for item in model.get("execution_plan", [])}
    execution_by_point = {item["test_point_id"]: item for item in model.get("execution_plan", [])}
    executors = sorted({item["executor"] for item in model.get("execution_plan", []) if item.get("executor") != "manual"})
    candidates_by_execution = {}
    scanned = {}
    ignored_unbound = 0
    ignored_stale = 0
    conflict_count = 0
    manual_conflict_count = 0
    manual_conflicts = []
    min_report_ts = max(0, int(model.get("created_at") or 0) - 300)
    for exec_id in executors:
        try:
            reports = _load_executor_reports(client, exec_id)
            scanned[exec_id] = len(reports)
        except Exception as exc:
            logger.warning("同步执行器 %s 结果失败: %s", exec_id, exc)
            scanned[exec_id] = f"error: {exc}"
            continue
        for report in reports:
            if not isinstance(report, dict):
                continue
            bound_analysis_id, execution_id, test_point_id = _report_binding(report)
            if bound_analysis_id != analysis_id:
                if bound_analysis_id:
                    continue
                ignored_unbound += 1
                continue
            report_ts = _report_time(report)
            if report_ts and report_ts < min_report_ts:
                ignored_stale += 1
                continue
            execution = execution_by_id.get(execution_id) or execution_by_point.get(test_point_id)
            if not execution or execution.get("executor") != exec_id:
                continue
            status = _normalize_executor_status(report, exec_id)
            if not status:
                continue
            last_audit = _last_execution_audit(model, execution["id"])
            last_source = str(last_audit.get("source") or "")
            current_status = str(execution.get("status") or "PENDING").upper()
            if last_source == "manual" and current_status != "PENDING" and current_status != status:
                manual_conflict_count += 1
                manual_conflicts.append({
                    "execution_id": execution["id"],
                    "manual_status": current_status,
                    "sync_status": status,
                    "executor": exec_id,
                    "summary": _report_summary(report),
                })
                continue
            candidate = {
                "execution_id": execution["id"],
                "status": status,
                "note": _report_summary(report),
                "evidence": _report_evidence(report, exec_id),
                "bug_id": str(report.get("bug_id") or "").strip(),
                "workaround": bool(report.get("workaround")),
                "source": f"sync:{exec_id}",
                "actor": "executor_report",
                "_report_ts": report_ts,
            }
            existing = candidates_by_execution.get(execution["id"])
            if existing:
                conflict_count += 1
                if (candidate["_report_ts"] or 0) <= (existing.get("_report_ts") or 0):
                    continue
            candidates_by_execution[execution["id"]] = candidate
    updates = list(candidates_by_execution.values())
    for item in updates:
        item.pop("_report_ts", None)
    if updates:
        model = apply_execution_results(model, updates, (request.get_json(silent=True) or {}).get("gate_mode"))
        model["report_markdown"] = build_report_markdown(model)
    model["sync_report"] = {
        "synced_at": int(time.time()),
        "updated_count": len(updates),
        "scanned": scanned,
        "ignored_unbound": ignored_unbound,
        "ignored_stale": ignored_stale,
        "conflict_count": conflict_count,
        "manual_conflict_count": manual_conflict_count,
        "manual_conflicts": manual_conflicts[:20],
    }
    get_precision_test_store().save(model)
    return success_response(model)


@precision_test_bp.route("/api/analyses/<analysis_id>/push_feishu", methods=["POST"])
def push_feishu(analysis_id):
    try:
        model = get_precision_test_store().get(analysis_id)
        if not model:
            return error_response("分析任务不存在", status_code=404)
        webhook_url = _load_feishu_webhook()
        if not webhook_url:
            return error_response(
                "未配置飞书 Webhook，请设置 PRECISION_TEST_FEISHU_WEBHOOK "
                "或在 llm_config.json 中配置 precision_test_feishu_webhook"
            )
        if not model.get("report_markdown"):
            model["report_markdown"] = build_report_markdown(model)
            get_precision_test_store().save(model)
        gate = model["quality_gate"]
        content = (
            f"**版本**：{model['change']['version']}\n"
            f"**质量结论**：{gate['decision']}\n"
            f"**执行覆盖率**：{gate['coverage_rate']}%\n"
            f"**采纳率**：{model.get('metrics', {}).get('adoption_rate', 0)}%（LLM {model.get('metrics', {}).get('llm_adoption_rate', 0)}% / 模板 {model.get('metrics', {}).get('template_adoption_rate', 0)}%）\n"
            f"**P0通过**：{gate['p0_passed']}/{gate['p0_total']}\n"
            f"**门禁模式**：{'强制' if gate['enforced'] else '观察'}\n"
            f"**结论依据**：{'；'.join(gate['reasons'])}\n"
            f"**平台报告**：/unified/report/{analysis_id}"
        )
        _post_feishu(webhook_url, f"【VOD测试结论】{model['change']['version']}", content)
        return success_response({"analysis_id": analysis_id}, message="质量结论已发送到飞书")
    except ValueError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.exception("精准回归飞书推送失败: %s", exc)
        return error_response("飞书推送失败，请检查 Webhook 与网络", status_code=502)
