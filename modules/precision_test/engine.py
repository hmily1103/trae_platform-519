import json
import re
import time
import uuid
from collections import Counter


ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
ALLOWED_STATUSES = {"PENDING", "PASS", "FAIL", "BLOCKED", "SKIPPED", "ENV_ERROR"}
ALLOWED_MODES = {"auto", "semi_auto", "manual"}

EXECUTORS = {
    "song_order": {"name": "点歌与搜索", "url": "/song_order/", "mode": "auto"},
    "api_stress": {"name": "API 压测", "url": "/api_stress/", "mode": "auto"},
    "player_stress": {"name": "播放器压测", "url": "/player_stress/", "mode": "auto"},
    "monkey": {"name": "Monkey 测试", "url": "/monkey/", "mode": "auto"},
    "performance_monitor": {"name": "性能监控", "url": "/performance_monitor/", "mode": "auto"},
    "log_monitor": {"name": "日志监控", "url": "/log_monitor/", "mode": "auto"},
    "reboot": {"name": "中控重启", "url": "/reboot/", "mode": "auto"},
    "combined_test": {"name": "组合测试", "url": "/combined_test/", "mode": "auto"},
    "ui_automation": {"name": "UI 自动化", "url": "/ui_automation/", "mode": "auto"},
    "server_stress": {"name": "ARM 服务器压测", "url": "/server_stress/", "mode": "auto"},
    "manual": {"name": "人工验证", "url": "", "mode": "manual"},
}

CATEGORY_EXECUTORS = {
    "点歌": ["song_order", "api_stress"],
    "播放": ["player_stress", "performance_monitor", "log_monitor"],
    "设备": ["reboot", "monkey", "log_monitor"],
    "服务端": ["api_stress", "server_stress"],
    "跨端": ["combined_test", "ui_automation", "log_monitor"],
    "异常": ["combined_test", "log_monitor"],
}

VOD_SIGNALS = (
    ("点歌", "P0", r"song|music|favorite|collect|queue|order|search|点歌|歌曲|收藏|歌单|队列|切歌"),
    ("播放", "P0", r"player|playback|codec|decode|audio|video|pause|seek|播放|起播|卡顿|黑屏|无声|音画|解码"),
    ("设备", "P1", r"adb|firmware|upgrade|boot|reboot|device|stb|机顶盒|固件|升级|启动|重启|配置"),
    ("服务端", "P1", r"api|controller|service|database|mysql|redis|cache|server|接口|数据库|缓存|服务器|房台"),
    ("跨端", "P1", r"sync|state|mobile|tv|central|room|同步|状态一致|移动端|中控|包厢"),
    ("异常", "P1", r"timeout|retry|exception|error|network|offline|weak|超时|重试|异常|断网|弱网|断电|降级"),
)


