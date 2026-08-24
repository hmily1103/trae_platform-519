import json
import re
import time
import uuid
from collections import Counter


ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
ALLOWED_STATUSES = {"PENDING", "PASS", "FAIL", "BLOCKED", "SKIPPED", "ENV_ERROR"}
ALLOWED_MODES = {"auto", "semi_auto", "manual"}
GATE_RULE_VERSION = "precision_gate_v1"

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

# 功能分类信号（不含「异常」），用于把被误判为「异常」的风险回正到业务分类。
# 顺序即优先级：命中前者优先归入该业务分类，故「点歌/播放」排在「服务端/跨端」前。
FUNCTIONAL_SIGNALS = tuple(
    (category, priority, pattern)
    for (category, priority, pattern) in VOD_SIGNALS
    if category != "异常"
)


def refine_category(raw_category, text):
    """把 LLM 可能误判的「异常」回正到功能分类（确定性安全网，不依赖模型）。

    规则：
      - 若 AI 给出的是具体功能分类（点歌/播放/设备/服务端/跨端），原样信任保留。
      - 若 AI 给出「异常」，但文本命中任一功能信号，说明改的是某业务子系统里的
        容错/兜底代码，应归到该业务分类（如「修复播放解码崩溃」→ 播放）。
      - 仅当文本确实以韧性/容灾为主线（无明确功能子系统信号）时才保留「异常」
        （如全局熔断/统一降级框架）。
      - 若 AI 给的分类完全不在允许集合，用 detect_vod_categories 兜底。
    """
    valid = set(CATEGORY_EXECUTORS)
    cat = str(raw_category or "").strip()
    if cat not in valid:
        cat = detect_vod_categories(text or "")[0]["category"]
    if cat != "异常":
        return cat
    # 仅当 AI 判为「异常」时才做回正检查
    for category, _priority, pattern in FUNCTIONAL_SIGNALS:
        if re.search(pattern, text or "", re.I):
            return category
    # 无功能子系统信号，确属韧性/容灾主线，保留「异常」
    return "异常"


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
    if summary.get("runtime_impact") == "none":
        change_type = summary.get("change_type") or "non_runtime"
        title_map = {
            "ai_tooling": "AI 协作工具配置变更不影响 VOD 运行时",
            "documentation": "文档变更不影响 VOD 运行时",
            "test_only": "测试代码变更不影响产品运行时",
        }
        return [{
            "id": "R01",
            "category": "跨端",
            "title": title_map.get(change_type, "非运行时变更不影响 VOD 主链路"),
            "priority": "P2",
            "impact_type": "indirect",
            "affected_users": "开发、测试或协作人员",
            "scope": summary.get("reason") or "本次改动未进入产品运行时链路",
            "evidence": summary.get("files") or ["Diff 文件列表"],
            "confidence": "high",
        }]

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
    if summary.get("runtime_impact") == "none":
        return _default_risks(requirement, code_diff, summary)

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
        # 回正：把被误判的「异常」归位到命中的功能分类（确定性安全网）
        category = refine_category(
            category,
            f"{item.get('title', '')} {item.get('scope', '')} "
            f"{' '.join(item.get('evidence') or [])}",
        )
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


def _target_test_point_count(risks, summary):
    if summary.get("runtime_impact") == "none":
        return 3
    if any(risk.get("priority") == "P0" for risk in risks):
        return 10
    if any(risk.get("priority") == "P1" for risk in risks):
        return 8
    return 5


