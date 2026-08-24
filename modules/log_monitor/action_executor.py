# -*- coding: utf-8 -*-
"""告警动作执行器（#29）：只读动作真实落地，custom_shell 自由命令不放开。

安全边界（与"只增强诊断与闭环、不自动修改代码"约束对齐）：
- screenshot：adb screencap 截图落盘（纯只读取证，产物挂证据链）
- shell:<cmd>：仅允许白名单只读命令（dumpsys/logcat/cat/ps/top/getprop 等），
  且必须通过 DANGEROUS_TOKENS 黑名单护栏（与 selfheal 探针同款）
- custom_shell / 其他自由命令：一律拒绝执行，记录拒绝原因（可审计）

产出统一为结构化 dict（type/status/summary/artifact/output/executed_at），
由挂载方（views）序列化写入 AlertRecord.action_taken，并注入自愈证据链。
"""

import json
import logging
import os
import shlex
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 复用自愈 Agent 的危险命令黑名单（单一事实来源，避免两套口径）
from .selfheal import DANGEROUS_TOKENS

# shell 动作白名单：只允许这些只读命令作为首个 token
ACTION_SHELL_WHITELIST = (
    "dumpsys", "logcat", "cat", "ps", "top", "getprop",
    "df", "uptime", "ls", "pidof", "wm", "settings",
)

# 截图产物目录（static 下，前端可直接以 URL 预览）
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_MODULE_DIR))
CAPTURE_DIR = os.path.join(_PROJECT_ROOT, "static", "alert_captures")
CAPTURE_URL_PREFIX = "/static/alert_captures"

# 单个动作执行超时（秒）
ACTION_TIMEOUT = 20


def _now_iso() -> str:
    return datetime.now().isoformat()


def _result(action_type: str, status: str, summary: str,
            artifact: str = "", output: str = "", command: str = "") -> Dict[str, Any]:
    """统一动作结果结构。status: ok / failed / refused / skipped"""
    return {
        "type": action_type,
        "status": status,
        "summary": summary,
        "artifact": artifact,      # 截图等产物的 URL 路径（可为空）
        "output": output,          # shell 输出摘要（可为空）
        "command": command,        # 实际执行的命令（可审计）
        "executed_at": _now_iso(),
    }


def validate_shell_action(cmd: str) -> Optional[str]:
    """校验 shell 动作是否为白名单只读命令。合法返回 None，否则返回拒绝原因。

    规则：
    - 命中 DANGEROUS_TOKENS 黑名单 → 拒绝
    - 管道各段的首个命令都必须在白名单内（允许 grep/head/tail/sort/uniq/wc 作为过滤器）
    """
    text = (cmd or "").strip()
    if not text:
        return "命令为空"
    lowered = text.lower()
    for tok in DANGEROUS_TOKENS:
        if tok in lowered:
            return f"命中危险命令黑名单: {tok.strip()}"
    # 允许的管道过滤器（不产生副作用）
    filters = ("grep", "head", "tail", "sort", "uniq", "wc", "cut", "awk", "sed")
    for i, seg in enumerate(text.split("|")):
        seg = seg.strip()
        if not seg:
            return "存在空管道段"
        first = seg.split()[0]
        if i == 0:
            if first not in ACTION_SHELL_WHITELIST:
                return f"命令 '{first}' 不在只读白名单内"
        else:
            if first not in ACTION_SHELL_WHITELIST and first not in filters:
                return f"管道段命令 '{first}' 不在只读白名单内"
    # 禁止重定向写文件（> / >>），避免借重定向落盘产生副作用
    if ">" in text.replace("2>/dev/null", "").replace(">/dev/null", ""):
        return "禁止输出重定向（>）"
    return None


