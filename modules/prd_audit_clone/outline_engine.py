# -*- coding: utf-8 -*-
import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List
from .rules_engine import build_rule_engine_model, run_rules
from .explain_engine import build_explainable_report
from .strategy_engine import build_strategy_report
from .rule_plugin_engine import resolve_rule_plugin, append_plugin_usage, load_rule_plugin_profiles
from .prompt_center import select_prompt_profile, evaluate_prompt_outcome, append_prompt_evaluation
from .explicit_outline_engine import generate_explicit_outline

try:
    from modules.prd_outline import OutlineEngine as _CoreOutlineEngine
except Exception:
    _CoreOutlineEngine = None

GENERIC_TITLES = {
    "正文",
    "内容",
    "文档",
    "章节",
    "标题",
    "概述",
    "说明",
    "需求",
    "PRD",
    "要求",
    "规则",
    "展示",
    "功能",
    "优先级",
    "暂无二级要点",
    "【PRD未说明】",
}

NOISE_KEYWORDS = {
    "节点", "ui设计", "客户端开发", "服务开发", "测试", "负责人",
    "制作&开发周期", "上线时间", "硬件开发", "背景描述", "发送",
}

DEFAULT_LINE_BUSINESS_HINTS = [
    "投屏", "游戏", "广告", "展示", "模式", "优先级", "打断", "恢复",
    "横屏", "竖屏", "场景", "画中画", "数字人", "切歌", "触摸屏",
]

OUTLINE_KEYWORD_PACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outline_keyword_packs.json")
_OUTLINE_PACK_CACHE: Dict[str, Any] = {}
_OUTLINE_PACK_MTIME: float = -1.0

CONNECTOR_ENDINGS = ("按", "需", "则", "为", "及", "和", "并", "与", "后", "前", "时")

DEFAULT_BUSINESS_LEXICON = {
    "tv端": "大屏端",
    "TV端": "大屏端",
    "触摸屏幕": "触摸屏",
    "gogo秀": "GOGO秀",
    "dj模式": "DJ模式",
    "ai数字人": "AI数字人",
    "欢迎欢送期间": "欢迎/欢送场景",
    "欢迎欢送": "欢迎/欢送场景",
    "展示页面": "展示页",
}

BUSINESS_LEXICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "business_lexicon.json")
_LEXICON_CACHE: Dict[str, str] = {}
_LEXICON_MTIME: float = -1.0


def _to_text_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    return []