def extract_json_object(text):
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("{")
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(raw)):
        char = raw[index]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(raw[start:index + 1])
                    return value if isinstance(value, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def detect_vod_categories(text):
    found = []
    for category, priority, pattern in VOD_SIGNALS:
        if re.search(pattern, text or "", re.I):
            found.append({"category": category, "priority": priority})
    return found or [{"category": "服务端", "priority": "P2"}]


def _normalize_priority(value, default="P1"):
    priority = str(value or default).upper()
    return priority if priority in ALLOWED_PRIORITIES else default


def _unique_strings(values, limit=8):
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result[:limit]


def _default_risks(requirement, code_diff, summary):
    source = f"{requirement}\n{code_diff}"
    risks = []
    evidence = (summary.get("files") or ["需求说明"])[0]
    templates = {
        "点歌": ("点歌状态或队列不一致", "点歌成功但机顶盒未进入正确播放队列"),
        "播放": ("播放主链路异常", "可能出现不起播、卡顿、黑屏、无声或切歌失败"),
        "设备": ("机顶盒兼容与恢复异常", "不同型号、固件或重启后状态可能不一致"),
        "服务端": ("接口与数据一致性异常", "接口成功状态、缓存和数据库结果可能不一致"),
        "跨端": ("跨端状态不同步", "机顶盒、移动端、中控和服务器可能展示不同状态"),
        "异常": ("异常恢复路径不完整", "断网、超时、重试或断电后可能留下脏状态"),
    }
    for index, signal in enumerate(detect_vod_categories(source), 1):
        title, description = templates[signal["category"]]
        risks.append({
            "id": f"R{index:02d}",
            "category": signal["category"],
            "title": title,
            "priority": signal["priority"],
            "impact_type": "direct" if index == 1 else "indirect",
            "affected_users": "使用相关功能的包厢与用户",
            "scope": description,
            "evidence": [evidence],
            "confidence": "medium",
        })
    return risks


def _normalize_risks(raw_risks, requirement, code_diff, summary):
    items = raw_risks if isinstance(raw_risks, list) else []
    normalized = []
    valid_categories = set(CATEGORY_EXECUTORS)
    for index, item in enumerate(items[:12], 1):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        if category not in valid_categories:
            category = detect_vod_categories(
                f"{item.get('title', '')} {item.get('scope', '')}"
            )[0]["category"]
        normalized.append({
            "id": f"R{index:02d}",
            "category": category,
            "title": str(item.get("title") or f"{category}风险").strip(),
            "priority": _normalize_priority(item.get("priority")),
            "impact_type": "indirect" if str(item.get("impact_type")).lower() == "indirect" else "direct",
            "affected_users": str(item.get("affected_users") or "相关包厢与用户").strip(),
            "scope": str(item.get("scope") or item.get("description") or "待进一步确认影响范围").strip(),
            "evidence": _unique_strings(item.get("evidence") or summary.get("files") or ["需求说明"]),
            "confidence": str(item.get("confidence") or "medium").lower()
            if str(item.get("confidence") or "").lower() in {"high", "medium", "low"} else "medium",
        })
    return normalized or _default_risks(requirement, code_diff, summary)


def _test_templates(risk):
    category = risk["category"]
    title = risk["title"]
    common = [
        ("正常流程", f"验证{title}相关主流程", "按需求完成主流程操作", "业务结果与服务端、机顶盒状态一致"),
        ("异常流程", f"验证{title}异常恢复", "制造超时、失败或依赖不可用", "有明确提示且状态可恢复，不产生脏数据"),
    ]
    extras = {
        "点歌": [
            ("边界场景", "重复点歌与连续点击", "连续触发相同歌曲点歌10次", "请求幂等，队列无异常重复"),
            ("数据一致性", "点歌队列跨端一致", "比较服务端、机顶盒与中控队列", "歌曲顺序和状态完全一致"),
        ],
        "播放": [
            ("性能场景", "起播与切歌体验", "连续起播、暂停、继续、切歌", "无黑屏、无声和明显卡顿"),
            ("长稳场景", "长时间播放稳定性", "持续播放并采集性能和日志", "无Crash/ANR，指标不越过基线"),
        ],
        "设备": [
            ("兼容场景", "设备矩阵兼容", "在目标型号和固件执行主流程", "行为一致且无设备专属异常"),
            ("恢复场景", "重启后状态恢复", "业务进行中重启并重新进入", "恢复策略符合需求且无残留状态"),
        ],
        "服务端": [
            ("接口场景", "接口契约与错误码", "验证正常、非法和边界参数", "状态码、错误码和数据契约正确"),
            ("并发场景", "并发与幂等验证", "并发提交相同业务请求", "无重复写入、状态错乱或缓存脏读"),
        ],
        "跨端": [
            ("一致性", "跨端状态同步", "在不同入口操作并观察各端", "状态在约定时间内一致"),
            ("冲突场景", "多端并发操作", "机顶盒和移动端同时操作", "按唯一优先级裁决且结果可观测"),
        ],
        "异常": [
            ("弱网场景", "断网与弱网恢复", "操作中断网后恢复网络", "提示、重试和最终状态符合约定"),
            ("容灾场景", "服务不可用与降级", "模拟依赖超时或不可用", "主流程按约定阻断或降级"),
        ],
    }
    return common + extras.get(category, [])


def _build_test_points(raw_points, risks):
    points = []
    items = raw_points if isinstance(raw_points, list) else []
    risk_ids = {risk["id"] for risk in risks}
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        linked = [rid for rid in _unique_strings(item.get("risk_ids"), 4) if rid in risk_ids]
        if not linked:
            continue
        risk = next(r for r in risks if r["id"] == linked[0])
        points.append({
            "id": "",
            "risk_ids": linked,
            "priority": _normalize_priority(item.get("priority"), risk["priority"]),
            "type": str(item.get("type") or "功能场景").strip(),
            "title": str(item.get("title") or "验证风险").strip(),
            "precondition": str(item.get("precondition") or "测试环境与数据准备完成").strip(),
            "steps": str(item.get("steps") or "执行对应业务操作").strip(),
            "expected": str(item.get("expected") or "结果符合需求且状态一致").strip(),
            "mode": str(item.get("mode") or "").lower(),
        })

    if len(points) < 10:
        points = []
        for risk in risks:
            for point_type, title, steps, expected in _test_templates(risk):
                points.append({
                    "id": "",
                    "risk_ids": [risk["id"]],
                    "priority": risk["priority"],
                    "type": point_type,
                    "title": title,
                    "precondition": "目标版本、机顶盒和服务端环境可用",
                    "steps": steps,
                    "expected": expected,
                    "mode": "",
                })
                if len(points) >= 20:
                    break
            if len(points) >= 20:
                break
        fallback_scenarios = [
            ("冒烟场景", "核心链路冒烟验证", "完成搜索、点歌、起播、切歌主链路", "主链路可用且各端状态一致"),
            ("日志场景", "关键异常日志检查", "执行主流程并检查设备与服务端日志", "无新增Crash、ANR或高频错误"),
            ("性能场景", "核心链路性能基线", "执行主流程并采集CPU、内存、FPS", "指标不劣化且无明显卡顿"),
            ("恢复场景", "中断后恢复验证", "主流程中断网络或重启后重新进入", "状态可预测且数据不丢失"),
            ("兼容场景", "目标设备矩阵验证", "选择代表性型号与固件执行主流程", "各设备行为符合统一业务规则"),
        ]
        fallback_index = 0
        while len(points) < 10:
            risk = risks[fallback_index % len(risks)]
            point_type, title, steps, expected = fallback_scenarios[fallback_index % len(fallback_scenarios)]
            points.append({
                "id": "",
                "risk_ids": [risk["id"]],
                "priority": risk["priority"],
                "type": point_type,
                "title": title,
                "precondition": "目标版本、代表性机顶盒和服务端环境可用",
                "steps": steps,
                "expected": expected,
                "mode": "",
            })
            fallback_index += 1

    points.sort(key=lambda p: {"P0": 0, "P1": 1, "P2": 2}[p["priority"]])
    for index, point in enumerate(points[:20], 1):
        point["id"] = f"T{index:02d}"
    return points[:20]


def _executor_for_point(point, risk):
    text = f"{point['title']} {point['type']} {point['steps']}"
    candidates = list(CATEGORY_EXECUTORS.get(risk["category"], ["manual"]))
    if re.search(r"画面|黑屏|无声|音画|体验", text):
        return "player_stress", "semi_auto"
    if re.search(r"日志|错误码|异常", text):
        return "log_monitor", "auto"
    if re.search(r"CPU|内存|FPS|性能|卡顿", text, re.I):
        return "performance_monitor", "auto"
    if re.search(r"并发|接口|错误码|参数|幂等", text):
        return "api_stress", "auto"
    if re.search(r"重启|冷启动|断电|恢复", text):
        return "combined_test", "auto"
    executor_id = candidates[0] if candidates else "manual"
    mode = point.get("mode")
    if mode not in ALLOWED_MODES:
        mode = EXECUTORS[executor_id]["mode"]
    return executor_id, mode


def build_execution_plan(test_points, risks):
    risk_map = {risk["id"]: risk for risk in risks}
    plan = []
    for point in test_points:
        risk = risk_map[point["risk_ids"][0]]
        executor_id, mode = _executor_for_point(point, risk)
        executor = EXECUTORS[executor_id]
        plan.append({
            "id": f"E{len(plan) + 1:02d}",
            "test_point_id": point["id"],
            "risk_ids": point["risk_ids"],
            "priority": point["priority"],
            "mode": mode,
            "executor": executor_id,
            "executor_name": executor["name"],
            "executor_url": executor["url"],
            "status": "PENDING",
            "bug_id": "",
            "note": "",
            "evidence": [],
            "workaround": False,
        })
    return plan


def normalize_analysis(
    raw,
    *,
    requirement,
    code_diff,
    project_type,
    version,
    summary,
    environment=None,
    git_source=None,
):
    raw = raw if isinstance(raw, dict) else {}
    risks = _normalize_risks(raw.get("risks"), requirement, code_diff, summary)
    test_points = _build_test_points(raw.get("test_points"), risks)
    impacts = []
    for risk in risks:
        impacts.append({
            "name": risk["title"],
            "type": risk["impact_type"],
            "category": risk["category"],
            "evidence": risk["evidence"],
            "confidence": risk["confidence"],
        })
    analysis_id = f"pt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    model = {
        "analysis_id": analysis_id,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "change": {
            "version": version or "未填写",
            "project_type": project_type,
            "requirement": requirement,
            "summary": str(raw.get("change_summary") or raw.get("summary") or "根据需求说明与代码变更生成"),
            "diff_summary": summary,
            "environment": environment or {},
            "git_source": git_source or {},
        },
        "risks": risks,
        "impacts": impacts,
        "test_points": test_points,
        "execution_plan": build_execution_plan(test_points, risks),
        "quality_gate": {},
        "metrics": {
            "analysis_duration_ms": 0,
            "recommended_count": len(test_points),
            "adopted_count": len(test_points),
        },
        "report_markdown": "",
        "gate_mode": "observe",
    }
    model["quality_gate"] = calculate_quality_gate(model["execution_plan"], model["risks"], "observe")
    return model