def _non_runtime_test_points(risks, summary):
    risk_id = risks[0]["id"] if risks else "R01"
    change_type = summary.get("change_type") or "non_runtime"
    if change_type == "ai_tooling":
        scenarios = [
            ("配置校验", "验证 AI 工具配置格式", "检查 JSON/Markdown 配置可解析", "配置格式正确，无语法错误"),
            ("协作流程", "验证 Hook/Command 不阻断正常开发", "按团队常用命令做一次干跑或人工检查", "不会误拦截正常代码编辑和文档维护"),
            ("产物影响", "确认 VOD 编译产物无变化", "确认改动文件不参与 Android/服务端构建产物", "无需执行点歌、播放、机顶盒全链路回归"),
        ]
    elif change_type == "documentation":
        scenarios = [
            ("文档校验", "验证文档内容准确性", "人工核对文档描述与当前流程一致", "文档无误导信息"),
            ("链接校验", "验证文档链接与引用", "检查新增链接、路径或命令引用", "链接和路径可访问"),
            ("产物影响", "确认文档变更无运行时影响", "确认改动文件不进入 VOD 编译和部署产物", "无需执行产品主链路回归"),
        ]
    else:
        scenarios = [
            ("测试资产", "验证测试代码可运行", "执行受影响测试或做语法检查", "测试资产本身可用"),
            ("覆盖意图", "确认测试改动覆盖目标风险", "核对测试名称、断言和数据准备", "测试意图清晰且不误报"),
            ("产物影响", "确认产品代码无变化", "确认 Diff 不包含产品运行时代码", "无需执行完整 VOD 回归"),
        ]
    points = []
    for index, (point_type, title, steps, expected) in enumerate(scenarios, 1):
        points.append({
            "id": f"T{index:02d}",
            "risk_ids": [risk_id],
            "priority": "P2",
            "type": point_type,
            "title": title,
            "precondition": "已获取本次 Diff 与需求说明",
            "steps": steps,
            "expected": expected,
            "mode": "manual",
            "source": "rule",
        })
    return points


def _build_test_points(raw_points, risks, summary=None):
    """合并 LLM 生成的有效测试点与分类模板补充，保证覆盖且不浪费模型智能。

    - LLM 返回的测试点只要 risk_ids 合法就保留（标记 source=llm）
    - 不足 10 条时按风险分类用模板补充（标记 source=template），而非清空重来
    - 极端情况下用通用 fallback 场景补到 10 条
    """
    summary = summary or {}
    if summary.get("runtime_impact") == "none":
        return _non_runtime_test_points(risks, summary)

    points = []
    items = raw_points if isinstance(raw_points, list) else []
    risk_by_id = {risk["id"]: risk for risk in risks}
    target_count = _target_test_point_count(risks, summary)

    # 1. 保留 LLM 生成的有效测试点（不再因数量不足而整体丢弃）
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        linked = [rid for rid in _unique_strings(item.get("risk_ids"), 4) if rid in risk_by_id]
        if not linked:
            continue
        risk = risk_by_id[linked[0]]
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
            "source": "llm",
        })

    # 2. 不足目标条数时，按风险分类用模板补充（保留 LLM 点，仅补齐缺失类别）
    if len(points) < target_count:
        for risk in risks:
            if len(points) >= target_count:
                break
            for point_type, title, steps, expected in _test_templates(risk):
                if any(p["title"] == title for p in points):
                    continue
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
                    "source": "template",
                })
                if len(points) >= target_count:
                    break

    # 3. 极端情况（LLM 点与模板补充后仍不足目标条数）用通用场景补齐
    #    先按标题去重；若一轮全部重复仍凑不够，则放宽允许重复以保证达到目标条数
    fallback_scenarios = [
        ("冒烟场景", "核心链路冒烟验证", "完成搜索、点歌、起播、切歌主链路", "主链路可用且各端状态一致"),
        ("日志场景", "关键异常日志检查", "执行主流程并检查设备与服务端日志", "无新增Crash、ANR或高频错误"),
        ("性能场景", "核心链路性能基线", "执行主流程并采集CPU、内存、FPS", "指标不劣化且无明显卡顿"),
        ("恢复场景", "中断后恢复验证", "主流程中断网络或重启后重新进入", "状态可预测且数据不丢失"),
        ("兼容场景", "目标设备矩阵验证", "选择代表性型号与固件执行主流程", "各设备行为符合统一业务规则"),
    ]
    fallback_index = 0
    fallback_dedup = True
    while len(points) < target_count:
        risk = risks[fallback_index % len(risks)]
        point_type, title, steps, expected = fallback_scenarios[fallback_index % len(fallback_scenarios)]
        if fallback_dedup and any(p["title"] == title for p in points):
            fallback_index += 1
            if fallback_index >= len(fallback_scenarios):
                fallback_dedup = False
                fallback_index = 0
            continue
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
            "source": "template",
        })
        fallback_index += 1

    points.sort(key=lambda p: {"P0": 0, "P1": 1, "P2": 2}[p["priority"]])
    for index, point in enumerate(points[:20], 1):
        point["id"] = f"T{index:02d}"
    return points[:20]