def _do_screenshot(device_id: str, alert_id: str) -> Dict[str, Any]:
    """截图取证：adb exec-out screencap -p 落盘 static/alert_captures/。"""
    try:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
    except Exception as e:
        return _result("screenshot", "failed", f"截图目录创建失败: {e}")
    safe_id = "".join(c for c in (alert_id or "alert") if c.isalnum() or c in "_-") or "alert"
    fname = f"{safe_id}_{datetime.now().strftime('%H%M%S')}.png"
    fpath = os.path.join(CAPTURE_DIR, fname)
    cmd = f"adb -s {shlex.quote(device_id)} exec-out screencap -p"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=ACTION_TIMEOUT)
        data = proc.stdout or b""
        # PNG 魔数校验：避免把 adb 错误信息当图片存下来
        if len(data) < 100 or not data.startswith(b"\x89PNG"):
            err = (proc.stderr or b"").decode("utf-8", "ignore")[:200]
            return _result("screenshot", "failed", f"截图无效输出: {err or '非 PNG 数据'}", command=cmd)
        with open(fpath, "wb") as f:
            f.write(data)
        url = f"{CAPTURE_URL_PREFIX}/{fname}"
        logger.info(f"告警动作截图成功: {url} ({len(data)} bytes)")
        return _result("screenshot", "ok", f"崩溃现场截图已留存（{len(data) // 1024}KB）",
                       artifact=url, command=cmd)
    except subprocess.TimeoutExpired:
        return _result("screenshot", "failed", f"截图超时（>{ACTION_TIMEOUT}s）", command=cmd)
    except Exception as e:
        return _result("screenshot", "failed", f"截图失败: {e}", command=cmd)


def _do_shell(device_id: str, shell_cmd: str, package: str = "") -> Dict[str, Any]:
    """白名单只读 shell 动作。"""
    filled = shell_cmd.strip()
    if "{package}" in filled:
        filled = filled.replace("{package}", shlex.quote(package or ""))
    reason = validate_shell_action(filled)
    if reason:
        logger.warning(f"告警动作拒绝执行 shell: {filled} ({reason})")
        return _result("shell", "refused", f"已拒绝执行：{reason}", command=filled)
    cmd = f"adb -s {shlex.quote(device_id)} shell {filled}"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=ACTION_TIMEOUT)
        out = (proc.stdout or "").strip()
        if not out:
            return _result("shell", "ok", "命令执行完成（无输出）", command=filled)
        # 截断：保头+保尾
        if len(out) > 1500:
            out = out[:800] + "\n...(中间截断)...\n" + out[-500:]
        return _result("shell", "ok", f"只读命令采集完成（{len(out)} 字符）",
                       output=out, command=filled)
    except subprocess.TimeoutExpired:
        return _result("shell", "failed", f"命令超时（>{ACTION_TIMEOUT}s）", command=filled)
    except Exception as e:
        return _result("shell", "failed", f"命令执行失败: {e}", command=filled)


def execute_action(action: str, device_id: str, alert_id: str = "",
                   package: str = "") -> Optional[Dict[str, Any]]:
    """执行规则配置的告警动作（只读）。

    :param action: 规则的 action 字段：screenshot / shell:<cmd> / custom_shell:<cmd> / none / 空
    :return: 结构化动作结果 dict；未配置动作返回 None
    """
    act = (action or "").strip()
    if not act or act.lower() == "none":
        return None
    if not device_id:
        return _result("unknown", "failed", "缺少设备 id，无法执行动作")

    if act == "screenshot":
        return _do_screenshot(device_id, alert_id)

    if act.startswith("shell:"):
        return _do_shell(device_id, act[len("shell:"):], package)

    if act.startswith("custom_shell"):
        # 自由命令不放开：守住"不自动修改/不越护栏"红线（配置了也拒绝，且原因可审计）
        logger.warning(f"告警动作拒绝 custom_shell（策略禁止）: {act[:80]}")
        return _result("custom_shell", "refused",
                       "custom_shell 自由命令按安全策略禁止执行（仅允许 screenshot 与白名单只读 shell）",
                       command=act[:200])

    # 未知动作类型：不执行，记录
    return _result(act.split(":")[0][:20], "skipped", f"未知动作类型，未执行: {act[:80]}")


def action_result_to_str(result: Optional[Dict[str, Any]]) -> str:
    """序列化动作结果为 JSON 字符串（写入 AlertRecord.action_taken，前端可解析）。"""
    if not result:
        return ""
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)
