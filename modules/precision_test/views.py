import json
import logging
import os
import re
import time
import urllib.request

from flask import render_template, request

from . import precision_test_bp
from .engine import (
    apply_execution_results,
    build_report_markdown,
    extract_json_object,
    normalize_analysis,
)
from .store import get_precision_test_store
from .git_source import build_git_diff, detect_release_baseline
from utils.response import error_response, success_response
from utils.llm_client import call_llm

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
    return {
        "files": files[:50],
        "file_count": len(files),
        "additions": additions,
        "deletions": deletions,
        "has_test_changes": has_test_changes,
        "risk_signals": risks,
    }


def _build_prompt(code_diff, requirement, project_type, summary):
    file_list = "\n".join(f"- {path}" for path in summary["files"]) or "- 未识别到文件名"
    return f"""你是雷石 KTV VOD 产业线的资深测试架构师。
请依据需求说明和 Git Diff，生成结构化精准回归方案。只允许输出一个 JSON 对象，不要 Markdown。

必须关注：
- 点歌：搜索、点歌、收藏、队列、切歌、优先级
- 播放：起播、暂停、继续、卡顿、黑屏、无声、音画同步、解码
- 设备：机顶盒型号、固件、启动、重启、升级、ADB
- 服务端：接口、数据库、Redis、缓存、并发、房台状态
- 跨端：机顶盒、移动端、中控、服务器状态一致性
- 异常：断网、弱网、超时、重试、服务不可用、断电与恢复

规则：
1. 禁止虚构；每个风险必须给出代码或需求证据。
2. 风险等级只能是 P0/P1/P2，影响类型只能是 direct/indirect。
3. 测试点控制在 10～20 条，优先 P0，覆盖正常、异常、边界、并发和数据一致性。
4. 测试点必须关联 risk_ids，并包含可执行步骤和明确预期。
5. 不确定内容降低 confidence，不要把猜测写成事实。

返回结构：
{{
  "change_summary": "一句话变更摘要",
  "risks": [
    {{
      "category": "点歌|播放|设备|服务端|跨端|异常",
      "title": "风险名称",
      "priority": "P0|P1|P2",
      "impact_type": "direct|indirect",
      "affected_users": "影响用户",
      "scope": "影响范围与后果",
      "evidence": ["文件/函数/需求原文"],
      "confidence": "high|medium|low"
    }}
  ],
  "test_points": [
    {{
      "risk_ids": ["R01"],
      "priority": "P0",
      "type": "正常流程|异常流程|边界场景|并发场景|数据一致性|性能场景",
      "title": "测试点",
      "precondition": "前置条件",
      "steps": "执行步骤",
      "expected": "预期结果",
      "mode": "auto|semi_auto|manual"
    }}
  ]
}}

项目类型：{PROJECT_TYPES[project_type]}
变更文件：
{file_list}

需求说明：
{requirement or "未提供需求说明，只能依据代码变更分析"}

Git Diff：
```diff
{code_diff}
```
"""


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
    config_path = os.environ.get("LLM_CONFIG_PATH") or os.path.join(
        os.path.dirname(__file__), "..", "test_case", "llm_config.json"
    )
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
        raw_analysis = {}
        analysis_source = "llm"
        llm_error = ""
        try:
            response_text = call_llm(
                messages=[{"role": "user", "content": _build_prompt(code_diff, requirement, project_type, summary)}],
                timeout=90,
                temperature=0.1,
            )
            raw_analysis = extract_json_object(response_text)
            if not raw_analysis:
                analysis_source = "rules_fallback"
                llm_error = "模型返回内容不是有效 JSON，已使用 VOD 规则降级"
        except Exception as exc:
            analysis_source = "rules_fallback"
            llm_error = str(exc)
            logger.warning("精准回归 LLM 降级: %s", exc)

        model = normalize_analysis(
            raw_analysis,
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
        model["analysis_source"] = analysis_source
        model["llm_error"] = llm_error
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
        model = apply_execution_results(model, results, data.get("gate_mode"))
        model["report_markdown"] = build_report_markdown(model)
        get_precision_test_store().save(model)
        return success_response(model)
    except ValueError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.exception("保存精准回归执行结果失败: %s", exc)
        return error_response("保存执行结果失败", status_code=500)


@precision_test_bp.route("/api/analyses/<analysis_id>/finalize", methods=["POST"])
def finalize_analysis(analysis_id):
    model = get_precision_test_store().get(analysis_id)
    if not model:
        return error_response("分析任务不存在", status_code=404)
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
            f"**覆盖率**：{gate['coverage_rate']}%\n"
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
