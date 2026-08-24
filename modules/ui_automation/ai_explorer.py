"""
AI 驱动 UI 探索：自然语言 / 用例步骤 → 抓 UI 快照 → 固化为 RecordingSession。

对齐 Raina APP 方案的「探索 → 生成脚本」思路，技术栈仍用现有 uiautomator2，
不引入 Appium；产出可直接走 ScriptGenerator / 套件回归。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.llm_client import call_llm
from utils.logger import setup_logger

from .core.device_controller import DeviceController
from .core.element_locator import ElementLocator
from .core.ui_tree_parser import UIElement, UITreeParser
from .models import RecordingSession, UIAction, UISelector
from .storage import RecordingStorage

logger = setup_logger("ui_automation_ai_explorer")

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]

MAX_ELEMENTS_FOR_LLM = 80
MAX_PLANNED_STEPS = 30


def _extract_json_payload(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start_obj = raw.find("{")
    start_arr = raw.find("[")
    starts = [i for i in (start_obj, start_arr) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    opener = raw[start]
    closer = "}" if opener == "{" else "]"
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
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _split_case_lines(case_text: str) -> List[str]:
    lines: List[str] = []
    for raw in (case_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\d]+[\.\)、]\s*", "", line).strip()
        if line:
            lines.append(line)
    return lines[:MAX_PLANNED_STEPS]


def _element_brief(idx: int, el: UIElement) -> Dict[str, Any]:
    brief: Dict[str, Any] = {"i": idx}
    if el.resource_id:
        brief["id"] = el.resource_id
    if el.text:
        brief["text"] = el.text[:80]
    if el.content_desc:
        brief["desc"] = el.content_desc[:80]
    if el.class_name:
        brief["cls"] = el.class_name.split(".")[-1]
    if el.bounds:
        brief["cx"] = el.bounds.get("center_x")
        brief["cy"] = el.bounds.get("center_y")
    brief["clickable"] = bool(el.clickable)
    return brief


def _collect_candidates(parser: UITreeParser) -> List[UIElement]:
    scored: List[Tuple[int, UIElement]] = []
    for el in parser.elements:
        score = 0
        if el.clickable:
            score += 5
        if el.focusable:
            score += 2
        if el.resource_id:
            score += 4
        if el.text:
            score += 3
        if el.content_desc:
            score += 3
        if el.scrollable:
            score += 1
        if score <= 0:
            continue
        if not el.bounds:
            continue
        scored.append((score, el))
    scored.sort(key=lambda x: (-x[0], x[1].bounds["center_y"] if x[1].bounds else 0))
    return [el for _, el in scored[:MAX_ELEMENTS_FOR_LLM]]


class AIExplorer:
    """自然语言驱动的只读探索 + 用例固化。"""

    def __init__(self, storage: RecordingStorage):
        self.storage = storage

    def explore_and_create_case(
        self,
        device_id: str,
        case_text: str,
        *,
        name: str = "",
        package_name: str = "",
        project_id: str = "",
        description: str = "",
        execute_while_exploring: bool = True,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        case_text = (case_text or "").strip()
        if not device_id:
            raise ValueError("device_id 必填")
        if not case_text:
            raise ValueError("case_text 必填（自然语言或分步用例）")

        controller = DeviceController(device_id)
        if package_name:
            self._launch_app(controller, package_name)
            time.sleep(1.5)

        planned = self._plan_steps(case_text)
        recording_id = f"ai_{uuid.uuid4().hex[:12]}"
        session = RecordingSession(
            id=recording_id,
            device_id=device_id,
            package_name=package_name or "",
            created_at=datetime.now(),
            description=description or f"AI 探索生成：{case_text[:120]}",
            project_id=project_id or "",
            name=name or (planned[0][:40] if planned else "AI 生成用例"),
        )

        agent_log: List[Dict[str, Any]] = []
        self._emit(
            progress_callback,
            {
                "phase": "planned",
                "recording_id": recording_id,
                "planned_steps": planned,
            },
        )

        for idx, goal in enumerate(planned, start=1):
            step_result = self._resolve_one_step(
                controller=controller,
                session=session,
                step_num=idx,
                goal=goal,
                case_context=case_text,
                execute=execute_while_exploring,
            )
            agent_log.append(step_result)
            self._emit(
                progress_callback,
                {
                    "phase": "step",
                    "recording_id": recording_id,
                    "step_num": idx,
                    "goal": goal,
                    "result": step_result,
                },
            )
            if step_result.get("status") == "failed" and step_result.get("abort"):
                break
            wait_ms = int(step_result.get("wait_after") or 800)
            if execute_while_exploring and wait_ms > 0:
                time.sleep(wait_ms / 1000.0)

        ok = self.storage.save_recording(session)
        if not ok:
            raise RuntimeError("保存用例失败")

        return {
            "recording_id": recording_id,
            "name": session.name,
            "package_name": session.package_name,
            "step_count": len(session.actions),
            "planned_steps": planned,
            "actions": [a.to_dict() for a in session.actions],
            "agent_log": agent_log,
            "needs_human_review": True,
            "message": "已固化为可回归用例，建议人工抽查定位策略后再跑套件",
        }

    def _plan_steps(self, case_text: str) -> List[str]:
        lines = _split_case_lines(case_text)
        # 多行清晰步骤：直接使用，少一次 LLM
        if len(lines) >= 2:
            return lines

        prompt = (
            "你是 Android UI 自动化规划助手。把用户描述拆成可执行的有序步骤。\n"
            "只输出 JSON 数组，每项是一句短中文步骤，例如："
            '["打开点歌页","点击搜索框","输入周杰伦","点击第一首歌"]。\n'
            "约束：最多 20 步；不要解释；不要断言以外的废话；"
            "动作限于 click / input / swipe / wait / key / assertion；"
            "若用户描述含验证/成功标准，最后一步用断言（例如：断言：界面出现「搜索结果」）。\n"
            f"用户描述：\n{case_text}"
        )
        try:
            raw = call_llm(
                [{"role": "user", "content": prompt}],
                timeout=60,
                temperature=0.2,
                max_tokens=1024,
            )
            data = _extract_json_payload(raw)
            if isinstance(data, list):
                steps = [str(x).strip() for x in data if str(x).strip()]
                if steps:
                    return steps[:MAX_PLANNED_STEPS]
            if isinstance(data, dict) and isinstance(data.get("steps"), list):
                steps = [str(x).strip() for x in data["steps"] if str(x).strip()]
                if steps:
                    return steps[:MAX_PLANNED_STEPS]
        except Exception as e:
            logger.warning(f"LLM 规划失败，回退单步: {e}")

        return lines or [case_text.strip()]

    def _resolve_one_step(
        self,
        controller: DeviceController,
        session: RecordingSession,
        step_num: int,
        goal: str,
        case_context: str,
        execute: bool,
    ) -> Dict[str, Any]:
        xml = controller.get_ui_tree()
        if not xml:
            return {
                "status": "failed",
                "goal": goal,
                "error": "无法获取 UI 树",
                "abort": True,
            }

        screenshot_path = self.storage.get_screenshot_path(session.id, step_num)
        try:
            controller.screenshot(screenshot_path)
        except Exception:
            screenshot_path = None

        ui_tree_path = self.storage.get_ui_tree_path(session.id, step_num)
        try:
            import os

            os.makedirs(os.path.dirname(ui_tree_path), exist_ok=True)
            with open(ui_tree_path, "w", encoding="utf-8") as f:
                f.write(xml)
        except Exception as e:
            logger.warning(f"保存 UI 树失败: {e}")
            ui_tree_path = None

        parser = UITreeParser(xml)
        candidates = _collect_candidates(parser)
        briefs = [_element_brief(i, el) for i, el in enumerate(candidates)]
        decision = self._llm_pick_action(goal, case_context, briefs, session.package_name)

        action_type = (decision.get("action_type") or "click").lower().strip()
        wait_after = int(decision.get("wait_after") or 1000)
        description = (decision.get("description") or goal).strip()
        value = decision.get("value")
        element_index = decision.get("element_index")

        selector: Optional[UISelector] = None
        coordinates: Optional[Dict[str, int]] = None
        picked: Optional[UIElement] = None

        if action_type in ("click", "long_press", "input", "assertion") and element_index is not None:
            try:
                ei = int(element_index)
                if 0 <= ei < len(candidates):
                    picked = candidates[ei]
                    locator = ElementLocator(parser)
                    selector = locator._build_selector(picked)
                    if picked.bounds:
                        coordinates = {
                            "x": picked.bounds["center_x"],
                            "y": picked.bounds["center_y"],
                        }
            except (TypeError, ValueError):
                pass

        if action_type == "swipe":
            swipe = decision.get("swipe") or {}
            coordinates = {
                "x": int(swipe.get("x1") or 0),
                "y": int(swipe.get("y1") or 0),
                "x2": int(swipe.get("x2") or 0),
                "y2": int(swipe.get("y2") or 0),
            }
            selector = UISelector(
                strategy="coordinates",
                value=f"{coordinates['x']},{coordinates['y']}",
            )
        elif action_type == "wait":
            wait_after = int(value or wait_after or 1000)
            selector = UISelector(strategy="coordinates", value="0,0")
        elif action_type == "key":
            selector = UISelector(strategy="coordinates", value=str(value or "4"))
        elif not selector:
            # 坐标兜底
            cx = decision.get("x")
            cy = decision.get("y")
            if cx is not None and cy is not None:
                coordinates = {"x": int(cx), "y": int(cy)}
                selector = UISelector(strategy="coordinates", value=f"{cx},{cy}")
            else:
                return {
                    "status": "failed",
                    "goal": goal,
                    "error": "LLM 未给出可用控件或坐标",
                    "decision": decision,
                    "abort": False,
                }

        exec_ok = True
        exec_error = ""
        if execute:
            exec_ok, exec_error = self._execute_action(
                controller, action_type, coordinates, value, wait_after
            )

        display = description
        if selector and selector.strategy == "text":
            display = f"{action_type}: {selector.value}"
        elif selector and selector.strategy == "resource_id":
            display = f"{action_type}: {selector.value.split('/')[-1]}"

        action = UIAction(
            step_num=step_num,
            action_type=action_type,
            selector=selector,
            value=str(value) if value is not None else None,
            coordinates=coordinates,
            screenshot=screenshot_path,
            ui_tree=ui_tree_path,
            timestamp=time.time(),
            wait_after=wait_after,
            description=description,
            display=display,
            status="completed" if exec_ok else "failed",
        )
        session.actions.append(action)
        self.storage.save_recording(session)

        return {
            "status": "ok" if exec_ok else "failed",
            "goal": goal,
            "action_type": action_type,
            "description": description,
            "selector": selector.to_dict() if selector else None,
            "wait_after": wait_after,
            "error": exec_error or None,
            "decision": decision,
            "abort": False,
        }

    def _llm_pick_action(
        self,
        goal: str,
        case_context: str,
        briefs: List[Dict[str, Any]],
        package_name: str,
    ) -> Dict[str, Any]:
        prompt = (
            "你是 Android UI 自动化探索助手。根据当前界面控件列表，选择完成「本步目标」的操作。\n"
            "只输出一个 JSON 对象，字段：\n"
            '{"action_type":"click|input|swipe|wait|key|assertion|long_press",'
            '"element_index":0,'
            '"value":"输入文本或按键码或等待毫秒或断言期望",'
            '"wait_after":1000,'
            '"description":"短描述",'
            '"swipe":{"x1":0,"y1":0,"x2":0,"y2":0},'
            '"x":null,"y":null,'
            '"confidence":0.0}\n'
            "规则：优先用 element_index 选列表中的控件；找不到再给 x/y；"
            "input 时 value 必填；assertion 用 element_index 指向要断言的控件；"
            "不要编造不存在的 resource-id。\n"
            f"应用包名: {package_name or '未知'}\n"
            f"整段用例上下文: {case_context[:500]}\n"
            f"本步目标: {goal}\n"
            f"控件列表: {json.dumps(briefs, ensure_ascii=False)}"
        )
        try:
            raw = call_llm(
                [{"role": "user", "content": prompt}],
                timeout=90,
                temperature=0.1,
                max_tokens=800,
            )
            data = _extract_json_payload(raw)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"LLM 选控件失败: {e}")
            return {"action_type": "wait", "value": 1000, "description": f"跳过: {goal}", "error": str(e)}
        return {"action_type": "wait", "value": 1000, "description": f"无法解析: {goal}"}

    def _execute_action(
        self,
        controller: DeviceController,
        action_type: str,
        coordinates: Optional[Dict],
        value: Any,
        wait_after: int,
    ) -> Tuple[bool, str]:
        try:
            if action_type == "wait":
                time.sleep(max(int(value or wait_after), 0) / 1000.0)
                return True, ""
            if action_type == "key":
                ok = controller.press_key(int(value or 4))
                return ok, "" if ok else (controller.last_output or "按键失败")
            if action_type == "swipe" and coordinates:
                ok = controller.swipe(
                    int(coordinates.get("x", 0)),
                    int(coordinates.get("y", 0)),
                    int(coordinates.get("x2", 0)),
                    int(coordinates.get("y2", 0)),
                )
                return ok, "" if ok else (controller.last_output or "滑动失败")
            if action_type in ("click", "long_press", "assertion") and coordinates:
                ok = controller.click(int(coordinates["x"]), int(coordinates["y"]))
                return ok, "" if ok else (controller.last_output or "点击失败")
            if action_type == "input":
                if coordinates:
                    controller.click(int(coordinates["x"]), int(coordinates["y"]))
                    time.sleep(0.3)
                ok = controller.input_text(str(value or ""))
                return ok, "" if ok else (controller.last_output or "输入失败")
            return False, f"不支持的动作: {action_type}"
        except Exception as e:
            return False, str(e)

    def _launch_app(self, controller: DeviceController, package_name: str) -> None:
        pkg = (package_name or "").strip()
        if not pkg:
            return
        # monkey 拉起 LAUNCHER Activity，兼容多数 STB/Android 应用
        controller._run_adb_command(
            [
                "shell",
                "monkey",
                "-p",
                pkg,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=15,
        )

    @staticmethod
    def _emit(cb: ProgressCallback, event: Dict[str, Any]) -> None:
        if not cb:
            return
        try:
            cb(event)
        except Exception:
            pass
