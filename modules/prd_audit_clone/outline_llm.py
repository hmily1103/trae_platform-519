# -*- coding: utf-8 -*-
"""
大模型专用：通用 PRD 认知大纲（与本地 outline_engine 并行，不改变原有流水线默认行为）。
"""
import json
import logging
import os
from typing import Any, Dict, Optional

from utils.llm_client import call_llm, _extract_first_json_object

logger = logging.getLogger(__name__)

STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTLINE_LLM_PROMPT_FILE = os.path.join(STORAGE_DIR, "prd_outline_llm_prompt.txt")


def _trim_stage1_for_outline(stage1: Dict[str, Any]) -> Dict[str, Any]:
    """仅保留与大纲相关的字段，紧凑 JSON。"""
    s = stage1 if isinstance(stage1, dict) else {}
    keys = (
        "modules",
        "flows",
        "states",
        "actions",
        "business_rules",
        "exceptions",
        "edge_cases",
        "dependencies",
        "data_structures",
        "permissions",
        "non_functional",
        "parse_quality",
        "required_elements",
    )
    out: Dict[str, Any] = {}
    for k in keys:
        v = s.get(k)
        if v is None:
            continue
        if k == "parse_quality" and isinstance(v, dict):
            out[k] = {kk: v.get(kk) for kk in ("score", "level", "notes") if kk in v}
        elif isinstance(v, (list, dict, str, int, float, bool)):
            out[k] = v
    blocks = s.get("blocks")
    if isinstance(blocks, list) and blocks:
        out["blocks_preview"] = blocks[:15]
    return out


def run_outline_llm(
    prd_text: str,
    stage1_output: Dict[str, Any],
    llm_config_path: str,
    timeout: int = 120,
    llm_config_override: Optional[Dict[str, Any]] = None,
    prd_max_chars: int = 100000,
) -> Dict[str, Any]:
    """
    调用大模型生成通用四支柱大纲 JSON。

    返回:
      ok: bool
      llm_outline: dict 解析后的 JSON（失败时为空 dict）
      error: 可选错误信息
      raw_response: 模型原文截断（便于排错）
    """
    prd_text = str(prd_text or "").strip()
    if not prd_text:
        return {"ok": False, "llm_outline": {}, "error": "prd_text 为空", "raw_response": ""}

    prompt_path = OUTLINE_LLM_PROMPT_FILE
    if not os.path.exists(prompt_path):
        return {"ok": False, "llm_outline": {}, "error": f"大纲 prompt 文件不存在: {prompt_path}", "raw_response": ""}

    with open(prompt_path, "r", encoding="utf-8") as f:
        template = f.read()

    trimmed = _trim_stage1_for_outline(stage1_output if isinstance(stage1_output, dict) else {})
    stage1_json = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
    if len(stage1_json) > 48000:
        stage1_json = stage1_json[:48000] + "…"

    excerpt = prd_text if len(prd_text) <= prd_max_chars else prd_text[:prd_max_chars] + "\n\n…[PRD 过长已截断]…"
    user_content = template.replace("{stage1_json}", stage1_json).replace("{prd_excerpt}", excerpt)

    try:
        resp = call_llm(
            [{"role": "user", "content": user_content}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            stream=False,
            timeout=timeout,
            max_tokens=8192,
        )
    except Exception as e:
        logger.warning("run_outline_llm LLM 调用失败: %s", e)
        return {"ok": False, "llm_outline": {}, "error": str(e), "raw_response": ""}

    raw = str(resp or "").strip()
    parsed = _extract_first_json_object(raw)
    if not isinstance(parsed, dict) or not parsed:
        logger.warning("run_outline_llm JSON 解析为空，原文前 500 字: %s", raw[:500])
        return {
            "ok": False,
            "llm_outline": {},
            "error": "模型返回无法解析为 JSON，请检查 prompt 或重试",
            "raw_response": raw[:8000],
        }

    return {"ok": True, "llm_outline": parsed, "error": None, "raw_response": raw[:4000]}


def merge_llm_with_local(
    llm_result: Dict[str, Any],
    local_outline_engine: Dict[str, Any],
) -> Dict[str, Any]:
    """将 LLM 大纲与本地 outline_engine 结果合并为统一结构（供前端选用）。"""
    return {
        "llm": llm_result if isinstance(llm_result, dict) else {},
        "local": local_outline_engine if isinstance(local_outline_engine, dict) else {},
        "source": "llm+local",
    }