def calculate_quality_gate(execution_plan, risks, gate_mode="observe"):
    risk_priorities = {risk["id"]: risk["priority"] for risk in risks}
    counts = Counter(str(item.get("status") or "PENDING").upper() for item in execution_plan)
    p0_items = [
        item for item in execution_plan
        if any(risk_priorities.get(risk_id) == "P0" for risk_id in item.get("risk_ids", []))
    ]
    p1_failures = [
        item for item in execution_plan
        if item.get("status") == "FAIL"
        and any(risk_priorities.get(risk_id) == "P1" for risk_id in item.get("risk_ids", []))
    ]
    p2_failures = [
        item for item in execution_plan
        if item.get("status") == "FAIL"
        and any(risk_priorities.get(risk_id) == "P2" for risk_id in item.get("risk_ids", []))
    ]

    reasons = []
    decision = "PASS"
    if any(item.get("status") in {"PENDING", "BLOCKED", "SKIPPED"} for item in p0_items):
        decision = "BLOCKED"
        reasons.append("存在 P0 测试点未完成")
    elif any(item.get("status") == "FAIL" for item in p0_items):
        decision = "BLOCKED"
        reasons.append("存在 P0 测试失败")
    elif any(item.get("status") == "ENV_ERROR" for item in p0_items):
        decision = "REVIEW_REQUIRED"
        reasons.append("P0 验证受到环境问题影响")
    elif any(not item.get("workaround") for item in p1_failures):
        decision = "BLOCKED"
        reasons.append("存在无规避方案的 P1 失败")
    elif p1_failures or p2_failures:
        decision = "CONDITIONAL_PASS"
        reasons.append("存在非阻断失败，需要接受遗留风险")
    elif any(item.get("status") == "ENV_ERROR" for item in execution_plan):
        decision = "REVIEW_REQUIRED"
        reasons.append("存在环境失败，需要人工确认覆盖有效性")
    elif any(item.get("status") == "PENDING" for item in execution_plan):
        decision = "REVIEW_REQUIRED"
        reasons.append("测试尚未全部执行")
    else:
        reasons.append("P0 全部通过且无阻断缺陷")

    completed = sum(counts[state] for state in ("PASS", "FAIL", "BLOCKED", "SKIPPED", "ENV_ERROR"))
    total = len(execution_plan)
    return {
        "decision": decision,
        "mode": gate_mode,
        "enforced": gate_mode == "enforce",
        "would_block_release": decision == "BLOCKED",
        "release_blocked": gate_mode == "enforce" and decision == "BLOCKED",
        "reasons": reasons,
        "counts": {status: counts.get(status, 0) for status in ALLOWED_STATUSES},
        "coverage_rate": round(completed * 100 / total, 1) if total else 0,
        "p0_total": len(p0_items),
        "p0_passed": sum(item.get("status") == "PASS" for item in p0_items),
    }