def _clean_title(s: str) -> str:
    t = unicodedata.normalize("NFKC", str(s or "")).strip()
    t = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", t)
    t = t.replace("\u3000", " ")
    t = t.replace("", " ")
    t = re.sub(r"[​]+", " ", t)
    t = re.sub(r"^[#\-\*\d\.\)\(一二三四五六七八九十、\s]+", "", t)
    t = re.sub(r"^[a-zA-Z]\.\s*", "", t)
    t = re.sub(r"^[•·●]+\s*", "", t)
    t = re.sub(r"(【PRD未说明】|暂无二级要点)", "", t)
    t = re.sub(r"\b(?:i|ii|iii|iv|v)\.\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip("：:；;，,。 ")
    return t


def _is_outline_broken_text(text: str) -> bool:
    t = _clean_title(text)
    if not t:
        return True
    if len(t) < 6:
        return True
    if re.search(r"(换脸|修音|(?<![A-Za-z])MV(?![A-Za-z])|mv换脸)", t, re.IGNORECASE):
        return True
    if re.search(r"(版$|此处规则需确认\)?$|^[a-zA-Z]$)", t):
        return True
    return False


def _is_outline_global_rule(text: str) -> bool:
    t = _clean_title(text)
    return bool(re.search(r"(仅支持|范围覆盖|定制版|标准版|机顶盒上运行|触摸屏和电视端均有展示|适用范围)", t))


def _looks_like_outline_role(text: str) -> bool:
    t = _clean_title(text)
    if not t:
        return False
    if re.search(r"^(在|当|若|如果|用户点击|用户使用|用户通过|歌曲最终是否|此处)", t):
        return False
    return bool(re.search(r"(用户|机顶盒|大屏端|TV端|电视端|手机端|手机APP|APP|小程序|云端|服务端|客户端|平台|系统|运营|研发|测试|管理员|商户|客服)$", t))


def _pick_outline_roles(stage1: Dict[str, Any], candidates: List[str]) -> List[str]:
    seeds = _to_text_list(stage1.get("user_roles")) + _to_text_list(stage1.get("modules"))
    vals: List[str] = []
    for x in seeds + candidates:
        t = _clean_title(x)
        if not t or not _looks_like_outline_role(t):
            continue
        vals.append(t)
    return _semantic_dedup_rules(vals, threshold=0.92, limit=8)


def _derive_outline_l0(stage1: Dict[str, Any], model: Dict[str, Any]) -> str:
    goal = next((x for x in _to_text_list(stage1.get("goal")) if not _is_outline_broken_text(x)), "")
    flows = [x for x in _to_text_list(stage1.get("flows")) if not _is_outline_broken_text(x)]
    rules = [x for x in _to_text_list(stage1.get("business_rules")) if not _is_outline_broken_text(x)]
    feature = next((x for x in _to_text_list(stage1.get("modules")) if len(_clean_title(x)) >= 2), "")
    if goal and len(_clean_title(goal)) >= 12:
        return _clean_title(goal)
    main_flow = _clean_title(flows[0]) if flows else ""
    key_rule = next((r for r in rules if any(k in r for k in ["上传", "保存", "扫码", "开关", "录音", "投屏", "支付"])), "")
    parts = []
    if feature:
        parts.append(_clean_title(feature))
    if main_flow:
        parts.append(main_flow[:22])
    if key_rule:
        parts.append(_clean_title(key_rule)[:22])
    if parts:
        return "，".join(parts[:3]).strip("，")
    return "本PRD用于定义核心功能主流程、关键规则和异常处理口径。"


def _state_keywords(state_name: str) -> List[str]:
    s = _clean_title(state_name)
    mapping = {
        "关闭": ["关闭", "不录制", "关闭开关", "关闭状态"],
        "开启": ["开启", "自动开启", "打开开关", "开始录制"],
        "录制中": ["录制中", "录音中", "红点", "TV右侧", "录制状态"],
        "保存": ["保存", "大于10秒", "录音作品", "落盘"],
        "上传": ["上传", "云端", "待上传", "上传成功"],
        "列表": ["列表", "已唱列表", "二维码", "扫码", "小程序", "获取录音", "手机端"],
    }
    for key, kws in mapping.items():
        if key in s:
            return kws
    return re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", s)


def _pick_state_specific_rules(state_name: str, rules: List[str], flows: List[str]) -> List[str]:
    kws = _state_keywords(state_name)
    out: List[str] = []
    for x in rules + flows:
        t = _clean_title(x)
        if not t or _is_outline_broken_text(t) or _is_outline_global_rule(t):
            continue
        if any(k in t for k in kws):
            out.append(t)
    return _semantic_dedup_rules(out, threshold=0.9, limit=3)


def _apply_business_lexicon(text: str) -> str:
    t = _clean_title(text)
    if not t:
        return t
    for src, dst in _load_business_lexicon().items():
        t = t.replace(src, dst)
    t = _clean_title(t)
    return t


def _load_business_lexicon() -> Dict[str, str]:
    global _LEXICON_CACHE, _LEXICON_MTIME
    mtime = -1.0
    try:
        if os.path.exists(BUSINESS_LEXICON_FILE):
            mtime = float(os.path.getmtime(BUSINESS_LEXICON_FILE))
    except Exception:
        mtime = -1.0
    if _LEXICON_CACHE and mtime == _LEXICON_MTIME:
        return _LEXICON_CACHE
    merged = dict(DEFAULT_BUSINESS_LEXICON)
    if mtime >= 0:
        try:
            with open(BUSINESS_LEXICON_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kk = _clean_title(k)
                    vv = _clean_title(v)
                    if kk and vv:
                        merged[kk] = vv
        except Exception:
            pass
    _LEXICON_CACHE = merged
    _LEXICON_MTIME = mtime
    return _LEXICON_CACHE


def _default_outline_pack_data() -> Dict[str, Any]:
    return {
        "default_pack": "generic",
        "system_type_pack": {
            "general": "generic",
            "state_machine": "generic",
            "business_flow": "generic",
            "scheduling_system": "scheduling",
        },
        "plugin_pack_map": {
            "generic_universal": "generic",
            "lite_gate": "generic",
            "scheduling_strict": "scheduling",
        },
        "packs": {
            "generic": {
                "line_business_hints": list(DEFAULT_LINE_BUSINESS_HINTS),
                "detect_system_type": {
                    "priority": ["优先级", "打断", "恢复", "抢占", ">"],
                    "display": ["展示", "播放", "投屏", "广告", "画面", "屏幕"],
                    "state": ["状态", "流转", "状态机", "进入", "退出"],
                    "business_flow": ["订单", "支付", "下单", "审批", "工单"],
                },
                "core_brief": {
                    "roles": ["角色", "参与方", "分工", "职责", "责任方", "合作方", "用户", "商户", "平台", "甲方", "乙方", "运营", "研发", "测试", "财务", "客服", "管理员"],
                    "payment": ["付费", "计费", "结算", "对账", "购买", "扣费", "分成", "账单", "订单", "成本"],
                    "flow": ["主流程", "核心流程", "流程", "步骤", "创建", "提交", "审批", "支付", "执行", "进入", "退出", "完成", "回滚"],
                    "integration": ["接口", "API", "token", "Token", "回调", "校验", "鉴权", "同步", "消息", "webhook"],
                    "exceptions": ["异常", "失败", "超时", "断网", "恢复", "告警", "兜底", "重试"],
                    "implementation": ["试点", "推广", "培训", "上线", "排期", "阶段", "实施", "维护", "验收"],
                    "risks": ["风险", "应对", "隐患", "阻塞", "影响", "不可用"],
                    "confirms": ["待确认", "需确认", "待定", "未确认", "未说明"],
                    "positioning": ["定位", "目标", "方案", "用于", "接入", "商业化", "项目"],
                },
                "scheduling_extract": {
                    "mode_terms": ["模式", "横屏", "竖屏", "画中画", "壁画", "GOGO秀", "DJ模式"],
                    "scene_terms": ["欢迎/欢送场景", "非指定模式", "场景", "大屏端"],
                    "constraint_terms": ["有且只能", "仅", "不可", "禁止", "必须"],
                    "actor_terms": ["投屏", "游戏", "广告", "直播", "播放", "弹窗", "来电", "语音"],
                },
            },
            "scheduling": {
                "line_business_hints": ["投屏", "游戏", "广告", "优先级", "打断", "恢复", "模式", "场景", "屏幕", "画中画", "切歌", "数字人"],
                "detect_system_type": {
                    "priority": ["优先级", "打断", "恢复", "抢占", "中断", ">"],
                    "display": ["展示", "投屏", "广告", "画面", "屏幕", "模式"],
                },
            },
        },
    }


def _merge_outline_pack(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base or {})
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_outline_pack(out.get(k) or {}, v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _load_outline_pack_data() -> Dict[str, Any]:
    global _OUTLINE_PACK_CACHE, _OUTLINE_PACK_MTIME
    mtime = -1.0
    try:
        if os.path.exists(OUTLINE_KEYWORD_PACK_FILE):
            mtime = float(os.path.getmtime(OUTLINE_KEYWORD_PACK_FILE))
    except Exception:
        mtime = -1.0
    if _OUTLINE_PACK_CACHE and mtime == _OUTLINE_PACK_MTIME:
        return _OUTLINE_PACK_CACHE
    data = _default_outline_pack_data()
    if mtime >= 0:
        try:
            with open(OUTLINE_KEYWORD_PACK_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                data = _merge_outline_pack(data, obj)
        except Exception:
            pass
    _OUTLINE_PACK_CACHE = data
    _OUTLINE_PACK_MTIME = mtime
    return _OUTLINE_PACK_CACHE


def _plugin_keyword_pack_map() -> Dict[str, str]:
    mapping = {}
    try:
        for p in load_rule_plugin_profiles():
            if not isinstance(p, dict):
                continue
            pid = _clean_title(p.get("plugin_id"))
            pack = _clean_title(p.get("keyword_pack"))
            if pid and pack:
                mapping[pid] = pack
    except Exception:
        pass
    return mapping


def _get_outline_keyword_pack(system_type: str = "", plugin_id: str = "") -> Dict[str, Any]:
    data = _load_outline_pack_data()
    packs = data.get("packs") if isinstance(data.get("packs"), dict) else {}
    default_pack_name = _clean_title(data.get("default_pack")) or "generic"
    system_pack_name = _clean_title((data.get("system_type_pack") or {}).get(_clean_title(system_type)))
    plugin_pack_name = _clean_title((data.get("plugin_pack_map") or {}).get(_clean_title(plugin_id)))
    ext_plugin_map = _plugin_keyword_pack_map()
    plugin_pack_name = _clean_title(ext_plugin_map.get(_clean_title(plugin_id)) or plugin_pack_name)
    base_pack = packs.get(default_pack_name) if isinstance(packs.get(default_pack_name), dict) else {}
    system_pack = packs.get(system_pack_name) if isinstance(packs.get(system_pack_name), dict) else {}
    plugin_pack = packs.get(plugin_pack_name) if isinstance(packs.get(plugin_pack_name), dict) else {}
    merged = _merge_outline_pack(base_pack, system_pack)
    merged = _merge_outline_pack(merged, plugin_pack)
    return merged if isinstance(merged, dict) else {}


def _pack_list(pack: Dict[str, Any], path: List[str], fallback: List[str]) -> List[str]:
    cur: Any = pack
    for p in path:
        if not isinstance(cur, dict):
            return list(fallback)
        cur = cur.get(p)
    if isinstance(cur, list) and cur:
        return [str(x) for x in cur if str(x).strip()]
    return list(fallback)


def _line_block_type(text: str) -> str:
    t = _clean_title(text)
    if not t:
        return "empty"
    low = t.lower()
    business_hints = set(_pack_list(_get_outline_keyword_pack(), ["line_business_hints"], DEFAULT_LINE_BUSINESS_HINTS))
    if any(k in low for k in NOISE_KEYWORDS):
        return "planning"
    if re.search(r"(负责人|上线时间|开发周期|客户端开发|服务开发|ui设计)", t, flags=re.IGNORECASE):
        return "planning"
    if re.search(r"(?:[\u4e00-\u9fff]{2,3}\s+){3,}", str(text or "")):
        if not any(k in t for k in business_hints):
            return "planning"
    if any(k in t for k in business_hints):
        return "rule"
    return "general"


def _dedup_repeated_text(line: str) -> str:
    s = _clean_title(line)
    if not s:
        return s
    for ln in range(min(28, len(s) // 2), 5, -1):
        left = s[:ln]
        right = s[ln: ln * 2]
        if left and right and left == right:
            s = _clean_title(left + s[ln * 2:])
            break
    for sep in [" ", "，", ",", "；", ";"]:
        parts = [p for p in s.split(sep) if p]
        if len(parts) >= 2 and parts[0] == parts[1]:
            s = sep.join([parts[0]] + parts[2:])
            s = _clean_title(s)
    return s


def _stitch_line_with_next(lines: List[str], idx: int) -> str:
    cur = _dedup_repeated_text(lines[idx]) if 0 <= idx < len(lines) else ""
    if not cur:
        return cur
    dangling = cur.endswith(("：", ":", "，", ",", "；", ";")) or cur.endswith(CONNECTOR_ENDINGS)
    if not dangling:
        return cur
    if idx + 1 >= len(lines):
        return cur
    nxt = _dedup_repeated_text(lines[idx + 1])
    if not nxt or _detect_heading_style(nxt) or _line_block_type(nxt) == "planning":
        return cur
    if len(nxt) > 60:
        nxt = nxt[:60]
    return _clean_title(cur + " " + nxt)


def _normalize_lines(content: str) -> List[str]:
    raw = [str(x).strip() for x in str(content or "").splitlines() if str(x).strip()]
    out: List[str] = []
    for line in raw:
        t = _dedup_repeated_text(_clean_title(line))
        if not t:
            continue
        if _line_block_type(t) == "planning":
            continue
        if out:
            prev = out[-1]
            join_cond = (
                len(prev) < 30
                or prev.endswith(("、", "，", ",", "：", ":", "及", "和", "按", "需", "则", "为"))
            )
            has_roman_heading = bool(re.match(r"^(?:i|ii|iii|iv|v)\.", t, flags=re.IGNORECASE))
            if join_cond and not _detect_heading_style(t) and not has_roman_heading and len(t) < 56:
                out[-1] = _clean_title(prev + " " + t)
                continue
        out.append(t)
    return out


def _extract_clause_candidates(content: str) -> List[str]:
    lines = _normalize_lines(content)
    clauses: List[str] = []
    for i, _ in enumerate(lines[:220]):
        ln = _stitch_line_with_next(lines, i)
        parts = re.split(r"[；;。]|(?<!\d)[,，](?!\d)", ln)
        for p in parts:
            t = _dedup_repeated_text(_clean_title(p))
            if not t:
                continue
            if len(t) < 6 or len(t) > 64:
                continue
            if _line_block_type(t) == "planning":
                continue
            if t in GENERIC_TITLES:
                continue
            clauses.append(t)
    return list(dict.fromkeys(clauses))[:120]


def _infer_modules_from_content(content: str) -> List[str]:
    text = _clean_title(content)
    seeds = ["投屏", "游戏", "广告", "画中画", "壁画", "gogo秀", "dj模式", "语音交互", "数字人", "星耀屏"]
    out = []
    for s in seeds:
        if s in text:
            out.append(s)
    for m in re.findall(r"([\u4e00-\u9fffA-Za-z]{2,8}模式)", text):
        mm = _clean_title(m)
        if mm and mm not in out and len(mm) <= 10:
            out.append(mm)
    return list(dict.fromkeys(out))[:10]


def _normalize_outline_title(title: str) -> str:
    t = _clean_title(title)
    if not t:
        return ""
    for sep in ["：", ":", "，", ",", "；", ";"]:
        if sep in t and len(t) > 18:
            head = _clean_title(t.split(sep, 1)[0])
            if 2 <= len(head) <= 16:
                t = head
                break
    return _clean_title(t)


def _is_bad_outline_title(t: str) -> bool:
    s = _clean_title(t)
    if not s:
        return True
    if s in GENERIC_TITLES:
        return True
    if _line_block_type(s) == "planning":
        return True
    if len(s) <= 4:
        return True
    if len(s) > 40:
        return True
    if any(x in s for x in ["负责人", "UI设计", "上线时间", "开发周期"]):
        return True
    if s.endswith(("按", "需", "则", "为", "：", "，", ",", "；", ";")):
        return True
    return False


def _post_clean_outline(outline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    idx = 1
    for it in outline:
        if not isinstance(it, dict):
            continue
        title = _normalize_outline_title(it.get("title") or "")
        if _is_bad_outline_title(title):
            continue
        children_raw = it.get("children") if isinstance(it.get("children"), list) else []
        children = []
        seen = set()
        for c in children_raw:
            cc = _clean_title(c)
            if not cc or cc in GENERIC_TITLES or cc in seen or len(cc) < 5:
                continue
            seen.add(cc)
            children.append(cc)
        out.append({"index": idx, "title": title, "children": children[:8]})
        idx += 1
    return out


def _enrich_outline_children(outline: List[Dict[str, Any]], child_candidates: List[str]) -> List[Dict[str, Any]]:
    used = set()
    out: List[Dict[str, Any]] = []
    for it in outline:
        if not isinstance(it, dict):
            continue
        title = _clean_title(it.get("title") or "")
        children = [str(x).strip() for x in (it.get("children") or []) if str(x).strip()]
        children = [_clean_title(x) for x in children if _clean_title(x) and len(_clean_title(x)) <= 48]
        if len(children) < 2:
            tks = set(_extract_tokens(title))
            for cand in child_candidates:
                c = _clean_title(cand)
                if not c or c in used or c in children:
                    continue
                if _is_bad_outline_title(c):
                    continue
                if len(c) > 48:
                    continue
                if any(sym in c for sym in ["：", ":", "；", ";"]) and len(c) > 24:
                    continue
                cks = set(_extract_tokens(c))
                if tks and cks and (tks & cks):
                    children.append(c)
                    used.add(c)
                if len(children) >= 4:
                    break
        if len(children) < 2:
            for cand in child_candidates:
                c = _clean_title(cand)
                if not c or c in used or c in children:
                    continue
                if _is_bad_outline_title(c):
                    continue
                if len(c) > 48:
                    continue
                if any(sym in c for sym in ["：", ":", "；", ";"]) and len(c) > 24:
                    continue
                children.append(c)
                used.add(c)
                if len(children) >= 3:
                    break
        out.append({"index": int(it.get("index") or len(out) + 1), "title": title, "children": children[:8]})
    return out


def _detect_heading_style(text: str) -> bool:
    t = str(text or "").strip()
    patterns = [
        r"^\d+\.",
        r"^\d+\.\d+",
        r"^#{1,6}\s+",
        r"^[一二三四五六七八九十]+、",
        r"^\(\d+\)",
        r"^（[一二三四五六七八九十\d]+）",
    ]
    return any(re.match(p, t) for p in patterns)


def _jaccard(a: str, b: str) -> float:
    sa = set([x for x in re.split(r"[^\w\u4e00-\u9fff]+", (a or "").lower()) if x])
    sb = set([x for x in re.split(r"[^\w\u4e00-\u9fff]+", (b or "").lower()) if x])
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _topic_name(text: str) -> str:
    t = str(text or "")
    rules = [
        ("产品目标", ["目标", "目的", "价值", "背景", "愿景"]),
        ("用户角色", ["用户", "角色", "人群", "权限", "管理员"]),
        ("核心流程", ["流程", "步骤", "进入", "退出", "切换", "恢复"]),
        ("异常处理", ["异常", "失败", "超时", "重试", "错误", "降级", "弱网"]),
        ("状态规则", ["状态", "规则", "约束", "优先级", "冲突", "口径"]),
        ("数据指标", ["数据", "字段", "埋点", "指标", "统计", "口径"]),
        ("依赖限制", ["依赖", "第三方", "主板", "平台", "兼容", "限制"]),
    ]
    for name, keys in rules:
        if any(k in t for k in keys):
            return name
    return "功能模块"


def _build_nodes_from_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for b in blocks[:80]:
        title = _clean_title(b.get("title") or "")
        level = int(b.get("level") or 1)
        if not title:
            continue
        if title in GENERIC_TITLES:
            continue
        if level < 1:
            level = 1
        if level > 4:
            level = 4
        nodes.append({"level": level, "title": title})
    return nodes


def _need_fallback_from_structured(nodes: List[Dict[str, Any]]) -> bool:
    if not nodes:
        return True
    lv1 = [str(n.get("title") or "").strip() for n in nodes if int(n.get("level") or 1) <= 1]
    lv2_count = sum(1 for n in nodes if int(n.get("level") or 1) >= 2)
    if len(lv1) <= 1 and lv2_count == 0:
        t = lv1[0] if lv1 else ""
        return (not t) or (t in GENERIC_TITLES)
    return False


def _build_nodes_semantic(paragraphs: List[str]) -> List[Dict[str, Any]]:
    if not paragraphs:
        return []
    clusters: List[List[str]] = [[paragraphs[0]]]
    for p in paragraphs[1:]:
        prev = clusters[-1][-1]
        sim = _jaccard(prev, p)
        if sim < 0.25:
            clusters.append([p])
        else:
            clusters[-1].append(p)
    nodes: List[Dict[str, Any]] = []
    for c in clusters[:30]:
        merged = " ".join(c)
        nodes.append({"level": 1, "title": _topic_name(merged)})
        for s in c[:3]:
            title = _clean_title(s)[:80]
            if title:
                nodes.append({"level": 2, "title": title})
    return nodes


def _dedup_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for n in nodes:
        k = (int(n.get("level") or 1), str(n.get("title") or ""))
        if not k[1] or k in seen:
            continue
        seen.add(k)
        out.append({"level": k[0], "title": k[1]})
    return out


def _score_quality(nodes: List[Dict[str, Any]], stage1: Dict[str, Any]) -> Dict[str, float]:
    modules = _to_text_list(stage1.get("modules"))
    flows = _to_text_list(stage1.get("flows"))
    exceptions = _to_text_list(stage1.get("exceptions"))
    has_l2 = any(int(n.get("level") or 0) >= 2 for n in nodes)
    structure = 60.0 + (20.0 if has_l2 else 0.0) + min(20.0, len(nodes) * 0.5)
    clarity = 50.0 + min(30.0, len(modules) * 6.0) + min(20.0, len(set([n.get("title") for n in nodes])) * 1.0)
    flow = 40.0 + min(35.0, len(flows) * 7.0) + (15.0 if exceptions else 0.0)
    structure = max(0.0, min(100.0, structure))
    clarity = max(0.0, min(100.0, clarity))
    flow = max(0.0, min(100.0, flow))
    overall = round((structure + clarity + flow) / 3.0, 1)
    return {
        "structure_completeness": round(structure, 1),
        "module_clarity": round(clarity, 1),
        "flow_completeness": round(flow, 1),
        "overall": overall,
    }


def _extract_tokens(text: str) -> List[str]:
    raw = [x for x in re.split(r"[^\w\u4e00-\u9fff]+", str(text or "").lower()) if x]
    out = []
    for t in raw:
        if len(t) >= 2 and t not in {"功能", "模块", "系统", "页面", "需求", "规则", "流程"}:
            out.append(t)
    return out


def _build_outline_by_modules(
    modules: List[str],
    child_candidates: List[str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used = set()
    clean_modules = []
    seen_modules = set()
    for m in modules:
        t = _clean_title(m)
        if not t or t in GENERIC_TITLES or t in seen_modules:
            continue
        seen_modules.add(t)
        clean_modules.append(t)
    for idx, m in enumerate(clean_modules[:12], start=1):
        mtoks = set(_extract_tokens(m))
        children = []
        for c in child_candidates:
            if c in used:
                continue
            ctoks = set(_extract_tokens(c))
            if mtoks and ctoks and (mtoks & ctoks):
                children.append(c)
                used.add(c)
            if len(children) >= 4:
                break
        if not children:
            for c in child_candidates:
                if c in used:
                    continue
                children.append(c)
                used.add(c)
                if len(children) >= 3:
                    break
        out.append({"index": idx, "title": m, "children": children})
    if out:
        return out
    return [{"index": 1, "title": "核心功能", "children": child_candidates[:8]}]


def _detect_system_type(content: str, stage1: Dict[str, Any]) -> str:
    text = str(content or "")
    text2 = " ".join(
        _to_text_list(stage1.get("business_rules"))
        + _to_text_list(stage1.get("flows"))
        + _to_text_list(stage1.get("exceptions"))
    )
    merged = (text + " " + text2).lower()
    
    # 强制识别特定业务场景
    if "录音" in merged and ("ktv" in merged or "机顶盒" in merged or "盒子" in merged):
        return "ktv_recording_system"
        
    pack = _get_outline_keyword_pack()
    detect = pack.get("detect_system_type") if isinstance(pack.get("detect_system_type"), dict) else {}
    kw_priority = [str(x).lower() for x in _pack_list({"x": detect}, ["x", "priority"], ["优先级", "打断", "恢复", "抢占", ">"])]
    kw_display = [str(x).lower() for x in _pack_list({"x": detect}, ["x", "display"], ["展示", "播放", "投屏", "广告", "画面", "屏幕"])]
    kw_state = [str(x).lower() for x in _pack_list({"x": detect}, ["x", "state"], ["状态", "流转", "状态机", "进入", "退出"])]
    kw_biz = [str(x).lower() for x in _pack_list({"x": detect}, ["x", "business_flow"], ["订单", "支付", "下单", "审批", "工单"])]
    has_priority = any(k in merged for k in kw_priority)
    has_display = any(k in merged for k in kw_display)
    has_state = any(k in merged for k in kw_state)
    has_biz_flow = any(k in merged for k in kw_biz)
    if has_priority and has_display:
        return "scheduling_system"
    if has_state:
        return "state_machine"
    if has_biz_flow:
        return "business_flow"
    return "general"


def _extract_scheduling_rules(content: str, stage1: Dict[str, Any], keyword_pack: Dict[str, Any] = None) -> Dict[str, List[str]]:
    lines: List[str] = []
    lines.extend([x for x in str(content or "").splitlines() if str(x).strip()])
    lines.extend(_to_text_list(stage1.get("business_rules")))
    lines.extend(_to_text_list(stage1.get("flows")))
    lines = [_clean_title(x) for x in lines if _clean_title(x)]
    priorities: List[str] = []
    interrupts: List[str] = []
    resumes: List[str] = []
    actors: List[str] = []
    entry_exit_rules: List[str] = []
    mode_rules: List[str] = []
    scene_rules: List[str] = []
    constraint_rules: List[str] = []
    pack = keyword_pack if isinstance(keyword_pack, dict) else _get_outline_keyword_pack("scheduling_system", "")
    mode_terms = _pack_list(pack, ["scheduling_extract", "mode_terms"], ["模式", "横屏", "竖屏", "画中画", "壁画", "GOGO秀", "DJ模式"])
    scene_terms = _pack_list(pack, ["scheduling_extract", "scene_terms"], ["欢迎/欢送场景", "非指定模式", "场景", "大屏端"])
    constraint_terms = _pack_list(pack, ["scheduling_extract", "constraint_terms"], ["有且只能", "仅", "不可", "禁止", "必须"])
    actor_terms = _pack_list(pack, ["scheduling_extract", "actor_terms"], ["投屏", "游戏", "广告", "直播", "播放", "弹窗", "来电", "语音"])
    for ln in lines[:300]:
        norm_ln = _apply_business_lexicon(ln)
        if ">" in norm_ln:
            priorities.append(norm_ln)
        if any(k in norm_ln for k in ["进入", "退出", "默认", "返回"]):
            entry_exit_rules.append(norm_ln)
        if any(k in norm_ln for k in mode_terms):
            mode_rules.append(norm_ln)
        if any(k in norm_ln for k in scene_terms):
            scene_rules.append(norm_ln)
        if any(k in norm_ln for k in constraint_terms):
            constraint_rules.append(norm_ln)
        if "打断" in norm_ln or "抢占" in norm_ln:
            interrupts.append(norm_ln)
        if "恢复" in norm_ln or "继续" in norm_ln:
            resumes.append(norm_ln)
        for term in re.split(r"[>、，,;/；\s]+", norm_ln):
            t = _clean_title(term)
            if not t:
                continue
            if 1 < len(t) <= 10 and not re.search(r"\d", t):
                if any(k in t for k in ["打断", "恢复", "优先", "结束", "启动", "出现", "规则"]):
                    continue
                if any(k in t for k in actor_terms):
                    actors.append(t)
    priorities = list(dict.fromkeys(priorities))[:8]
    interrupts = list(dict.fromkeys(interrupts))[:8]
    resumes = list(dict.fromkeys(resumes))[:8]
    entry_exit_rules = list(dict.fromkeys(entry_exit_rules))[:8]
    mode_rules = list(dict.fromkeys(mode_rules))[:8]
    scene_rules = list(dict.fromkeys(scene_rules))[:8]
    constraint_rules = list(dict.fromkeys(constraint_rules))[:8]
    actors = list(dict.fromkeys(actors))[:10]
    return {
        "priority_rules": priorities,
        "interrupt_rules": interrupts,
        "resume_rules": resumes,
        "entry_exit_rules": entry_exit_rules,
        "mode_rules": mode_rules,
        "scene_rules": scene_rules,
        "constraint_rules": constraint_rules,
        "actors": actors,
    }


def _rule_bucket_of_text(text: str) -> str:
    t = _apply_business_lexicon(text)
    if any(k in t for k in ["有且只能", "仅", "不可", "禁止", "必须"]):
        return "constraint"
    if any(k in t for k in ["进入", "退出", "默认", "返回"]):
        return "entry_exit"
    if any(k in t for k in ["打断", "抢占"]):
        return "interrupt"
    if any(k in t for k in ["恢复", "继续"]):
        return "resume"
    if any(k in t for k in ["优先级", ">"]):
        return "priority"
    if any(k in t for k in ["模式", "横屏", "竖屏", "画中画", "壁画", "GOGO秀", "DJ模式"]):
        return "mode"
    if any(k in t for k in ["欢迎/欢送场景", "非指定模式", "场景", "大屏端"]):
        return "scene"
    return "general"


def _extract_atomic_rules(content: str, stage1: Dict[str, Any], model: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = []
    for raw in [x for x in str(content or "").splitlines() if str(x).strip()]:
        parts = re.split(r"[。；;]|(?<!\d)[,，](?!\d)", str(raw))
        for p in parts:
            t = _clean_title(p)
            if t:
                lines.append(t)
    lines.extend(_to_text_list(stage1.get("business_rules")))
    lines.extend(_to_text_list(stage1.get("flows")))
    if isinstance(model, dict):
        for key in ["priority_order", "entry_exit_rules", "interrupt_rules", "resume_rules", "mode_rules", "scene_rules", "constraint_rules"]:
            lines.extend([str(x) for x in (model.get(key) or []) if str(x).strip()])
    uniq = []
    seen = set()
    for ln in lines:
        t = _apply_business_lexicon(ln)
        if not t or t in seen or len(t) < 6:
            continue
        seen.add(t)
        uniq.append(t)
    actors = [str(x) for x in (model.get("actors") or [])] if isinstance(model, dict) else []
    out: List[Dict[str, Any]] = []
    for idx, ln in enumerate(uniq[:80], start=1):
        bucket = _rule_bucket_of_text(ln)
        if bucket == "general":
            continue
        actor = ""
        for a in actors:
            aa = _apply_business_lexicon(a)
            if aa and aa in ln:
                actor = aa
                break
        condition = ""
        action = ""
        if "：" in ln:
            left, right = ln.split("：", 1)
            condition = _clean_title(left)
            action = _clean_title(right)
        elif ":" in ln:
            left, right = ln.split(":", 1)
            condition = _clean_title(left)
            action = _clean_title(right)
        else:
            if "则" in ln:
                left, right = ln.split("则", 1)
                condition = _clean_title(left)
                action = _clean_title("则" + right)
            else:
                action = _clean_title(ln)
        constraint = ln if any(k in ln for k in ["有且只能", "仅", "不可", "禁止", "必须"]) else ""
        recovery = ln if any(k in ln for k in ["恢复", "继续"]) else ""
        exception = ln if any(k in ln for k in ["异常", "弱网", "超时", "失败"]) else ""
        signal_count = sum(1 for x in [condition, action, actor, constraint, recovery, exception] if x)
        confidence = round(min(0.98, 0.42 + signal_count * 0.1), 2)
        out.append({
            "id": f"ATOMIC_{idx:03d}",
            "bucket": bucket,
            "condition": condition or "【PRD未说明】",
            "actor": actor or "【PRD未说明】",
            "action": action or "【PRD未说明】",
            "constraint": constraint or "",
            "recovery": recovery or "",
            "exception": exception or "",
            "source_text": ln,
            "confidence": confidence,
        })
    return out[:40]


def _normalize_rule_for_compare(text: str) -> str:
    t = _apply_business_lexicon(text)
    if not t:
        return ""
    t = re.sub(r"^(优先级规则|进入/退出规则|打断规则|恢复规则|模式规则|场景规则|限制条件)[:：]\s*", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t).lower()
    return t


def _rule_similarity(a: str, b: str) -> float:
    aa = _normalize_rule_for_compare(a)
    bb = _normalize_rule_for_compare(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        short = min(len(aa), len(bb))
        long = max(len(aa), len(bb))
        if long > 0 and short / float(long) >= 0.72:
            return 0.9
    sa = set([aa[i:i + 2] for i in range(max(1, len(aa) - 1))]) if len(aa) > 1 else {aa}
    sb = set([bb[i:i + 2] for i in range(max(1, len(bb) - 1))]) if len(bb) > 1 else {bb}
    jaccard = (len(sa & sb) / float(len(sa | sb))) if (sa | sb) else 0.0
    seq = SequenceMatcher(None, aa, bb).ratio()
    return round((jaccard * 0.55 + seq * 0.45), 4)


def _semantic_dedup_rules(items: List[str], threshold: float = 0.83, limit: int = 20) -> List[str]:
    out: List[str] = []
    for it in items:
        t = _apply_business_lexicon(it)
        if not t:
            continue
        duplicated = False
        for kept in out:
            if _rule_similarity(t, kept) >= threshold:
                duplicated = True
                break
        if duplicated:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _format_structured_rule(atom: Dict[str, Any]) -> str:
    a = atom if isinstance(atom, dict) else {}
    bucket = str(a.get("bucket") or "")
    bucket_name = {
        "priority": "优先级规则",
        "entry_exit": "进入/退出规则",
        "interrupt": "打断规则",
        "resume": "恢复规则",
        "mode": "模式规则",
        "scene": "场景规则",
        "constraint": "限制条件",
    }.get(bucket, "规则")
    cond = str(a.get("condition") or "")
    actor = str(a.get("actor") or "")
    action = str(a.get("action") or "")
    cond_txt = cond if cond and cond != "【PRD未说明】" else "条件未说明"
    actor_txt = actor if actor and actor != "【PRD未说明】" else "对象未说明"
    action_txt = action if action and action != "【PRD未说明】" else "动作未说明"
    return f"{bucket_name}：触发={cond_txt}；对象={actor_txt}；结果={action_txt}"


def _default_module_gap_rule(actor: str) -> str:
    a = _apply_business_lexicon(actor)
    if "投屏" in a:
        return "【PRD未说明】投屏是否允许被再次打断"
    if "游戏" in a:
        return "【PRD未说明】游戏被打断后是否保留进度并可恢复"
    if "广告" in a:
        return "【PRD未说明】广告被打断后是否断点续播"
    return f"【PRD未说明】{a}模块的特有中断与恢复规则"


def _build_global_and_module_diff_rules(system_model: Dict[str, Any], atomic_rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    model = system_model if isinstance(system_model, dict) else {}
    atoms = atomic_rules if isinstance(atomic_rules, list) else []
    actors = _semantic_dedup_rules([str(x) for x in (model.get("actors") or []) if str(x).strip()], threshold=0.9, limit=12)
    if not actors:
        actors = ["投屏", "游戏", "广告"]
    module_rules: Dict[str, List[str]] = {a: [] for a in actors}
    actor_set = set(actors)
    def _valid_rule_text(text: str) -> bool:
        t = _apply_business_lexicon(text)
        if not t:
            return False
        if t in actor_set:
            return False
        if len(t) < 6 or len(t) > 96:
            return False
        if (t.count("。") + t.count("；") + t.count(";")) >= 2 and "触发=" not in t:
            return False
        if any(k in t for k in ["触发=", "结果=", "优先级", "打断", "恢复", "进入", "退出", "模式", "场景", "限制", ">"]):
            return True
        return False
    global_candidates: List[str] = []
    for atom in atoms:
        txt = _format_structured_rule(atom)
        actor = _apply_business_lexicon(str((atom or {}).get("actor") or ""))
        if _valid_rule_text(txt):
            if actor and actor in module_rules and actor != "【PRD未说明】":
                module_rules[actor].append(txt)
            else:
                global_candidates.append(txt)
    prefix_map = {
        "priority_order": "优先级规则：",
        "entry_exit_rules": "进入/退出规则：",
        "interrupt_rules": "打断规则：",
        "resume_rules": "恢复规则：",
        "mode_rules": "模式规则：",
        "scene_rules": "场景规则：",
        "constraint_rules": "限制条件：",
    }
    for key in ["priority_order", "entry_exit_rules", "interrupt_rules", "resume_rules", "mode_rules", "scene_rules", "constraint_rules"]:
        for r in [str(x) for x in (model.get(key) or []) if str(x).strip()]:
            rr = _apply_business_lexicon(r)
            if not _valid_rule_text(rr):
                continue
            if len(rr) <= 56:
                candidate = prefix_map.get(key, "规则：") + rr
                if any(_rule_similarity(candidate, old) >= 0.8 for old in global_candidates):
                    continue
                global_candidates.append(candidate)
    global_rules = _semantic_dedup_rules(global_candidates, threshold=0.84, limit=16)

    module_diff: List[Dict[str, Any]] = []
    for actor in actors[:8]:
        base = _semantic_dedup_rules(module_rules.get(actor) or [], threshold=0.84, limit=8)
        uniq: List[str] = []
        for r in base:
            if any(_rule_similarity(r, g) >= 0.84 for g in global_rules):
                continue
            uniq.append(r)
        uniq = _semantic_dedup_rules(uniq, threshold=0.86, limit=4)
        if not uniq:
            uniq = [_default_module_gap_rule(actor)]
        module_diff.append({"module": actor, "rules": uniq[:4]})
    return {"global_rules": global_rules[:12], "module_diff_rules": module_diff[:8]}


def _build_state_machine_model(system_model: Dict[str, Any], atomic_rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    model = system_model if isinstance(system_model, dict) else {}
    atoms = atomic_rules if isinstance(atomic_rules, list) else []
    actors = _semantic_dedup_rules([str(x) for x in (model.get("actors") or []) if str(x).strip()], threshold=0.9, limit=10)
    if not actors:
        actors = ["投屏", "游戏", "广告"]
    states = ["空闲"] + actors[:8]
    transitions: List[Dict[str, Any]] = []

    def add_transition(src: str, dst: str, trigger: str, action: str, source: str) -> None:
        s = _apply_business_lexicon(src)
        d = _apply_business_lexicon(dst)
        t = _apply_business_lexicon(trigger)
        a = _apply_business_lexicon(action)
        if not s or not d or not t or not a:
            return
        key = (s, d, _normalize_rule_for_compare(t), _normalize_rule_for_compare(a))
        for old in transitions:
            okey = (
                str(old.get("from") or ""),
                str(old.get("to") or ""),
                _normalize_rule_for_compare(str(old.get("trigger") or "")),
                _normalize_rule_for_compare(str(old.get("action") or "")),
            )
            if key == okey:
                return
        transitions.append({
            "from": s,
            "to": d,
            "trigger": t,
            "action": a,
            "source": source,
        })

    for atom in atoms:
        b = str((atom or {}).get("bucket") or "")
        actor = _apply_business_lexicon(str((atom or {}).get("actor") or ""))
        cond = str((atom or {}).get("condition") or "")
        act = str((atom or {}).get("action") or "")
        if actor and actor != "【PRD未说明】":
            if b == "entry_exit" and ("进入" in cond or "进入" in act):
                add_transition("空闲", actor, cond or "进入触发", act, "atomic")
            if b == "entry_exit" and ("退出" in cond or "退出" in act):
                add_transition(actor, "空闲", cond or "退出触发", act, "atomic")
            if b == "resume":
                add_transition("被打断态", actor, cond or "恢复触发", act, "atomic")
            if b == "interrupt":
                add_transition(actor, "被打断态", cond or "打断触发", act, "atomic")

    priority_items = [str(x) for x in (model.get("priority_order") or []) if str(x).strip()]
    priority_chain = []
    for txt in priority_items:
        t = _apply_business_lexicon(txt)
        if ">" in t:
            parts = [_clean_title(x) for x in t.split(">") if _clean_title(x)]
            if len(parts) >= 2:
                priority_chain = parts
                break
    if not priority_chain and len(priority_items) >= 2 and all(len(_clean_title(x)) <= 12 for x in priority_items):
        priority_chain = [_clean_title(x) for x in priority_items if _clean_title(x)]
    if len(priority_chain) >= 2:
        for i in range(len(priority_chain) - 1):
            high = priority_chain[i]
            low = priority_chain[i + 1]
            add_transition(low, high, f"{high}触发", f"{high}打断{low}", "priority")
            add_transition(high, low, f"{high}结束", f"恢复{low}", "priority")

    for x in [str(r) for r in (model.get("entry_exit_rules") or []) if str(r).strip()]:
        tx = _apply_business_lexicon(x)
        matched_actor = next((a for a in actors if a in tx), "")
        if not matched_actor:
            continue
        if "进入" in tx:
            add_transition("空闲", matched_actor, tx, f"进入{matched_actor}", "rule")
        if "退出" in tx:
            add_transition(matched_actor, "空闲", tx, f"退出{matched_actor}", "rule")

    evidence_actors = _semantic_dedup_rules([
        _apply_business_lexicon(str((a or {}).get("actor") or ""))
        for a in atoms if str((a or {}).get("actor") or "") and str((a or {}).get("actor") or "") != "【PRD未说明】"
    ], threshold=0.92, limit=8)
    fallback_actors = evidence_actors if len(evidence_actors) >= 1 else actors
    if not transitions and len(fallback_actors) >= 1:
        add_transition("空闲", fallback_actors[0], f"{fallback_actors[0]}触发", f"进入{fallback_actors[0]}", "fallback")
        add_transition(fallback_actors[0], "空闲", f"{fallback_actors[0]}结束", f"退出{fallback_actors[0]}", "fallback")

    if any("被打断态" == str(t.get("to") or "") or "被打断态" == str(t.get("from") or "") for t in transitions):
        states.append("被打断态")
    edge_states = set()
    for t in transitions:
        a = str(t.get("from") or "")
        b = str(t.get("to") or "")
        if a:
            edge_states.add(a)
        if b:
            edge_states.add(b)
    if edge_states:
        states = [s for s in states if s == "空闲" or s in edge_states]
    states = _semantic_dedup_rules(states, threshold=0.95, limit=12)
    state_id_map: Dict[str, str] = {}
    for i, s in enumerate(states, start=1):
        base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(s or "")).strip("_")
        if not base:
            base = f"S{i}"
        if base[0].isdigit():
            base = "S_" + base
        state_id_map[s] = base
    mermaid_lines = ["stateDiagram-v2"]
    if states:
        mermaid_lines.append(f"  [*] --> {state_id_map.get(states[0], 'S1')}")
    for s in states:
        sid = state_id_map.get(s, "")
        if sid:
            mermaid_lines.append(f"  state \"{s}\" as {sid}")
    for t in transitions[:24]:
        frm = state_id_map.get(str(t.get("from") or ""), "")
        to = state_id_map.get(str(t.get("to") or ""), "")
        trig = _apply_business_lexicon(str(t.get("trigger") or ""))
        if frm and to:
            mermaid_lines.append(f"  {frm} --> {to}: {trig or '触发'}")
    graph: Dict[str, List[str]] = {}
    indegree: Dict[str, int] = {s: 0 for s in states}
    for t in transitions[:24]:
        a = str(t.get("from") or "")
        b = str(t.get("to") or "")
        if not a or not b:
            continue
        graph.setdefault(a, []).append(b)
        indegree[b] = indegree.get(b, 0) + 1
        if a not in indegree:
            indegree[a] = indegree.get(a, 0)
    start_state = "空闲" if "空闲" in states else (states[0] if states else "")
    reachable = set()
    if start_state:
        stack = [start_state]
        while stack:
            cur = stack.pop()
            if cur in reachable:
                continue
            reachable.add(cur)
            for nxt in graph.get(cur, [])[:12]:
                if nxt not in reachable:
                    stack.append(nxt)
    unreachable = [s for s in states if s not in reachable]
    dead_end = [s for s in states if len(graph.get(s, [])) == 0 and s != start_state]
    cycles: List[List[str]] = []
    seen_cycle = set()
    for start in states[:12]:
        dfs_stack = [(start, [start])]
        while dfs_stack:
            node, path = dfs_stack.pop()
            for nxt in graph.get(node, [])[:8]:
                if nxt == start and len(path) >= 2:
                    cyc = path + [start]
                    key = tuple(cyc)
                    if key not in seen_cycle:
                        seen_cycle.add(key)
                        cycles.append(cyc)
                    continue
                if nxt not in path and len(path) < 8:
                    dfs_stack.append((nxt, path + [nxt]))
            if len(cycles) >= 6:
                break
        if len(cycles) >= 6:
            break
    risk_level = "low"
    if cycles or unreachable:
        risk_level = "high"
    elif dead_end:
        risk_level = "medium"
    graph_analysis = {
        "start_state": start_state,
        "unreachable_states": unreachable[:10],
        "dead_end_states": dead_end[:10],
        "cycles": cycles[:6],
        "risk_level": risk_level,
    }
    return {
        "states": states,
        "transitions": transitions[:24],
        "mermaid": "\n".join(mermaid_lines),
        "graph_analysis": graph_analysis,
        "summary": {
            "state_count": len(states),
            "transition_count": len(transitions[:24]),
            "cycle_count": len(cycles),
            "unreachable_count": len(unreachable),
            "dead_end_count": len(dead_end),
        },
    }


def _build_rule_diagnostics(system_model: Dict[str, Any], atomic_rules: List[Dict[str, Any]], rule_model: Dict[str, Any] = None) -> Dict[str, Any]:
    model = system_model if isinstance(system_model, dict) else {}
    rmodel = rule_model if isinstance(rule_model, dict) else {}
    atoms = atomic_rules if isinstance(atomic_rules, list) else []
    conflicts: List[Dict[str, Any]] = []
    closure_checks: List[Dict[str, Any]] = []

    priority_texts = [str(x) for x in (model.get("priority_order") or []) if str(x).strip()]
    if not priority_texts:
        priority_texts.extend([str(x) for x in (rmodel.get("priority_rules") or []) if str(x).strip()])
    if not priority_texts:
        chain = [str(x) for x in (rmodel.get("priority_chain") or []) if str(x).strip()]
        if len(chain) >= 2:
            priority_texts.append(" > ".join(chain))
    if not priority_texts:
        for a in atoms:
            if str(a.get("bucket") or "") == "priority":
                src = str(a.get("source_text") or "")
                if src:
                    priority_texts.append(src)
    edges = set()
    for txt in priority_texts:
        t = _apply_business_lexicon(txt)
        if ">" not in t:
            continue
        parts = [_clean_title(x) for x in t.split(">") if _clean_title(x)]
        for i in range(len(parts) - 1):
            a = parts[i]
            b = parts[i + 1]
            if a and b:
                edges.add((a, b))
    for a, b in list(edges):
        if (b, a) in edges and a != b:
            evidence = f"{a} > {b} 与 {b} > {a}"
            exists = any(c.get("evidence") == evidence for c in conflicts)
            if not exists:
                conflicts.append({
                    "type": "priority_conflict",
                    "severity": "high",
                    "message": "优先级存在互相压制冲突",
                    "evidence": evidence,
                })
    if edges:
        graph: Dict[str, List[str]] = {}
        for a, b in edges:
            graph.setdefault(a, []).append(b)
        cycle_path = []
        for start in list(graph.keys())[:12]:
            stack = [(start, [start])]
            visited_local = set()
            while stack:
                node, path = stack.pop()
                key = (node, tuple(path))
                if key in visited_local:
                    continue
                visited_local.add(key)
                for nxt in graph.get(node, [])[:6]:
                    if nxt == start and len(path) >= 2:
                        cycle_path = path + [start]
                        break
                    if nxt not in path and len(path) < 6:
                        stack.append((nxt, path + [nxt]))
                if cycle_path:
                    break
            if cycle_path:
                break
        if cycle_path:
            evidence = " > ".join(cycle_path)
            conflicts.append({
                "type": "priority_cycle",
                "severity": "high",
                "message": "优先级链存在循环依赖",
                "evidence": evidence,
            })

    mode_rules = [str(x) for x in (model.get("mode_rules") or []) if str(x).strip()]
    landscape = any("横屏" in x for x in mode_rules)
    portrait = any("竖屏" in x for x in mode_rules)
    exclusive = any(any(k in x for k in ["有且只能", "仅", "必须"]) for x in mode_rules)
    if landscape and portrait and exclusive:
        conflicts.append({
            "type": "mode_constraint_conflict",
            "severity": "medium",
            "message": "模式规则同时出现互斥方向与全局限制，可能冲突",
            "evidence": "模式规则包含横屏/竖屏与有且只能/仅/必须",
        })

    has_entry = any("进入" in str(x) for x in (model.get("entry_exit_rules") or []))
    has_exit = any("退出" in str(x) for x in (model.get("entry_exit_rules") or []))
    has_interrupt = len(model.get("interrupt_rules") or []) > 0
    has_resume = len(model.get("resume_rules") or []) > 0
    has_priority = len(model.get("priority_order") or []) > 0
    has_scene = len(model.get("scene_rules") or []) > 0
    has_constraint = len(model.get("constraint_rules") or []) > 0

    closure_checks.append({
        "name": "进入/退出闭环",
        "status": "pass" if (has_entry and has_exit) else "warn",
        "message": "进入与退出规则齐全" if (has_entry and has_exit) else "缺少进入或退出规则，闭环不完整",
    })
    closure_checks.append({
        "name": "打断/恢复闭环",
        "status": "pass" if (not has_interrupt or has_resume) else "warn",
        "message": "打断后具备恢复规则" if (not has_interrupt or has_resume) else "存在打断但无恢复规则",
    })
    closure_checks.append({
        "name": "优先级定义完整性",
        "status": "pass" if has_priority else "warn",
        "message": "已定义优先级顺序" if has_priority else "未定义优先级顺序",
    })
    closure_checks.append({
        "name": "场景限制一致性",
        "status": "pass" if (not has_scene or has_constraint) else "warn",
        "message": "场景规则与限制条件基本一致" if (not has_scene or has_constraint) else "存在场景规则但限制条件不足",
    })

    low_conf = [a for a in atoms if float(a.get("confidence") or 0) < 0.55]
    if low_conf:
        conflicts.append({
            "type": "low_confidence_rules",
            "severity": "low",
            "message": "存在低置信原子规则，建议人工复核",
            "evidence": "低置信规则数量：" + str(len(low_conf)),
        })

    summary = {
        "conflict_count": len(conflicts),
        "warn_count": sum(1 for c in closure_checks if c.get("status") == "warn"),
    }
    summary["health_level"] = "good" if summary["conflict_count"] == 0 and summary["warn_count"] == 0 else ("risk" if summary["conflict_count"] <= 1 else "high_risk")

    return {
        "summary": summary,
        "conflicts": conflicts[:12],
        "closure_checks": closure_checks,
    }


def _build_remediation_plan(rule_diagnostics: Dict[str, Any], atomic_rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    di = rule_diagnostics if isinstance(rule_diagnostics, dict) else {}
    conflicts = di.get("conflicts") if isinstance(di.get("conflicts"), list) else []
    checks = di.get("closure_checks") if isinstance(di.get("closure_checks"), list) else []
    atoms = atomic_rules if isinstance(atomic_rules, list) else []
    plan: List[Dict[str, Any]] = []

    def push(priority: str, target: str, action: str, expected_gain: str, score_gain: float, dimensions: List[str]) -> None:
        plan.append({
            "priority": priority,
            "target": target,
            "action": action,
            "expected_gain": expected_gain,
            "score_gain": round(float(score_gain), 2),
            "dimensions": dimensions[:4] if isinstance(dimensions, list) else [],
        })

    for c in conflicts:
        ctype = str(c.get("type") or "")
        if ctype in {"priority_conflict", "priority_cycle"}:
            push("P0", "优先级链", "重排优先级并去除循环依赖，形成唯一有向链", "冲突数-1；调度确定性提升", 0.8, ["规则明确度", "流程一致性"])
        elif ctype == "mode_constraint_conflict":
            push("P1", "模式规则", "拆分横屏/竖屏适用条件并补充例外分支", "模式冲突减少；场景命中率提升", 0.45, ["规则明确度", "异常覆盖度"])
        elif ctype == "low_confidence_rules":
            push("P2", "低置信规则", "补充原文触发条件和动作描述，提升规则可解析性", "低置信规则占比下降", 0.25, ["可测试性"])

    for ck in checks:
        if str(ck.get("status") or "") != "warn":
            continue
        name = str(ck.get("name") or "")
        if "进入/退出" in name:
            push("P0", "流程闭环", "补充进入前置条件与退出后去向，形成完整状态闭环", "闭环检查通过；遗漏风险下降", 0.65, ["流程一致性", "状态机完备度"])
        elif "打断/恢复" in name:
            push("P0", "恢复机制", "为每条打断规则补充恢复目标与恢复时机", "可恢复性提升；异常中断风险下降", 0.6, ["流程一致性", "异常覆盖度"])
        elif "优先级" in name:
            push("P1", "优先级定义", "补充统一优先级顺序并标注同级裁决策略", "优先级检查通过；冲突率下降", 0.5, ["规则明确度"])
        elif "场景限制" in name:
            push("P1", "场景限制", "为场景规则补充必须/禁止条件与例外说明", "场景一致性提升", 0.4, ["异常覆盖度", "可测试性"])

    if not plan and atoms:
        push("P2", "规则文案", "统一使用“条件：动作”句式并保留关键对象名称", "原子化置信度提升", 0.2, ["可测试性"])

    uniq = []
    seen = set()
    for p in plan:
        k = (p.get("priority"), p.get("target"), p.get("action"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    order = {"P0": 0, "P1": 1, "P2": 2}
    uniq.sort(key=lambda x: order.get(str(x.get("priority") or "P2"), 9))
    for i, p in enumerate(uniq, start=1):
        p["index"] = i
    total_gain = round(sum(float(x.get("score_gain") or 0.0) for x in uniq), 2)
    for p in uniq:
        p["projected_total_gain"] = total_gain
    return uniq[:10]


def _split_sentences(text: str) -> List[str]:
    raw = re.split(r"[\n\r]+|[。！？!?；;]+", str(text or ""))
    out = []
    for x in raw:
        t = _clean_title(x)
        if t and len(t) >= 6:
            out.append(t)
    return out[:400]


def _pick_sentences_by_keywords(sentences: List[str], keywords: List[str], limit: int = 6) -> List[str]:
    out = []
    for s in sentences:
        hit = sum(1 for k in keywords if k and k in s)
        if hit <= 0:
            continue
        if len(s) > 120:
            continue
        out.append((hit, s))
    out.sort(key=lambda x: (-x[0], len(x[1])))
    return _semantic_dedup_rules([x[1] for x in out], threshold=0.9, limit=limit)


def _extract_core_brief(content: str, stage1: Dict[str, Any], system_type: str, model: Dict[str, Any], keyword_pack: Dict[str, Any] = None) -> Dict[str, Any]:
    text = str(content or "")
    stage_blob = " ".join(
        _to_text_list(stage1.get("modules"))
        + _to_text_list(stage1.get("flows"))
        + _to_text_list(stage1.get("business_rules"))
        + _to_text_list(stage1.get("exceptions"))
    )
    merged = (text + "\n" + stage_blob).strip()
    sentences = _split_sentences(merged)
    pack = keyword_pack if isinstance(keyword_pack, dict) else _get_outline_keyword_pack(system_type, "")
    core_kw = pack.get("core_brief") if isinstance(pack.get("core_brief"), dict) else {}
    module_hints = [x for x in _to_text_list(stage1.get("modules")) if 1 < len(str(x)) <= 12][:8]
    role_keywords = _pack_list({"x": core_kw}, ["x", "roles"], ["角色", "参与方", "分工", "职责", "责任方", "合作方", "供应商", "用户", "商户", "平台", "甲方", "乙方", "运营", "研发", "测试", "财务", "客服", "管理员"]) + module_hints
    roles = _semantic_dedup_rules(_pick_sentences_by_keywords(sentences, role_keywords, limit=6), threshold=0.92, limit=6)
    payment = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "payment"], ["付费", "计费", "结算", "对账", "购买", "扣费", "分成", "账单", "订单", "成本"]), limit=6)
    flow = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "flow"], ["主流程", "核心流程", "流程", "步骤", "创建", "提交", "审批", "支付", "执行", "进入", "退出", "完成", "回滚"]), limit=8)
    integration = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "integration"], ["接口", "API", "token", "Token", "回调", "校验", "鉴权", "同步", "消息", "webhook"]), limit=6)
    exceptions = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "exceptions"], ["异常", "失败", "超时", "断网", "恢复", "告警", "兜底", "重试"]), limit=6)
    implementation = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "implementation"], ["试点", "推广", "培训", "上线", "排期", "阶段", "实施", "维护", "验收"]), limit=6)
    risks = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "risks"], ["风险", "应对", "隐患", "阻塞", "影响", "不可用"]), limit=6)
    confirms = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "confirms"], ["待确认", "需确认", "待定", "未确认", "未说明"]), limit=6)
    positioning = _pick_sentences_by_keywords(sentences, _pack_list({"x": core_kw}, ["x", "positioning"], ["定位", "目标", "方案", "用于", "接入", "商业化", "项目"]), limit=3)
    positioning = [x for x in positioning if not any(k in _clean_title(x).lower() for k in ["ui设计", "开发", "负责人", "测试"]) and not _is_outline_broken_text(x)]
    if not positioning:
        goal = next((x for x in _to_text_list(stage1.get("goal")) if not _is_outline_broken_text(x)), "")
        if goal:
            positioning = [_clean_title(goal)]
    if not positioning and merged:
        positioning = [_derive_outline_l0(stage1, model)]
    if not flow:
        flow = _semantic_dedup_rules(_to_text_list(stage1.get("flows")), threshold=0.9, limit=6)
    flow = [x for x in flow if not _is_outline_global_rule(x) and not re.search(r"(清空|此处规则需确认|转台不清空)", _clean_title(x))]
    role_candidates = _pick_outline_roles(stage1, roles)
    roles = role_candidates[:6]
    coverage_items = {
        "定位": positioning,
        "角色": roles,
        "付费": payment,
        "流程": flow,
        "接口": integration,
        "异常": exceptions,
        "实施": implementation,
        "风险": risks,
    }
    hit = sum(1 for _, v in coverage_items.items() if v)
    total = len(coverage_items)
    ratio = round(hit / float(total), 3) if total else 0.0
    level = "low"
    if ratio >= 0.75:
        level = "high"
    elif ratio >= 0.45:
        level = "medium"
    return {
        "system_type": system_type,
        "positioning": positioning[:2],
        "roles": roles[:6],
        "payment_model": payment[:6],
        "core_process": flow[:8],
        "integration": integration[:6],
        "exceptions": exceptions[:6],
        "implementation": implementation[:6],
        "risks": risks[:6],
        "todo_confirm": confirms[:6],
        "coverage": {
            "hit": hit,
            "total": total,
            "ratio": ratio,
            "level": level,
        },
        "source_hint": "local_deterministic",
    }


