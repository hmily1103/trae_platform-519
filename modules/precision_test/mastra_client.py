"""Mastra 多步分析 Agent 后端（可选增强，预留接口）。

精准回归的分析阶段可委托 Mastra（Next.js + Mastra 诊断服务，默认端口 4111）做
多步影响分析（风险粗分 -> 分类细化 -> 交叉校验）。这是「能力增强」而非「架构迁移」：
精准回归模块仍留在 Flask，仅在分析阶段可选地调用 Mastra 的 HTTP 端点。

部署前提：Mastra 服务需配置 LLM key 并运行；默认关闭（不配置 MASTRA_ANALYSIS_URL）。
调用失败时静默回退到本地规则 + LLM，不影响主流程。

注意：架构上不把精准回归迁移到 Next.js + Mastra（全量重写不划算），而是
「Flask 承载模块 + Mastra 提供多步 Agent 分析能力」的渐进组合。
"""
import os
import json
import urllib.request
import urllib.error


def call_mastra_analysis(code_diff, requirement, project_type):
    """调用 Mastra 分析端点。未配置或失败返回 None（调用方静默回退）。"""
    url = os.environ.get("MASTRA_ANALYSIS_URL") or ""
    if not url:
        return None
    payload = {
        "diff": code_diff,
        "requirement": requirement,
        "project_type": project_type,
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"error": "mastra_unreachable: %s" % exc}
    except Exception as exc:  # noqa: BLE001 - 预留接口，任何异常都回退
        return {"error": str(exc)}