def apply_execution_results(model, results, gate_mode=None):
    result_map = {
        str(item.get("execution_id") or ""): item
        for item in results or []
        if isinstance(item, dict)
    }
    for execution in model.get("execution_plan", []):
        update = result_map.get(execution["id"])
        if not update:
            continue
        status = str(update.get("status") or "").upper()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"不支持的执行状态: {status}")
        execution["status"] = status
        execution["bug_id"] = str(update.get("bug_id") or "").strip()
        execution["note"] = str(update.get("note") or "").strip()
        execution["workaround"] = bool(update.get("workaround"))
        execution["evidence"] = _unique_strings(update.get("evidence"), 10)
        execution["updated_at"] = int(time.time())
    if gate_mode in {"observe", "enforce"}:
        model["gate_mode"] = gate_mode
    model["updated_at"] = int(time.time())
    model["quality_gate"] = calculate_quality_gate(
        model.get("execution_plan", []),
        model.get("risks", []),
        model.get("gate_mode", "observe"),
    )
    return model


def build_report_markdown(model):
    gate = model["quality_gate"]
    change = model["change"]
    risk_counts = Counter(risk["priority"] for risk in model["risks"])
    failed = [item for item in model["execution_plan"] if item["status"] == "FAIL"]
    env_errors = [item for item in model["execution_plan"] if item["status"] == "ENV_ERROR"]
    lines = [
        "# VOD 精准回归测试结论",
        "",
        f"- **版本**：{change.get('version')}",
        f"- **变更**：{change.get('summary')}",
        f"- **质量结论**：**{gate['decision']}**",
        f"- **门禁模式**：{'强制阻断' if gate['enforced'] else '观察模式（不实际阻断发布）'}",
        f"- **覆盖率**：{gate['coverage_rate']}%",
        f"- **风险分布**：P0 {risk_counts['P0']} / P1 {risk_counts['P1']} / P2 {risk_counts['P2']}",
        "",
        "## 结论依据",
    ]
    lines.extend(f"- {reason}" for reason in gate["reasons"])
    lines.extend(["", "## 失败与环境问题"])
    if not failed and not env_errors:
        lines.append("- 暂无")
    for item in failed:
        lines.append(f"- FAIL {item['id']}：{item.get('note') or '未填写说明'} {item.get('bug_id') or ''}".rstrip())
    for item in env_errors:
        lines.append(f"- ENV_ERROR {item['id']}：{item.get('note') or '测试环境不可用'}")
    lines.extend(["", "## 执行明细", "", "| 测试项 | 优先级 | 执行方式 | 结果 | Bug | 证据 |", "|---|---|---|---|---|---|"])
    points = {point["id"]: point for point in model["test_points"]}
    for item in model["execution_plan"]:
        point = points.get(item["test_point_id"], {})
        evidence = item.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        if not isinstance(evidence, list):
            evidence = []
        evidence_text = "；".join(str(value).strip() for value in evidence if str(value).strip())
        if len(evidence_text) > 120:
            evidence_text = evidence_text[:117] + "..."
        lines.append(
            f"| {point.get('title', item['test_point_id'])} | {item['priority']} | "
            f"{item['executor_name']} / {item['mode']} | {item['status']} | {item.get('bug_id') or '-'} | {evidence_text or '-'} |"
        )
    return "\n".join(lines)