def _build_system_model(system_type: str, stage1: Dict[str, Any], extracted_rules: Dict[str, List[str]]) -> Dict[str, Any]:
    modules = [m for m in _to_text_list(stage1.get("modules")) if m and m != "【PRD未说明】"][:12]
    flows = [f for f in _to_text_list(stage1.get("flows")) if f and f != "【PRD未说明】"][:12]
    actors = extracted_rules.get("actors") or []
    if not actors:
        actors = modules[:]
    if system_type == "scheduling_system":
        return {
            "resource": "screen",
            "actors": actors[:10],
            "priority_order": extracted_rules.get("priority_rules") or [],
            "interrupt_rules": extracted_rules.get("interrupt_rules") or [],
            "resume_rules": extracted_rules.get("resume_rules") or [],
            "entry_exit_rules": extracted_rules.get("entry_exit_rules") or [],
            "mode_rules": extracted_rules.get("mode_rules") or [],
            "scene_rules": extracted_rules.get("scene_rules") or [],
            "constraint_rules": extracted_rules.get("constraint_rules") or [],
        }
    if system_type == "state_machine":
        states = [s for s in _to_text_list(stage1.get("states")) if s and s != "【PRD未说明】"][:12]
        return {
            "states": states,
            "core_flows": flows[:6],
            "rules": _to_text_list(stage1.get("business_rules"))[:8],
        }
    return {
        "modules": modules[:10],
        "core_flows": flows[:6],
        "rules": _to_text_list(stage1.get("business_rules"))[:8],
    }


