# -*- coding: utf-8 -*-
"""通过 SSH 获取服务器上 CPU/内存占用 Top10 进程"""
import logging
import re
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

try:
    import paramiko
except ImportError:
    paramiko = None


def _parse_ps_eo(text: str) -> List[Dict[str, Any]]:
    """解析 ps -eo pid,pcpu,pmem,comm 输出"""
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    result = []
    for i, line in enumerate(lines[:10]):
        parts = re.split(r"\s+", line, 3)
        if len(parts) >= 4:
            result.append({
                "rank": i + 1, "pid": parts[0], "cpu": parts[1], "mem": parts[2],
                "name": parts[3][:40],
            })
    return result


def _parse_ps_aux(text: str, sort_by: str = "cpu") -> List[Dict[str, Any]]:
    """解析 ps aux 输出（兼容 BusyBox），sort_by: cpu 或 mem"""
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    rows = []
    for line in lines[1:]:  # 跳过表头
        parts = re.split(r"\s+", line, 10)
        if len(parts) >= 11:
            pid, cpu, mem = parts[1], parts[2], parts[3]
            name = parts[10][:40] if len(parts) > 10 else ""
            rows.append({"pid": pid, "cpu": cpu, "mem": mem, "name": name})
    try:
        rows.sort(key=lambda x: float(x["cpu"] if sort_by == "cpu" else x["mem"]), reverse=True)
    except (ValueError, TypeError):
        pass
    return [{"rank": i + 1, **r} for i, r in enumerate(rows[:10])]


def _parse_ps_o(text: str, sort_by: str = "cpu") -> List[Dict[str, Any]]:
    """解析 ps -o pid,pcpu,pmem,args 输出（无表头）"""
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    rows = []
    for line in lines:
        parts = re.split(r"\s+", line, 3)
        if len(parts) >= 4:
            try:
                pid, cpu, mem = parts[0], parts[1], parts[2]
                name = parts[3][:40]
                rows.append({"pid": pid, "cpu": cpu, "mem": mem, "name": name})
            except IndexError:
                pass
    try:
        rows.sort(key=lambda x: float(x["cpu"] if sort_by == "cpu" else x["mem"]), reverse=True)
    except (ValueError, TypeError):
        pass
    return [{"rank": i + 1, **r} for i, r in enumerate(rows[:10])]


def _parse_top_generic(text: str, sort_by: str = "cpu") -> List[Dict[str, Any]]:
    """解析 top -n 1 输出（尝试自动识别表头）"""
    # 移除 ANSI 转义序列 (颜色代码等)
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    header_idx = -1
    col_map = {}
    
    # 查找表头行 (通常包含 PID 和 CPU/MEM 关键字)
    for i, line in enumerate(lines[:20]):
        parts = re.split(r"\s+", line)
        parts_lower = [p.lower() for p in parts]
        if "pid" in parts_lower and ("cpu" in parts_lower or "%cpu" in parts_lower or "cpu%" in parts_lower):
            header_idx = i
            for idx, p in enumerate(parts_lower):
                if p == "pid": col_map["pid"] = idx
                elif "cpu" in p: col_map["cpu"] = idx
                elif "mem" in p: col_map["mem"] = idx
                elif p in ["command", "name", "cmd", "args"]: col_map["name"] = idx
            break
            
    if header_idx == -1 or "pid" not in col_map:
        return []
        
    rows = []
    for line in lines[header_idx+1:]:
        parts = re.split(r"\s+", line)
        if len(parts) <= max(col_map.values()):
            continue
            
        try:
            pid = parts[col_map["pid"]]
            if not pid.isdigit(): continue
            
            cpu = parts[col_map.get("cpu", -1)] if "cpu" in col_map else "0"
            mem = parts[col_map.get("mem", -1)] if "mem" in col_map else "0"
            
            # Name
            name = "unknown"
            if "name" in col_map:
                name = parts[col_map["name"]]
            else:
                # 尝试猜测 name 列（通常是最后一列）
                name = parts[-1]
                
            rows.append({
                "pid": pid, 
                "cpu": cpu.replace("%", ""), 
                "mem": mem.replace("%", ""), 
                "name": name[:40]
            })
        except Exception:
            pass
            
    try:
        rows.sort(key=lambda x: float(x["cpu"] if sort_by == "cpu" else x["mem"]), reverse=True)
    except (ValueError, TypeError):
        pass
        
    return [{"rank": i + 1, **r} for i, r in enumerate(rows[:10])]