EXECUTOR_ROUTING_RULES = (
    {
        "executor": "song_order",
        "mode": "auto",
        "reason": "点歌、搜索、队列或收藏逻辑优先走点歌模块",
        "categories": {"点歌"},
        "pattern": r"点歌|搜索|队列|歌单|收藏|切歌顺序|song|music|queue|favorite|collect|order|search",
    },
    {
        "executor": "api_stress",
        "mode": "auto",
        "reason": "接口、参数、幂等、并发或缓存一致性优先走 API 压测",
        "categories": {"点歌", "服务端", "异常"},
        "pattern": r"接口|参数|错误码|幂等|并发|请求|响应|缓存|Redis|数据库|服务端|API|Controller|Service|DB",
    },
    {
        "executor": "player_stress",
        "mode": "semi_auto",
        "reason": "播放画面、声音、播控栏、卡顿或图层问题优先走播放器压测",
        "categories": {"播放", "跨端"},
        "pattern": r"播放|起播|切歌|暂停|继续|seek|卡顿|黑屏|无声|音画|解码|播控栏|ControlBar|PlayControl|图层|弹框|遮挡|player|playback|video|audio",
    },
    {
        "executor": "ui_automation",
        "mode": "semi_auto",
        "reason": "页面、弹框、按钮、布局或交互流程优先走 UI 自动化",
        "categories": {"播放", "设备", "跨端"},
        "pattern": r"页面|按钮|弹框|退出|遮挡|背景图|布局|显示|交互|扫码|登录|UI|View|Dialog|Activity|Fragment|Layout|Pad",
    },
    {
        "executor": "performance_monitor",
        "mode": "auto",
        "reason": "CPU、内存、FPS、卡顿或资源占用优先走性能监控",
        "categories": {"播放", "设备", "跨端"},
        "pattern": r"CPU|内存|FPS|性能|卡顿|掉帧|资源占用|memory|perf|jank",
    },
    {
        "executor": "log_monitor",
        "mode": "auto",
        "reason": "Crash、ANR、异常日志、错误码优先走日志监控",
        "categories": {"播放", "设备", "服务端", "跨端", "异常"},
        "pattern": r"日志|错误|异常|Crash|ANR|Exception|Error|超时|timeout|decoder|解码",
    },
    {
        "executor": "combined_test",
        "mode": "auto",
        "reason": "跨端状态、重启恢复、断网弱网或多模块组合优先走组合测试",
        "categories": {"设备", "跨端", "异常"},
        "pattern": r"跨端|同步|状态一致|主盒|Pad|中控|移动端|重启|恢复|断网|弱网|断电|扫码|连接|房台|组合",
    },
    {
        "executor": "reboot",
        "mode": "auto",
        "reason": "启动、冷启动、升级或多轮重启优先走中控重启",
        "categories": {"设备", "异常"},
        "pattern": r"启动|冷启动|重启|升级|固件|断电|boot|reboot|upgrade|firmware",
    },
    {
        "executor": "monkey",
        "mode": "auto",
        "reason": "稳定性、随机操作、Crash/ANR 探测优先走 Monkey",
        "categories": {"设备", "跨端", "异常"},
        "pattern": r"稳定|随机|长稳|Crash|ANR|monkey|压力",
    },
    {
        "executor": "server_stress",
        "mode": "auto",
        "reason": "服务高负载、ARM 服务器资源或服务端压测优先走 ARM 服务器压测",
        "categories": {"服务端"},
        "pattern": r"高负载|压测|ARM|服务器资源|吞吐|QPS|并发房台|server_stress",
    },
)


def _executor_for_point(point, risk):
    text = " ".join(str(value or "") for value in (
        point.get("title"),
        point.get("type"),
        point.get("steps"),
        point.get("expected"),
        risk.get("title"),
        risk.get("scope"),
        " ".join(risk.get("evidence") or []),
    ))
    if point.get("mode") == "manual" and point.get("source") == "rule":
        return "manual", "manual", "规则判断为轻量人工验证"
    candidates = list(CATEGORY_EXECUTORS.get(risk["category"], ["manual"]))
    matches = []
    for rule in EXECUTOR_ROUTING_RULES:
        if risk.get("category") not in rule["categories"] and rule["executor"] not in candidates:
            continue
        if re.search(rule["pattern"], text, re.I):
            matches.append(rule)
    if matches:
        chosen = matches[0]
        return chosen["executor"], chosen["mode"], chosen["reason"]
    executor_id = candidates[0] if candidates else "manual"
    mode = point.get("mode")
    if mode not in ALLOWED_MODES:
        mode = EXECUTORS[executor_id]["mode"]
    return executor_id, mode, f"按风险分类 {risk.get('category')} 默认映射"