def _build_cognitive_outline(system_type: str, model: Dict[str, Any], outline: List[Dict[str, Any]]) -> Dict[str, Any]:
    if system_type == "scheduling_system":
        actors = model.get("actors") if isinstance(model.get("actors"), list) else []
        actor_txt = "、".join([str(x) for x in actors[:6]]) if actors else "多个功能"
        l0 = f"本PRD用于定义屏幕资源在{actor_txt}竞争时的调度规则，确保展示结果稳定且可恢复。"
        l1 = [
            "触发功能后检测当前资源占用状态",
            "根据优先级判断是否切换当前展示",
            "高优先级触发时打断低优先级功能",
            "高优先级结束后恢复被打断功能",
            "异常或中断场景执行重试/回退策略",
        ]
        l2 = []
        l2.append("优先级裁决规则")
        if model.get("entry_exit_rules"):
            l2.append("进入/退出流程规则")
        if model.get("interrupt_rules") or model.get("resume_rules"):
            l2.append("打断与恢复规则")
        if model.get("mode_rules"):
            l2.append("模式切换与展示规则")
        if model.get("constraint_rules") or model.get("scene_rules"):
            l2.append("场景与限制条件")
        if not l2:
            l2 = ["优先级裁决规则", "打断与恢复规则", "场景与限制条件"]
        l3_seed = []
        for x in (model.get("priority_order") or [])[:3]:
            t = _apply_business_lexicon(x)
            if t:
                l3_seed.append("优先级规则：" + t if "优先级" not in t else t)
        for x in (model.get("entry_exit_rules") or [])[:2]:
            t = _apply_business_lexicon(x)
            if t:
                l3_seed.append("进入/退出规则：" + t if not any(k in t for k in ["进入", "退出", "默认"]) else t)
        for x in (model.get("interrupt_rules") or [])[:2]:
            t = _apply_business_lexicon(x)
            if t:
                l3_seed.append("打断规则：" + t if "打断" not in t else t)
        for x in (model.get("resume_rules") or [])[:2]:
            t = _apply_business_lexicon(x)
            if t:
                l3_seed.append("恢复规则：" + t if "恢复" not in t else t)
        for x in (model.get("mode_rules") or [])[:2]:
            t = _apply_business_lexicon(x)
            if t:
                l3_seed.append("模式规则：" + t if "模式" not in t else t)
        for x in (model.get("constraint_rules") or [])[:2]:
            t = _apply_business_lexicon(x)
            if t:
                l3_seed.append("限制条件：" + t if not any(k in t for k in ["不可", "有且只能", "仅", "禁止", "必须"]) else t)
        l3_model = [str(x) for x in (model.get("global_rules") or []) if str(x).strip()]
        l3 = _semantic_dedup_rules(l3_model or l3_seed, threshold=0.84, limit=10)
        l4 = []
        for item in (model.get("module_diff_rules") or [])[:8]:
            if not isinstance(item, dict):
                continue
            module = _apply_business_lexicon(str(item.get("module") or ""))
            rules = _semantic_dedup_rules([str(x) for x in (item.get("rules") or []) if str(x).strip()], threshold=0.86, limit=2)
            if not module:
                continue
            if rules:
                l4.append(module + "：" + "；".join(rules))
            else:
                l4.append(module + "：" + _default_module_gap_rule(module))
        return {"L0": l0, "L1": l1, "L2": l2, "L3": l3, "L4": l4}

    if system_type == "state_machine":
        states = model.get("states") if isinstance(model.get("states"), list) else []
        stage1_rules = [x for x in _to_text_list(stage1.get("business_rules")) if not _is_outline_broken_text(x)]
        stage1_flows = [x for x in _to_text_list(stage1.get("flows")) if not _is_outline_broken_text(x)]
        raw_rules = [str(x) for x in (model.get("rules") or []) if not _is_outline_broken_text(x)]
        l0 = _derive_outline_l0(stage1, model)
        l1 = []
        for line in (stage1_flows + stage1_rules)[:16]:
            t = _clean_title(line)
            if not t or _is_outline_global_rule(t):
                continue
            l1.append(t)
        l1 = _semantic_dedup_rules(l1, threshold=0.9, limit=5) or [
            "先确认主流程如何触发和进入目标状态",
            "再确认录制/展示/保存/上传各阶段的关键规则",
            "最后补齐异常、中断和恢复策略",
        ]
        l2 = states[:6] if states else ["状态定义", "转移条件", "异常处理", "回退机制"]
        l3 = _semantic_dedup_rules([x for x in raw_rules + stage1_rules if not _is_outline_global_rule(x)], threshold=0.88, limit=10)
        if not l3:
            l3 = _semantic_dedup_rules(stage1_rules, threshold=0.88, limit=8)
        l4: List[str] = []
        for state in l2:
            state_rules = _pick_state_specific_rules(state, raw_rules + stage1_rules, stage1_flows)
            if state_rules:
                l4.append(state + "：" + "；".join(state_rules[:2]))
        return {"L0": l0, "L1": l1, "L2": l2, "L3": l3, "L4": l4}

    first_title = "核心业务流程"
    if outline and isinstance(outline[0], dict):
        first_title = str(outline[0].get("title") or first_title)
    l0 = "本PRD用于描述核心业务流程与规则边界，目标是保证需求可实现且可测试。"
    l1 = [
        f"先理解{first_title}的主流程闭环",
        "再确认关键业务规则与口径约束",
        "最后补齐异常、边界与恢复策略",
    ]
    l2 = [str((it or {}).get("title") or "") for it in outline[:6] if isinstance(it, dict)]
    l3 = [str(x) for x in model.get("rules", [])[:8]] if isinstance(model, dict) else []
    return {"L0": l0, "L1": l1, "L2": l2, "L3": l3}


