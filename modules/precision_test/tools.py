"""精准回归 Agent 的只读工具集（ReAct 用）。"""
import os
import re
import json
import logging

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_HERE, "codegraph_config.json")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", ".workbuddy", ".idea", ".vscode", "venv", "env"}
_MAX_SCAN_FILES = 5000
_MAX_MATCHES = 20
_SCAN_EXTS = (".py", ".go", ".java", ".ts", ".js", ".kt", ".cpp", ".c", ".h")


def _cg_config():
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("repo_path", "")
    cfg.setdefault("test_glob", "")
    cfg["enabled"] = bool(cfg.get("enabled")) and bool(cfg.get("repo_path"))
    return cfg


def _repo_path():
    return (_cg_config().get("repo_path") or "").strip()


def search_code_symbol(name, repo=None):
    repo = (repo or _repo_path() or "").strip()
    name = (name or "").strip()
    if not repo or not os.path.isdir(repo):
        return {"available": False, "reason": "未配置或不存在的代码仓库路径", "matches": []}
    if not name or len(name) > 120 or not re.match(r"^[\w.\-]+$", name):
        return {"available": True, "reason": "无效的符号名", "matches": []}
    pattern = re.compile(
        r"^\s*(?:async\s+)?def\s+" + re.escape(name) + r"\s*\(|^\s*class\s+" + re.escape(name) + r"\s*[\(\:]"
    )
    matches = []
    scanned = 0
    try:
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in files:
                if not f.endswith(_SCAN_EXTS):
                    continue
                scanned += 1
                if scanned > _MAX_SCAN_FILES:
                    break
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh, 1):
                            if pattern.match(line):
                                matches.append({
                                    "file": os.path.relpath(path, repo),
                                    "line": i,
                                    "snippet": line.strip()[:200],
                                })
                                if len(matches) >= _MAX_MATCHES:
                                    break
                except Exception:
                    continue
                if len(matches) >= _MAX_MATCHES:
                    break
            if len(matches) >= _MAX_MATCHES:
                break
    except Exception as exc:
        return {"available": False, "reason": f"扫描异常: {exc}", "matches": []}
    return {"available": True, "matches": matches, "count": len(matches), "scanned_files": scanned}


def search_code_impact(diff=None, symbol=None):
    cfg = _cg_config()
    if not cfg.get("enabled"):
        return {"available": False, "reason": "CodeGraph 未启用或未配置 repo_path", "impacted": []}
    try:
        from .codegraph_client import analyze_diff_impact
        result = analyze_diff_impact(
            cfg["repo_path"], diff or "", cfg.get("test_glob") or None
        )
        return {"available": True, "impacted": result}
    except Exception as exc:
        logger.warning("search_code_impact 失败: %s", exc)
        return {"available": False, "reason": f"CodeGraph 调用失败: {exc}", "impacted": []}


def dispatch_tool_call(name, arguments):
    arguments = arguments or {}
    try:
        if name == "search_code_symbol":
            return search_code_symbol(arguments.get("name"), arguments.get("repo"))
        if name == "search_code_impact":
            return search_code_impact(arguments.get("diff"), arguments.get("symbol"))
    except Exception as exc:
        return {"available": False, "reason": f"工具执行异常: {exc}"}
    return {"available": False, "reason": f"未知工具: {name}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_code_symbol",
            "description": (
                "在目标代码仓库中查找函数或类的定义位置（相对路径、行号、签名片段）。"
                "用于核实某次改动是否真实涉及某个符号，或定位实现入口。仅读取源码，不修改任何文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要查找的函数或类名"},
                    "repo": {"type": "string", "description": "可选，仓库绝对路径；缺省用平台配置的目标仓库"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code_impact",
            "description": (
                "基于 CodeGraph 计算某段代码改动的影响面（受影响的函数与测试）。"
                "用于判断改动波及范围。需要平台已启用 CodeGraph，否则返回不可用。仅读取分析结果，不修改任何文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "diff": {"type": "string", "description": "代码 diff 文本"},
                    "symbol": {"type": "string", "description": "可选，聚焦的符号名"},
                },
                "required": [],
            },
        },
    },
]