def fetch_top_processes(
    host: str,
    port: int,
    username: str,
    password: str,
    timeout: int = 8,
) -> Dict[str, Any]:
    """
    通过 SSH 获取 CPU 和内存 Top10 进程。
    返回: { "success": bool, "error": str?, "cpu_top": [...], "mem_top": [...] }
    """
    if not paramiko:
        return {"success": False, "error": "Paramiko 未安装，无法使用 SSH 功能", "cpu_top": [], "mem_top": []}

    host = (host or "").strip()
    if not host or not username:
        return {"success": False, "error": "请填写服务器 IP 和用户名", "cpu_top": [], "mem_top": []}

    port = int(port) if port else 222
    # 优先使用 ps -eo（标准 Linux），失败则用 ps aux（兼容 BusyBox）
    cmd1 = "ps -eo pid,pcpu,pmem,comm --no-headers --sort=-pcpu 2>/dev/null | head -10"
    cmd2 = "ps -eo pid,pcpu,pmem,comm --no-headers --sort=-pmem 2>/dev/null | head -10"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password or None,
            timeout=5,
        )
    except Exception as e:
        err = str(e).lower()
        if "auth" in err or "password" in err:
            msg = "SSH 认证失败，请检查用户名和密码"
        elif "timeout" in err or "timed out" in err:
            msg = f"连接超时，请检查 IP {host} 和端口 {port} 是否可达"
        elif "refused" in err or "connection" in err:
            msg = f"连接被拒绝，请确认 SSH 服务在 {host}:{port} 已启动"
        else:
            msg = f"连接失败: {e}"
        return {"success": False, "error": msg, "cpu_top": [], "mem_top": []}

    try:
        _, stdout, stderr = client.exec_command(f"{cmd1}; echo '---SEP---'; {cmd2}", timeout=timeout)
        out = stdout.read().decode("utf-8", errors="ignore")
        err_out = stderr.read().decode("utf-8", errors="ignore")
        client.close()
    except Exception as e:
        return {"success": False, "error": f"执行命令失败: {e}", "cpu_top": [], "mem_top": []}

    parts = out.split("---SEP---")
    cpu_raw = parts[0] if len(parts) > 0 else ""
    mem_raw = parts[1] if len(parts) > 1 else ""
    cpu_top = _parse_ps_eo(cpu_raw)
    mem_top = _parse_ps_eo(mem_raw)

    # 若无数据，尝试多种 ps 格式（兼容 BusyBox/嵌入式）
    fallbacks = [
        ("ps aux 2>/dev/null", _parse_ps_aux),
        ("ps -o pid=,pcpu=,pmem=,args= 2>/dev/null", lambda t, sb: _parse_ps_o(t, sb)),
        ("ps -o pid,pcpu,pmem,args 2>/dev/null | tail -n +2", lambda t, sb: _parse_ps_o(t, sb)),
        # Android / BusyBox top command
        ("top -n 1 2>/dev/null", lambda t, sb: _parse_top_generic(t, sb)),
        # Linux top batch mode
        ("top -b -n 1 2>/dev/null", lambda t, sb: _parse_top_generic(t, sb)),
        # Android specific ps (newer Android)
        ("ps -A -o PID,NAME,%CPU,%MEM 2>/dev/null", lambda t, sb: _parse_top_generic(t, sb)),
    ]
    for cmd, parser in fallbacks:
        if cpu_top or mem_top:
            break
        try:
            c2 = paramiko.SSHClient()
            c2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c2.connect(hostname=host, port=port, username=username, password=password or None, timeout=5)
            _, so, _ = c2.exec_command(cmd, timeout=timeout)
            out2 = so.read().decode("utf-8", errors="ignore")
            c2.close()
            if out2.strip():
                cpu_top = parser(out2, "cpu")
                mem_top = parser(out2, "mem")
        except Exception:
            pass

    return {
        "success": True,
        "error": "连接成功，但目标系统 ps 格式不兼容，无法解析进程数据" if (not cpu_top and not mem_top) else None,
        "cpu_top": cpu_top,
        "mem_top": mem_top,
    }