def _outline_unclear(outline: List[Dict[str, Any]]) -> bool:
    if not outline:
        return True
    total = len(outline)
    empty_children = sum(1 for it in outline if not (it.get("children") or []))
    noisy = sum(
        1 for it in outline
        if _is_bad_outline_title(str(it.get("title") or ""))
        or len(str(it.get("title") or "")) > 18
        or any(sym in str(it.get("title") or "") for sym in ["：", ":", "，", ","])
    )
    return empty_children >= max(1, int(total * 0.5)) or noisy >= max(1, int(total * 0.35))


def _build_semantic_outline_from_model(
    system_type: str,
    system_model: Dict[str, Any],
    cognitive_outline: Dict[str, Any],
) -> List[Dict[str, Any]]:
    def _infer_business_prefix() -> str:
        # 仅当 PRD 正文/模型中**明确出现**「星耀屏」时使用该前缀。
        # 旧逻辑用「屏」或 screen 泛匹配，会把「大屏/触摸屏/屏幕」等无关需求也标成星耀屏。
        resource = str(system_model.get("resource") or "")
        actors_text = " ".join([str(x) for x in (system_model.get("actors") or [])])
        blob = resource + " " + actors_text
        if "星耀屏" in blob:
            return "星耀屏"
        return ""

    def _business_title_for_actor(actor_name: str) -> str:
        a = _apply_business_lexicon(actor_name)
        if "投屏" in a:
            return "投屏进入与占用规则"
        if "广告" in a:
            return "广告抢占与展示规则"
        if any(k in a for k in ["画中画", "壁画", "gogo", "dj", "模式"]):
            return "特殊模式与兼容规则"
        if any(k in a for k in ["语音", "数字人", "互动"]):
            return "互动功能与展示联动规则"
        if "游戏" in a:
            return "游戏模式切换规则"
        return f"{a}展示联动规则" if a else "展示联动规则"

    if system_type == "scheduling_system":
        prefix = _infer_business_prefix()
        actors = [str(x).strip() for x in (system_model.get("actors") or []) if str(x).strip()]
        if not actors:
            actors = ["投屏", "游戏", "广告"]
        module_diff = system_model.get("module_diff_rules") if isinstance(system_model.get("module_diff_rules"), list) else []
        module_map: Dict[str, List[str]] = {}
        for item in module_diff:
            if not isinstance(item, dict):
                continue
            m = _apply_business_lexicon(str(item.get("module") or ""))
            rs = _semantic_dedup_rules([str(x) for x in (item.get("rules") or []) if str(x).strip()], threshold=0.86, limit=4)
            if m:
                module_map[m] = rs
        out: List[Dict[str, Any]] = []
        used_titles = set()
        for actor in actors[:8]:
            raw = _apply_business_lexicon(actor)
            if not raw:
                continue
            title = _apply_business_lexicon(_business_title_for_actor(raw))
            if prefix and not title.startswith(prefix):
                title = prefix + "·" + title
            if title in used_titles:
                continue
            used_titles.add(title)
            children = _semantic_dedup_rules(module_map.get(raw) or [], threshold=0.86, limit=4)
            if not children:
                children = [_default_module_gap_rule(raw)]
            out.append({"index": len(out) + 1, "title": title, "children": children[:4]})
            if len(out) >= 4:
                break
        if len(out) < 3:
            default_titles = ["投屏进入与占用规则", "广告抢占与展示规则", "特殊模式与兼容规则", "退出恢复与异常兜底规则"]
            for t in default_titles:
                if prefix and not t.startswith(prefix):
                    t = prefix + "·" + t
                if t in used_titles:
                    continue
                children = ["【PRD未说明】该模块的特有规则尚未明确"]
                out.append({"index": len(out) + 1, "title": _apply_business_lexicon(t), "children": children})
                if len(out) >= 4:
                    break
        if out:
            return out

    l2 = [str(x).strip() for x in (cognitive_outline.get("L2") or []) if str(x).strip()]
    l3 = [str(x).strip() for x in (cognitive_outline.get("L3") or []) if str(x).strip()]
    l1 = [str(x).strip() for x in (cognitive_outline.get("L1") or []) if str(x).strip()]
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(l2[:6], start=1):
        title = _clean_title(item)
        if not title or _is_bad_outline_title(title):
            continue
        children = [x for x in l3[:2] if x]
        if len(children) < 2:
            children.extend(l1[:2])
        out.append({"index": idx, "title": title, "children": list(dict.fromkeys([_clean_title(x) for x in children if _clean_title(x)]))[:4]})
    return out