def _build_executor_url_with_env(executor_url, environment, tracking=None):
    """把执行环境（设备/型号/固件/版本/服务端）预填到执行器页面链接的 query 参数。

    统一前缀 `env_` 避免与各执行器页面自身参数冲突；各页面可选消费，不消费也不影响。
    返回空串（manual 等无 url 的执行器）或带 query 的绝对/相对路径。
    """
    if not executor_url:
        return ""
    env = environment or {}
    params = {}
    mapping = {
        "env_device_id": "device_id",
        "env_stb_model": "stb_model",
        "env_firmware": "firmware",
        "env_vod_version": "vod_version",
        "env_server": "server",
    }
    for qkey, ekey in mapping.items():
        val = str(env.get(ekey) or "").strip()
        if val:
            params[qkey] = val
    for key, value in (tracking or {}).items():
        val = str(value or "").strip()
        if val:
            params[key] = val
    if not params:
        return executor_url
    from urllib.parse import urlencode
    sep = "&" if "?" in executor_url else "?"
    return executor_url + sep + urlencode(params)


def build_execution_plan(test_points, risks, environment=None):
    risk_map = {risk["id"]: risk for risk in risks}
    plan = []
    for point in test_points:
        risk = risk_map[point["risk_ids"][0]]
        executor_id, mode, routing_reason = _executor_for_point(point, risk)
        executor = EXECUTORS[executor_id]
        execution_id = f"E{len(plan) + 1:02d}"
        tracking = {
            "precision_analysis_id": environment.get("analysis_id") if isinstance(environment, dict) else "",
            "precision_test_point_id": point["id"],
            "precision_execution_id": execution_id,
        }
        plan.append({
            "id": execution_id,
            "test_point_id": point["id"],
            "risk_ids": point["risk_ids"],
            "priority": point["priority"],
            "mode": mode,
            "executor": executor_id,
            "executor_name": executor["name"],
            "routing_reason": routing_reason,
            "executor_url": executor["url"],
            "executor_url_with_env": _build_executor_url_with_env(executor["url"], environment, tracking),
            "status": "PENDING",
            "bug_id": "",
            "note": "",
            "evidence": [],
            "workaround": False,
        })
    return plan


def _contains_any(text, keywords):
    return any(keyword.lower() in text.lower() for keyword in keywords)


def _build_tester_brief(risks, test_points, summary):
    text = "\n".join(
        " ".join(str(value or "") for value in (
            risk.get("title"),
            risk.get("scope"),
            " ".join(risk.get("evidence") or []),
        ))
        for risk in risks
    )
    p0_count = sum(1 for risk in risks if risk.get("priority") == "P0")
    p1_count = sum(1 for risk in risks if risk.get("priority") == "P1")
    focus = []
    confirmations = []
    plain = []

    if summary.get("runtime_impact") == "none":
        plain.append("本次改动未进入 VOD 产品运行时，重点确认配置/文档/测试资产本身是否正确。")
        focus.append("确认改动文件不参与 Android、机顶盒或服务端发布产物")
    else:
        if p0_count:
            plain.append(f"本次存在 {p0_count} 个 P0 风险，必须先完成主链路验证再给上线结论。")
        elif p1_count:
            plain.append(f"本次以 P1 风险为主，建议完成代表性设备和核心接口验证。")
        else:
            plain.append("本次未识别到 P0/P1 主风险，可按轻量回归处理。")

    if _contains_any(text, ["PlayControlBar", "播控栏", "MultiModePlayControlBarView"]):
        plain.append("改动影响播放控制栏，真实风险集中在播放、暂停、继续、切歌、退出弹框和图层显示。")
        focus.extend([
            "Pad 进入播放页后播控栏显示和按钮响应正常",
            "播放中执行暂停、继续、切歌、退出，弹框不被背景图或三方应用图层遮挡",
            "RK3576 与非 RK3576 Pad 至少各选一台做对比验证",
        ])
    if _contains_any(text, ["getMainBoxModel", "MAIN_BOX_MODEL", "主盒型号", "TS_KTV_X9"]):
        plain.append("改动涉及主盒型号判断，可能把多型号适配逻辑统一收敛到 X9，需要研发明确这是有意设计。")
        confirmations.append("请研发确认：getMainBoxModel() 固定返回 TS_KTV_X9 是否为本次需求设计，是否允许废弃动态主盒型号。")
        focus.extend([
            "扫码初始化后 Pad 能正确连接主盒并进入业务页面",
            "依赖主盒型号的页面、播放控制、三方应用图层逻辑仍符合当前产品预期",
        ])
    if _contains_any(text, ["Pad", "isPadChip", "平板"]):
        focus.append("覆盖 Pad 设备，不只验证 X9 主盒")
    if _contains_any(text, ["扫码", "ScanCode", "Initialize"]):
        focus.append("覆盖扫码初始化、断开重连和重新进入后的状态恢复")

    if not focus:
        focus = [point.get("title") for point in test_points[:5] if point.get("title")]
    return {
        "plain_summary": _unique_strings(plain, 4),
        "must_confirm": _unique_strings(confirmations, 5),
        "verification_focus": _unique_strings(focus, 8),
        "suggested_action": "先确认设计意图，再执行最小验证集" if confirmations else "按最小验证集执行",
    }


