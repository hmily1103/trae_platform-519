"""
AI 失败归因：执行失败 → 证据汇总 → 根因摘要 + 建议补丁（人工在环确认后再改用例）。

对齐 Raina「失败归因 → 人工确认是否修复重跑」与平台 log_monitor 红线：
不自动改脚本落地，除非调用方显式 apply。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from utils.llm_client import call_llm
from utils.logger import setup_logger

from .core.device_controller import DeviceController
from .core.ui_tree_parser import UITreeParser
from .models import UIAction, UISelector
from .storage import RecordingStorage

logger = setup_logger("ui_automation_ai_diagnoser")


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = None
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if in_string:
            if c == in_string:
                in_string = None
            continue
        if c in ('"', "'"):
            in_string = c
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(raw[start : i + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _compact_ui(xml: str, limit: int = 60) -> List[Dict[str, Any]]:
    try:
        parser = UITreeParser(xml)
    except Exception:
        return []
    items: List[Dict[str, Any]] = []
    for el in parser.elements:
        if not (el.clickable or el.text or el.resource_id or el.content_desc):
            continue
        item: Dict[str, Any] = {}
        if el.resource_id:
            item["id"] = el.resource_id
        if el.text:
            item["text"] = el.text[:60]
        if el.content_desc:
            item["desc"] = el.content_desc[:60]
        if el.class_name:
            item["cls"] = el.class_name.split(".")[-1]
        if el.bounds:
            item["bounds"] = el.bounds
        items.append(item)
        if len(items) >= limit:
            break
    return items


class AIDiagnoser:
    """执行失败归因与建议补丁（需人工确认）。"""

    def __init__(self, storage: RecordingStorage):
        self.storage = storage

    def diagnose_failure(
        self,
        case_id: str,
        *,
        device_id: str = "",
        failed_step: Optional[int] = None,
        error: str = "",
        step_details: Optional[List[Dict[str, Any]]] = None,
        dump_live_ui: bool = True,
    ) -> Dict[str, Any]:
        session = self.storage.load_recording(case_id)
        if not session:
            raise ValueError(f"用例不存在: {case_id}")

        # 优先用报告传入的失败步；否则从 step_details / traces 推断
        if failed_step is None and step_details:
            for s in step_details:
                if s.get("success") is False:
                    failed_step = int(s.get("step_num") or 0)
                    error = error or (s.get("error") or "")
                    break

        if failed_step is None:
            traces = self.storage.load_execution_traces(case_id)
            for t in traces:
                data = t.to_dict() if hasattr(t, "to_dict") else t
                if not data.get("success", True):
                    failed_step = int(data.get("step_num") or 0)
                    error = error or (data.get("error") or "")
                    break

        expected_action = None
        case_actions_brief = [a.to_dict() for a in session.actions]
        if failed_step:
            for a in session.actions:
                if a.step_num == failed_step:
                    expected_action = a.to_dict()
                    break
            if expected_action is None and 0 < failed_step <= len(session.actions):
                expected_action = session.actions[failed_step - 1].to_dict()

        live_ui: List[Dict[str, Any]] = []
        live_dump_ok = False
        target_device = device_id or session.device_id
        if dump_live_ui and target_device:
            try:
                controller = DeviceController(target_device)
                xml = controller.get_ui_tree()
                if xml:
                    live_ui = _compact_ui(xml)
                    live_dump_ok = True
            except Exception as e:
                logger.warning(f"现场 UI dump 失败: {e}")

        diagnosis = self._llm_diagnose(
            case_name=session.name or case_id,
            package_name=session.package_name,
            failed_step=failed_step,
            error=error,
            expected_action=expected_action,
            case_actions=case_actions_brief,
            step_details=step_details or [],
            live_ui=live_ui,
        )

        result = {
            "case_id": case_id,
            "device_id": target_device,
            "failed_step": failed_step,
            "error": error,
            "expected_action": expected_action,
            "live_ui_dumped": live_dump_ok,
            "needs_human_approval": True,
            "root_cause": diagnosis.get("root_cause") or "未能定位根因",
            "confidence": float(diagnosis.get("confidence") or 0.0),
            "category": diagnosis.get("category") or "unknown",
            "evidence": diagnosis.get("evidence") or [],
            "suggestions": diagnosis.get("suggestions") or [],
            "suggested_patch": diagnosis.get("suggested_patch"),
            "diagnosed_at": time.time(),
        }

        # 落盘归因结果，便于报告页回看
        try:
            path = os.path.join(self.storage.reports_dir, f"ai_diag_{case_id}_{int(time.time())}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            result["diagnosis_file"] = path
        except Exception as e:
            logger.warning(f"保存归因结果失败: {e}")

        return result

    def apply_suggested_patch(
        self,
        case_id: str,
        suggested_patch: Dict[str, Any],
        *,
        approved: bool = False,
    ) -> Dict[str, Any]:
        if not approved:
            raise PermissionError("未人工确认，拒绝修改用例（needs_human_approval）")
        if not isinstance(suggested_patch, dict):
            raise ValueError("suggested_patch 无效")

        session = self.storage.load_recording(case_id)
        if not session:
            raise ValueError(f"用例不存在: {case_id}")

        step_num = int(suggested_patch.get("step_num") or 0)
        if step_num <= 0:
            raise ValueError("suggested_patch.step_num 必填")

        target: Optional[UIAction] = None
        for a in session.actions:
            if a.step_num == step_num:
                target = a
                break
        if target is None and step_num <= len(session.actions):
            target = session.actions[step_num - 1]

        if target is None:
            raise ValueError(f"找不到步骤 {step_num}")

        if suggested_patch.get("action_type"):
            target.action_type = str(suggested_patch["action_type"])
        if "value" in suggested_patch and suggested_patch["value"] is not None:
            target.value = str(suggested_patch["value"])
        if suggested_patch.get("description"):
            target.description = str(suggested_patch["description"])
            target.display = target.description
        if suggested_patch.get("wait_after") is not None:
            target.wait_after = int(suggested_patch["wait_after"])

        sel = suggested_patch.get("selector")
        if isinstance(sel, dict) and sel.get("strategy") and sel.get("value") is not None:
            target.selector = UISelector.from_dict(sel)

        coords = suggested_patch.get("coordinates")
        if isinstance(coords, dict) and "x" in coords and "y" in coords:
            target.coordinates = {
                "x": int(coords["x"]),
                "y": int(coords["y"]),
            }

        target.status = "completed"
        ok = self.storage.save_recording(session)
        if not ok:
            raise RuntimeError("保存用例失败")

        return {
            "case_id": case_id,
            "step_num": target.step_num,
            "updated_action": target.to_dict(),
            "message": "已按人工确认补丁更新用例，可重新执行验证",
        }

    def _llm_diagnose(
        self,
        *,
        case_name: str,
        package_name: str,
        failed_step: Optional[int],
        error: str,
        expected_action: Optional[Dict[str, Any]],
        case_actions: List[Dict[str, Any]],
        step_details: List[Dict[str, Any]],
        live_ui: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        prompt = (
            "你是 Android UI 自动化失败归因助手。根据失败步骤、错误信息、期望操作与当前界面控件，"
            "给出根因摘要和建议补丁。\n"
            "只输出 JSON：\n"
            "{\n"
            '  "root_cause": "一句话根因",\n'
            '  "confidence": 0.0,\n'
            '  "category": "selector_stale|timing|ui_changed|device|assertion|unknown",\n'
            '  "evidence": ["证据1"],\n'
            '  "suggestions": ["人工可执行建议1"],\n'
            '  "suggested_patch": {\n'
            '    "step_num": 1,\n'
            '    "action_type": "click",\n'
            '    "selector": {"strategy":"text","value":"搜索","fallbacks":[]},\n'
            '    "value": null,\n'
            '    "wait_after": 1500,\n'
            '    "description": "点击搜索",\n'
            '    "coordinates": null\n'
            "  },\n"
            '  "needs_human_approval": true\n'
            "}\n"
            "约束：不要建议自动改代码或执行危险 shell；"
            "suggested_patch 仅改失败那一步的定位/等待/输入；找不到可靠补丁时 suggested_patch 可为 null。\n"
            f"用例: {case_name}\n"
            f"包名: {package_name}\n"
            f"失败步骤: {failed_step}\n"
            f"错误: {error}\n"
            f"期望操作: {json.dumps(expected_action, ensure_ascii=False)}\n"
            f"用例全部步骤: {json.dumps(case_actions[:25], ensure_ascii=False)}\n"
            f"步骤摘要: {json.dumps(step_details[-12:], ensure_ascii=False)}\n"
            f"当前界面控件: {json.dumps(live_ui[:50], ensure_ascii=False)}"
        )
        try:
            raw = call_llm(
                [{"role": "user", "content": prompt}],
                timeout=90,
                temperature=0.2,
                max_tokens=1200,
            )
            data = _extract_json_object(raw)
            if data:
                data["needs_human_approval"] = True
                return data
        except Exception as e:
            logger.warning(f"LLM 归因失败: {e}")
            return {
                "root_cause": f"归因服务异常: {e}",
                "confidence": 0.0,
                "category": "unknown",
                "evidence": [error] if error else [],
                "suggestions": ["检查设备在线、重新 dump UI、人工打开录制页核对定位"],
                "suggested_patch": None,
                "needs_human_approval": True,
            }
        return {
            "root_cause": "模型未返回可解析结果",
            "confidence": 0.0,
            "category": "unknown",
            "evidence": [error] if error else [],
            "suggestions": ["人工查看失败截图与步骤详情"],
            "suggested_patch": None,
            "needs_human_approval": True,
        }