def run_outline_engine(content: str, stage1_output: Dict[str, Any], stage2_output: Dict[str, Any] = None) -> Dict[str, Any]:
    stage1 = stage1_output if isinstance(stage1_output, dict) else {}
    blocks = stage1.get("blocks") if isinstance(stage1.get("blocks"), list) else []
    nodes = _build_nodes_from_blocks(blocks)

    mode = "structured"
    if _need_fallback_from_structured(nodes):
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", str(content or "")) if p.strip()]
        has_heading = sum(1 for p in paragraphs[:30] if _detect_heading_style(p)) >= 2
        if has_heading:
            mode = "heading"
            nodes = []
            for p in paragraphs[:60]:
                t = _clean_title(p)
                if not t:
                    continue
                if t in GENERIC_TITLES:
                    continue
                level = 2 if _detect_heading_style(p) and "." in p else 1
                nodes.append({"level": level, "title": t[:80]})
        else:
            mode = "semantic"
            nodes = _build_nodes_semantic(paragraphs)

    nodes = _dedup_nodes(nodes)[:80]
    quality = _score_quality(nodes, stage1)

    outline = []
    idx = 0
    current = None
    for n in nodes:
        lv = int(n.get("level") or 1)
        t = str(n.get("title") or "").strip()
        if not t:
            continue
        if lv <= 1:
            idx += 1
            current = {"index": idx, "title": t, "children": []}
            outline.append(current)
        else:
            if current is None:
                idx += 1
                current = {"index": idx, "title": "核心功能", "children": []}
                outline.append(current)
            current["children"].append(t)

    feature_modules = _to_text_list(stage1.get("modules"))
    if len(feature_modules) < 2:
        feature_modules.extend(_infer_modules_from_content(content))
    feature_modules = list(dict.fromkeys([_clean_title(x) for x in feature_modules if _clean_title(x)]))[:12]
    flows = _to_text_list(stage1.get("flows"))
    rules = _to_text_list(stage1.get("business_rules"))
    exceptions = _to_text_list(stage1.get("exceptions"))
    non_functionals = _to_text_list(stage1.get("non_functional"))

    child_candidates = []
    child_candidates.extend(flows)
    child_candidates.extend(rules)
    child_candidates.extend(exceptions)
    child_candidates.extend(non_functionals)
    if len(child_candidates) < 3:
        child_candidates.extend(_extract_clause_candidates(content))
    cleaned_candidates = []
    seen_candidates = set()
    for x in child_candidates:
        t = _clean_title(x)
        if not t or t in GENERIC_TITLES or t in seen_candidates:
            continue
        seen_candidates.add(t)
        cleaned_candidates.append(t)
    child_candidates = cleaned_candidates[:60]

    outline = _post_clean_outline(outline)

    if len(feature_modules) >= 2:
        weak_children = sum(1 for it in outline if not (it.get("children") or []))
        generic_titles = {"功能模块", "核心功能"} | GENERIC_TITLES
        generic_count = sum(1 for it in outline if str(it.get("title") or "").strip() in generic_titles)
        bad_title_count = sum(1 for it in outline if _is_bad_outline_title(str(it.get("title") or "")))
        noisy_title_count = sum(
            1 for it in outline
            if any(sym in str(it.get("title") or "") for sym in ["：", ":", "，", ",", "；", ";"])
            or len(str(it.get("title") or "")) > 18
        )
        if (not outline) or weak_children >= max(1, int(len(outline) * 0.6)) or generic_count >= max(1, int(len(outline) * 0.5)) or bad_title_count >= max(1, int(len(outline) * 0.4)) or noisy_title_count >= max(1, int(len(outline) * 0.4)):
            outline = _build_outline_by_modules(feature_modules, child_candidates)

    has_children = any((it.get("children") or []) for it in outline)
    if outline and not has_children:
        extra_children = list(dict.fromkeys(feature_modules[:4] + child_candidates[:8]))
        if extra_children:
            outline[0]["children"] = extra_children[:8]
    outline = _enrich_outline_children(outline, child_candidates)

    system_type = _detect_system_type(content, stage1)
    base_keyword_pack = _get_outline_keyword_pack(system_type, "")
    extracted_rules = _extract_scheduling_rules(content, stage1, keyword_pack=base_keyword_pack)
    system_model = _build_system_model(system_type, stage1, extracted_rules)
    if system_type == "scheduling_system" and isinstance(system_model, dict):
        system_model["atomic_rules"] = _extract_atomic_rules(content, stage1, system_model)
    cognitive_outline = _build_cognitive_outline(system_type, system_model, outline)

    classifier_score = {}
    classifier_confidence = 0.0
    rule_model = extracted_rules
    if _CoreOutlineEngine is not None:
        try:
            enhanced = _CoreOutlineEngine().generate(
                text=str(content or ""),
                stage1=stage1 if isinstance(stage1, dict) else {},
                stage2=stage2_output if isinstance(stage2_output, dict) else {},
            )
            if isinstance(enhanced, dict):
                system_type = str(enhanced.get("system_type") or system_type)
                system_model = enhanced.get("system_model") if isinstance(enhanced.get("system_model"), dict) else system_model
                cognitive_outline = enhanced.get("cognitive_outline") if isinstance(enhanced.get("cognitive_outline"), dict) else cognitive_outline
                classifier_score = enhanced.get("classifier_score") if isinstance(enhanced.get("classifier_score"), dict) else {}
                classifier_confidence = float(enhanced.get("classifier_confidence") or 0.0)
                rule_model = enhanced.get("rule_model") if isinstance(enhanced.get("rule_model"), dict) else rule_model
        except Exception:
            pass

    keyword_plugin = resolve_rule_plugin(content, stage1 if isinstance(stage1, dict) else {}, system_type)
    keyword_pack = _get_outline_keyword_pack(system_type, str(keyword_plugin.get("plugin_id") or ""))
    explicit_outline = generate_explicit_outline(content, stage1 if isinstance(stage1, dict) else {})
    if isinstance(system_model, dict):
        system_model["core_brief"] = _extract_core_brief(content, stage1, system_type, system_model, keyword_pack=keyword_pack)
        system_model["explicit_outline"] = explicit_outline if isinstance(explicit_outline, dict) else {}

    if system_type == "scheduling_system" and isinstance(system_model, dict):
        if not isinstance(system_model.get("atomic_rules"), list) or not system_model.get("atomic_rules"):
            system_model["atomic_rules"] = _extract_atomic_rules(content, stage1, system_model)
        diff_bundle = _build_global_and_module_diff_rules(system_model, system_model.get("atomic_rules") or [])
        system_model["global_rules"] = diff_bundle.get("global_rules") or []
        system_model["module_diff_rules"] = diff_bundle.get("module_diff_rules") or []
        system_model["state_machine"] = _build_state_machine_model(system_model, system_model.get("atomic_rules") or [])
        if not isinstance(cognitive_outline, dict):
            cognitive_outline = {}
        if system_model.get("global_rules"):
            cognitive_outline["L3"] = _semantic_dedup_rules([str(x) for x in (system_model.get("global_rules") or []) if str(x).strip()], threshold=0.84, limit=10)
        l4_lines = []
        for item in (system_model.get("module_diff_rules") or [])[:8]:
            if not isinstance(item, dict):
                continue
            module = _apply_business_lexicon(str(item.get("module") or ""))
            rules4 = _semantic_dedup_rules([str(x) for x in (item.get("rules") or []) if str(x).strip()], threshold=0.86, limit=2)
            if module:
                l4_lines.append(module + "：" + "；".join(rules4 if rules4 else [_default_module_gap_rule(module)]))
        cognitive_outline["L4"] = l4_lines
    rule_diagnostics = _build_rule_diagnostics(
        system_model if isinstance(system_model, dict) else {},
        system_model.get("atomic_rules") if isinstance(system_model, dict) else [],
        rule_model if isinstance(rule_model, dict) else {},
    ) if system_type == "scheduling_system" else {"summary": {"conflict_count": 0, "warn_count": 0, "health_level": "good"}, "conflicts": [], "closure_checks": []}
    remediation_plan = _build_remediation_plan(
        rule_diagnostics if isinstance(rule_diagnostics, dict) else {},
        system_model.get("atomic_rules") if isinstance(system_model, dict) else [],
    ) if system_type == "scheduling_system" else []
    state_machine = system_model.get("state_machine") if isinstance(system_model, dict) else {}
    deterministic_rules = {}
    explainable_report = {}
    strategy_report = {}
    rule_plugin = keyword_plugin if isinstance(keyword_plugin, dict) else {}
    prompt_profile = {}
    prompt_evaluation = {}
    if system_type == "scheduling_system":
        engine_model = build_rule_engine_model(
            system_model if isinstance(system_model, dict) else {},
            state_machine if isinstance(state_machine, dict) else {},
            system_model.get("atomic_rules") if isinstance(system_model, dict) else [],
            rule_model if isinstance(rule_model, dict) else {},
        )
        if not isinstance(rule_plugin, dict) or not rule_plugin:
            rule_plugin = resolve_rule_plugin(content, stage1 if isinstance(stage1, dict) else {}, system_type)
        deterministic_rules = run_rules(engine_model, enabled=rule_plugin.get("enabled_map") if isinstance(rule_plugin, dict) else {})
        if isinstance(rule_plugin, dict):
            append_plugin_usage({
                "plugin_id": str(rule_plugin.get("plugin_id") or ""),
                "name": str(rule_plugin.get("name") or ""),
                "match_score": int(rule_plugin.get("match_score") or 0),
                "enabled_rule_count": int(rule_plugin.get("enabled_rule_count") or 0),
                "system_type": system_type,
            })
        if isinstance(deterministic_rules, dict) and isinstance(rule_plugin, dict):
            deterministic_rules["plugin"] = {
                "plugin_id": rule_plugin.get("plugin_id"),
                "name": rule_plugin.get("name"),
                "description": rule_plugin.get("description"),
                "keyword_pack": rule_plugin.get("keyword_pack"),
                "match_score": rule_plugin.get("match_score"),
                "matched_terms": rule_plugin.get("matched_terms"),
                "enabled_rule_count": rule_plugin.get("enabled_rule_count"),
                "total_rule_count": rule_plugin.get("total_rule_count"),
            }
        if isinstance(rule_diagnostics, dict) and isinstance(deterministic_rules, dict):
            summary = rule_diagnostics.get("summary") if isinstance(rule_diagnostics.get("summary"), dict) else {}
            summary["rule_engine_score"] = deterministic_rules.get("score", 0)
            summary["rule_engine_failed"] = len(deterministic_rules.get("defects") or [])
            rule_diagnostics["summary"] = summary
        explainable_report = build_explainable_report(
            rule_diagnostics if isinstance(rule_diagnostics, dict) else {},
            deterministic_rules if isinstance(deterministic_rules, dict) else {},
            state_machine if isinstance(state_machine, dict) else {},
        )
        strategy_report = build_strategy_report(
            deterministic_rules if isinstance(deterministic_rules, dict) else {},
            explainable_report if isinstance(explainable_report, dict) else {},
            state_machine if isinstance(state_machine, dict) else {},
        )
        prompt_profile = select_prompt_profile(system_type, str(rule_plugin.get("plugin_id") or "").strip() if isinstance(rule_plugin, dict) else "")
        prompt_evaluation = evaluate_prompt_outcome(
            {
                "deterministic_rules": deterministic_rules,
                "explainable_report": explainable_report,
                "strategy_report": strategy_report,
                "state_machine": state_machine,
            },
            prompt_profile,
        )
        append_prompt_evaluation({
            "profile_id": str(prompt_evaluation.get("profile_id") or ""),
            "variant": str(prompt_evaluation.get("variant") or "A"),
            "quality_estimate": float(prompt_evaluation.get("quality_estimate") or 0),
            "passed": bool(prompt_evaluation.get("passed")),
            "system_type": system_type,
            "plugin_id": str(rule_plugin.get("plugin_id") or "") if isinstance(rule_plugin, dict) else "",
        })

    if _outline_unclear(outline):
        rebuilt = _build_semantic_outline_from_model(system_type, system_model if isinstance(system_model, dict) else {}, cognitive_outline if isinstance(cognitive_outline, dict) else {})
        if rebuilt:
            outline = rebuilt

    return {
        "mode": mode,
        "nodes": nodes,
        "outline": outline,
        "quality_score": quality,
        "feature_modules": feature_modules,
        "flows": flows,
        "rules": rules,
        "system_type": system_type,
        "system_model": system_model,
        "cognitive_outline": cognitive_outline,
        "classifier_score": classifier_score,
        "classifier_confidence": classifier_confidence,
        "rule_model": rule_model if isinstance(rule_model, dict) else {},
        "atomic_rules": system_model.get("atomic_rules") if isinstance(system_model, dict) else [],
        "core_brief": system_model.get("core_brief") if isinstance(system_model, dict) and isinstance(system_model.get("core_brief"), dict) else {},
        "explicit_outline": explicit_outline if isinstance(explicit_outline, dict) else {},
        "rule_diagnostics": rule_diagnostics,
        "remediation_plan": remediation_plan,
        "state_machine": state_machine if isinstance(state_machine, dict) else {},
        "deterministic_rules": deterministic_rules if isinstance(deterministic_rules, dict) else {},
        "rule_plugin": rule_plugin if isinstance(rule_plugin, dict) else {},
        "prompt_profile": prompt_profile if isinstance(prompt_profile, dict) else {},
        "prompt_evaluation": prompt_evaluation if isinstance(prompt_evaluation, dict) else {},
        "explainable_report": explainable_report if isinstance(explainable_report, dict) else {},
        "strategy_report": strategy_report if isinstance(strategy_report, dict) else {},
    }