def _build_confirmations(brief, risks):
    priority = "P2"
    if any(risk.get("priority") == "P0" for risk in risks):
        priority = "P0"
    elif any(risk.get("priority") == "P1" for risk in risks):
        priority = "P1"
    return [{
        "id": f"C{index:02d}",
        "title": text,
        "priority": priority,
        "status": "OPEN",
        "assignee": "研发负责人",
        "response": "",
        "confirmed_by": "",
        "confirmed_at": 0,
        "evidence": [],
    } for index, text in enumerate(brief.get("must_confirm") or [], 1)]


def _compute_adoption_metrics(model):
    """根据执行结果计算采纳率：给出明确执行结论（非 PENDING）的测试点视为已采纳。

    按测试点来源(source)拆出 LLM 采纳率与模板采纳率，作为"精准回归"的可度量指标：
    - 整体采纳率 = 已采纳测试点 / 推荐测试点
    - LLM 采纳率 = 已采纳的 AI 生成点 / AI 生成点总数
    - 模板采纳率 = 已采纳的模板补充点 / 模板补充点总数
    """
    test_points = model.get("test_points") or []
    execution_plan = model.get("execution_plan") or []
    source_by_point = {p["id"]: p.get("source") or "custom" for p in test_points}

    llm_recommended = sum(1 for p in test_points if p.get("source") == "llm")
    template_recommended = sum(1 for p in test_points if p.get("source") == "template")
    recommended = len(test_points)
    custom_recommended = recommended - llm_recommended - template_recommended

    adopted = llm_adopted = template_adopted = custom_adopted = 0
    for item in execution_plan:
        if str(item.get("status") or "PENDING").upper() == "PENDING":
            continue
        adopted += 1
        source = source_by_point.get(item.get("test_point_id"), "custom")
        if source == "llm":
            llm_adopted += 1
        elif source == "template":
            template_adopted += 1
        else:
            custom_adopted += 1

    def _rate(part, whole):
        return round(part * 100.0 / whole, 1) if whole else 0.0

    metrics = model.setdefault("metrics", {})
    metrics["recommended_count"] = recommended
    metrics["custom_added_count"] = custom_recommended
    metrics["llm_generated_count"] = llm_recommended
    metrics["template_supplement_count"] = template_recommended
    metrics["adopted_count"] = adopted
    metrics["adoption_rate"] = _rate(adopted, recommended)
    metrics["llm_adopted_count"] = llm_adopted
    metrics["llm_adoption_rate"] = _rate(llm_adopted, llm_recommended)
    metrics["template_adopted_count"] = template_adopted
    metrics["template_adoption_rate"] = _rate(template_adopted, template_recommended)
    metrics["custom_adopted_count"] = custom_adopted
    metrics["custom_adoption_rate"] = _rate(custom_adopted, custom_recommended)
    return model


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
    test_points = _build_test_points(raw.get("test_points"), risks, summary)
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
    tester_brief = _build_tester_brief(risks, test_points, summary)
    confirmations = _build_confirmations(tester_brief, risks)
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
            "environment": dict(environment or {}, analysis_id=analysis_id),
            "git_source": git_source or {},
        },
        "risks": risks,
        "impacts": impacts,
        "tester_brief": tester_brief,
        "confirmations": confirmations,
        "test_points": test_points,
        "execution_plan": build_execution_plan(test_points, risks, dict(environment or {}, analysis_id=analysis_id)),
        "quality_gate": {},
        "metrics": {
            "analysis_duration_ms": 0,
            "recommended_count": len(test_points),
            "custom_added_count": sum(1 for p in test_points if p.get("source") not in ("llm", "template")),
            "llm_generated_count": sum(1 for p in test_points if p.get("source") == "llm"),
            "template_supplement_count": sum(1 for p in test_points if p.get("source") == "template"),
            "adopted_count": 0,
            "adoption_rate": 0.0,
            "llm_adopted_count": 0,
            "llm_adoption_rate": 0.0,
            "template_adopted_count": 0,
            "template_adoption_rate": 0.0,
            "custom_adopted_count": sum(1 for p in test_points if p.get("source") not in ("llm", "template")),
            "custom_adoption_rate": 0.0,
        },
        "report_markdown": "",
        "gate_mode": "observe",
    }
    model["quality_gate"] = calculate_quality_gate(
        model["execution_plan"], model["risks"], "observe", model.get("confirmations")
    )
    return model


def _open_blocking_confirmations(confirmations):
    return [
        item for item in confirmations or []
        if item.get("status") == "OPEN" and item.get("priority") in {"P0", "P1"}
    ]


def calculate_quality_gate(execution_plan, risks, gate_mode="observe", confirmations=None):
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
    elif _open_blocking_confirmations(confirmations):
        decision = "REVIEW_REQUIRED"
        reasons.append("存在未关闭的高风险研发确认项")
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
        "rule_version": GATE_RULE_VERSION,
        "mode": gate_mode,
        "enforced": gate_mode == "enforce",
        "would_block_release": decision == "BLOCKED",
        "release_blocked": gate_mode == "enforce" and decision == "BLOCKED",
        "reasons": reasons,
        "counts": {status: counts.get(status, 0) for status in ALLOWED_STATUSES},
        "coverage_rate": round(completed * 100 / total, 1) if total else 0,
        "p0_total": len(p0_items),
        "p0_passed": sum(item.get("status") == "PASS" for item in p0_items),
        "open_confirmations": len(_open_blocking_confirmations(confirmations)),
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
        before = {
            "status": execution.get("status"),
            "bug_id": execution.get("bug_id"),
            "note": execution.get("note"),
            "workaround": execution.get("workaround"),
            "evidence": execution.get("evidence"),
        }
        status = str(update.get("status") or "").upper()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"不支持的执行状态: {status}")
        execution["status"] = status
        execution["bug_id"] = str(update.get("bug_id") or "").strip()
        execution["note"] = str(update.get("note") or "").strip()
        execution["workaround"] = bool(update.get("workaround"))
        execution["evidence"] = _unique_strings(update.get("evidence"), 10)
        execution["updated_at"] = int(time.time())
        after = {
            "status": execution.get("status"),
            "bug_id": execution.get("bug_id"),
            "note": execution.get("note"),
            "workaround": execution.get("workaround"),
            "evidence": execution.get("evidence"),
        }
        if before != after:
            model.setdefault("audit_log", []).append({
                "ts": int(time.time()),
                "action": "execution_result_update",
                "source": str(update.get("source") or "manual"),
                "actor": str(update.get("actor") or "unknown"),
                "execution_id": execution["id"],
                "before": before,
                "after": after,
            })
    if gate_mode in {"observe", "enforce"}:
        model["gate_mode"] = gate_mode
    model["updated_at"] = int(time.time())
    model["quality_gate"] = calculate_quality_gate(
        model.get("execution_plan", []),
        model.get("risks", []),
        model.get("gate_mode", "observe"),
        model.get("confirmations"),
    )
    model = _compute_adoption_metrics(model)
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
        f"- **执行覆盖率**：{gate['coverage_rate']}%",
        f"- **采纳率**：{model.get('metrics', {}).get('adoption_rate', 0)}%（LLM {model.get('metrics', {}).get('llm_adoption_rate', 0)}% / 模板 {model.get('metrics', {}).get('template_adoption_rate', 0)}%）",
        f"- **风险分布**：P0 {risk_counts['P0']} / P1 {risk_counts['P1']} / P2 {risk_counts['P2']}",
        "",
        "## 测试负责人解读",
    ]
    brief = model.get("tester_brief") or {}
    for item in brief.get("plain_summary") or []:
        lines.append(f"- {item}")
    if brief.get("must_confirm"):
        lines.extend(["", "## 需研发确认"])
        lines.extend(f"- {item}" for item in brief["must_confirm"])
    if brief.get("verification_focus"):
        lines.extend(["", "## 建议优先验证"])
        lines.extend(f"- {item}" for item in brief["verification_focus"])
    lines.extend([
        "",
        "## 结论依据",
    ]
    )
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
