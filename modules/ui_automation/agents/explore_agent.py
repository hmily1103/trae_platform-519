"""
UI 闭环探索 Agent：observe → decide → act → verify → retry/replan。

在现有 AIExplorer 规划/固化能力之上增加：
- 单步多候选重试（排除已失败控件）
- 失败后滚动再找
- 连续失败时重规划剩余步骤
- tool/步数预算熔断
- 取消 / 硬超时
- 更稳的成功校验与进度事件（含可选缩略图）
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from utils.llm_client import call_llm
from utils.logger import setup_logger

from ..ai_explorer import AIExplorer, _extract_json_payload
from ..core.device_controller import DeviceController
from ..models import RecordingSession, UIAction, UISelector
from ..storage import RecordingStorage
from .device_tools import DeviceToolkit, goal_keywords
from .case_validator import CaseValidator

logger = setup_logger("ui_explore_agent")

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
CancelCheck = Optional[Callable[[], bool]]

MAX_PLANNED_STEPS = 30
MAX_STEP_RETRIES = 3
MAX_TOOL_BUDGET = 28
MAX_REPLANS = 2
DEFAULT_TIMEOUT_SEC = 600
STRONG_SELECTOR_STRATEGIES = ("resource_id", "text", "content_desc", "xpath")


class ExploreCancelled(Exception):
    """用户取消或超时。"""


class ExploreAgent:
    """闭环探索：失败可重试/换控件/滚动/重规划，仍固化为 RecordingSession。"""

    def __init__(self, storage: RecordingStorage):
        self.storage = storage
        self._explorer = AIExplorer(storage)

    def run(
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
        auto_diagnose: bool = True,
        cancel_check: CancelCheck = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        attach_previews: bool = True,
        auto_validate: bool = True,
    ) -> Dict[str, Any]:
        case_text = (case_text or "").strip()
        if not device_id:
            raise ValueError("device_id 必填")
        if not case_text:
            raise ValueError("case_text 必填（自然语言或分步用例）")

        controller = DeviceController(device_id)
        tools = DeviceToolkit(controller)
        budget = {"left": MAX_TOOL_BUDGET}
        deadline = (time.time() + float(timeout_sec)) if timeout_sec and timeout_sec > 0 else 0.0

        def _stop_if_needed():
            if cancel_check and cancel_check():
                raise ExploreCancelled("用户已取消探索")
            if deadline and time.time() > deadline:
                raise ExploreCancelled(f"探索超时（>{int(timeout_sec)}s）")

        if package_name:
            _stop_if_needed()
            self._emit(
                progress_callback,
                {
                    "phase": "launch",
                    "package_name": package_name,
                    "message": f"正在拉起 {package_name}",
                },
            )
            tools.launch_app(package_name)
            time.sleep(1.2)
            budget["left"] -= 1

        _stop_if_needed()
        planned = self._explorer._plan_steps(case_text)
        planned = self._ensure_assertion_steps(planned, case_text)
        recording_id = f"ai_{uuid.uuid4().hex[:12]}"
        session = RecordingSession(
            id=recording_id,
            device_id=device_id,
            package_name=package_name or "",
            created_at=datetime.now(),
            description=description or f"AI 闭环探索：{case_text[:120]}",
            project_id=project_id or "",
            name=name or (planned[0][:40] if planned else "AI 生成用例"),
        )

        agent_log: List[Dict[str, Any]] = []
        timeline: List[Dict[str, Any]] = []
        replan_count = 0
        step_num = 0
        goal_queue = list(planned)
        cancelled = False
        cancel_reason = ""

        self._emit(
            progress_callback,
            {
                "phase": "planned",
                "recording_id": recording_id,
                "planned_steps": list(goal_queue),
                "message": f"已规划 {len(goal_queue)} 步",
            },
        )
        timeline.append({"phase": "planned", "planned_steps": list(goal_queue)})

        try:
            while goal_queue:
                _stop_if_needed()
                if budget["left"] <= 0:
                    self._emit(
                        progress_callback,
                        {"phase": "budget_exhausted", "message": "工具预算用尽，提前结束"},
                    )
                    timeline.append({"phase": "budget_exhausted"})
                    break

                goal = goal_queue.pop(0)
                step_num += 1
                self._emit(
                    progress_callback,
                    {
                        "phase": "step_start",
                        "step_num": step_num,
                        "goal": goal,
                        "remaining": len(goal_queue),
                        "budget_left": budget["left"],
                        "message": f"第 {step_num} 步：{goal}",
                    },
                )

                step_result = self._resolve_step_with_retries(
                    tools=tools,
                    session=session,
                    step_num=step_num,
                    goal=goal,
                    case_context=case_text,
                    execute=execute_while_exploring,
                    budget=budget,
                    progress_callback=progress_callback,
                    stop_check=_stop_if_needed,
                    attach_preview=attach_previews and (step_num % 2 == 1),
                )
                agent_log.append(step_result)
                timeline.append(
                    {
                        "phase": "step",
                        "step_num": step_num,
                        "goal": goal,
                        "status": step_result.get("status"),
                        "attempts": step_result.get("attempts"),
                        "error": step_result.get("error"),
                    }
                )
                self._emit(
                    progress_callback,
                    {
                        "phase": "step_done",
                        "step_num": step_num,
                        "goal": goal,
                        "result": {
                            "status": step_result.get("status"),
                            "action_type": step_result.get("action_type"),
                            "description": step_result.get("description"),
                            "attempts": step_result.get("attempts"),
                            "error": step_result.get("error"),
                            "selector_strategy": step_result.get("selector_strategy"),
                        },
                        "message": (
                            f"第 {step_num} 步成功"
                            if step_result.get("status") == "ok"
                            else f"第 {step_num} 步失败：{step_result.get('error') or ''}"
                        ),
                    },
                )

                if step_result.get("status") == "ok":
                    wait_ms = int(step_result.get("wait_after") or 600)
                    if execute_while_exploring and wait_ms > 0:
                        time.sleep(min(wait_ms, 2500) / 1000.0)
                    continue

                if replan_count < MAX_REPLANS and goal_queue and budget["left"] > 2:
                    _stop_if_needed()
                    replan_count += 1
                    self._emit(
                        progress_callback,
                        {
                            "phase": "replan",
                            "step_num": step_num,
                            "message": f"步骤失败，正在重规划剩余 {len(goal_queue)} 步…",
                            "failed_goal": goal,
                            "error": step_result.get("error"),
                        },
                    )
                    new_remaining = self._replan_remaining(
                        case_text=case_text,
                        failed_goal=goal,
                        failed_error=step_result.get("error") or "",
                        remaining=goal_queue,
                        tools=tools,
                        budget=budget,
                    )
                    if new_remaining:
                        goal_queue = new_remaining
                        timeline.append(
                            {
                                "phase": "replan",
                                "after_step": step_num,
                                "new_remaining": list(goal_queue),
                            }
                        )
                        self._emit(
                            progress_callback,
                            {
                                "phase": "replanned",
                                "planned_steps_remaining": list(goal_queue),
                                "message": f"已重规划，剩余 {len(goal_queue)} 步",
                            },
                        )
        except ExploreCancelled as e:
            cancelled = True
            cancel_reason = str(e)
            self._emit(progress_callback, {"phase": "cancelled", "message": cancel_reason})
            timeline.append({"phase": "cancelled", "message": cancel_reason})

        ok = self.storage.save_recording(session)
        if not ok:
            raise RuntimeError("保存用例失败")

        failed_steps = [x for x in agent_log if x.get("status") != "ok"]
        diagnose = None
        validation = None
        regression_ready = False

        if (
            auto_validate
            and not cancelled
            and execute_while_exploring
            and any((a.status or "") == "completed" for a in session.actions)
        ):
            try:
                _stop_if_needed()
                self._emit(
                    progress_callback,
                    {
                        "phase": "validate_start",
                        "message": "开始强定位自跑验收…",
                    },
                )
                validator = CaseValidator(tools=tools)
                validation = validator.validate(
                    session,
                    relaunch=bool(package_name),
                    require_strong=True,
                    progress_callback=progress_callback,
                    stop_check=_stop_if_needed,
                )
                regression_ready = bool(validation.get("regression_ready"))
                timeline.append(
                    {
                        "phase": "validated",
                        "regression_ready": regression_ready,
                        "reason": validation.get("reason"),
                    }
                )
                session.meta = dict(session.meta or {})
                session.meta["validation"] = {
                    "ok": validation.get("ok"),
                    "regression_ready": regression_ready,
                    "reason": validation.get("reason"),
                    "passed": validation.get("passed"),
                    "failed": validation.get("failed"),
                    "weak_only": validation.get("weak_only"),
                    "steps": validation.get("steps") or [],
                }
                session.meta["regression_ready"] = regression_ready
                self.storage.save_recording(session)
            except ExploreCancelled as e:
                cancelled = True
                cancel_reason = str(e)
                self._emit(progress_callback, {"phase": "cancelled", "message": cancel_reason})
                timeline.append({"phase": "cancelled", "message": cancel_reason})
            except Exception as e:
                logger.warning(f"探索后验收失败: {e}")
                validation = {"ok": False, "regression_ready": False, "error": str(e)}
                self._emit(
                    progress_callback,
                    {"phase": "validate_error", "message": f"验收异常：{e}"},
                )

        if (
            auto_diagnose
            and failed_steps
            and session.actions
            and not cancelled
        ):
            try:
                from ..ai_diagnoser import AIDiagnoser

                first_fail = next(
                    (a for a in session.actions if (a.status or "") == "failed"),
                    None,
                )
                diagnoser = AIDiagnoser(self.storage)
                diagnose = diagnoser.diagnose_failure(
                    session.id,
                    device_id=device_id,
                    failed_step=first_fail.step_num if first_fail else None,
                    error=(failed_steps[0].get("error") or ""),
                    step_details=[],
                    dump_live_ui=True,
                )
                self._emit(
                    progress_callback,
                    {
                        "phase": "diagnosed",
                        "message": "已生成失败归因建议（需人工确认才可应用补丁）",
                        "needs_human_approval": True,
                    },
                )
                timeline.append({"phase": "diagnosed"})
            except Exception as e:
                logger.warning(f"探索后自动归因失败: {e}")
                diagnose = {"error": str(e)}

        if cancelled:
            message = f"探索已取消：{cancel_reason}（已保存 {len(session.actions)} 步）"
        elif regression_ready:
            message = "闭环探索完成并通过强定位自跑验收，可标为可回归"
        elif validation and not validation.get("ok"):
            message = (
                f"闭环探索完成，但验收未通过：{validation.get('reason') or validation.get('error') or ''}"
            )
        elif validation and validation.get("weak_only"):
            message = f"闭环探索完成，但存在弱定位：{validation.get('reason') or ''}"
        elif not failed_steps:
            message = "闭环探索完成，建议人工抽查定位策略后再跑套件"
        else:
            message = (
                f"闭环探索完成，有 {len(failed_steps)} 步失败，"
                "已附带归因建议（需人工确认）"
            )

        result = {
            "recording_id": recording_id,
            "name": session.name,
            "package_name": session.package_name,
            "step_count": len(session.actions),
            "planned_steps": planned,
            "actions": [a.to_dict() for a in session.actions],
            "agent_log": agent_log,
            "timeline": timeline,
            "failed_count": len(failed_steps),
            "replan_count": replan_count,
            "budget_left": budget["left"],
            "closed_loop": True,
            "cancelled": cancelled,
            "cancel_reason": cancel_reason or None,
            "needs_human_review": not regression_ready,
            "regression_ready": regression_ready,
            "validation": validation,
            "diagnose": diagnose,
            "message": message,
        }
        self._emit(
            progress_callback,
            {
                "phase": "done" if not cancelled else "cancelled_done",
                "recording_id": recording_id,
                "step_count": result["step_count"],
                "failed_count": result["failed_count"],
                "cancelled": cancelled,
                "regression_ready": regression_ready,
                "message": result["message"],
            },
        )
        return result

    @staticmethod
    def _ensure_assertion_steps(planned: List[str], case_text: str) -> List[str]:
        """若规划里没有断言/验证，补一条基于关键词的断言。"""
        steps = [str(s).strip() for s in (planned or []) if str(s).strip()]
        if not steps:
            return steps
        markers = ("断言", "验证", "检查", "确认出现", "应出现", "应该看到")
        if any(any(m in s for m in markers) for s in steps):
            return steps
        keys = goal_keywords(case_text) or goal_keywords(steps[-1])
        if not keys:
            return steps
        # 取最后一个有信息量的词
        key = keys[-1] if keys else ""
        if not key:
            return steps
        return steps + [f"断言：界面出现「{key}」"]

    # --- step loop ---

    def _resolve_step_with_retries(
        self,
        *,
        tools: DeviceToolkit,
        session: RecordingSession,
        step_num: int,
        goal: str,
        case_context: str,
        execute: bool,
        budget: Dict[str, int],
        progress_callback: ProgressCallback,
        stop_check: Optional[Callable[[], None]] = None,
        attach_preview: bool = False,
    ) -> Dict[str, Any]:
        exclude: Set[int] = set()
        last_error = ""
        last_decision: Dict[str, Any] = {}
        attempts_log: List[Dict[str, Any]] = []

        for attempt in range(1, MAX_STEP_RETRIES + 1):
            if stop_check:
                stop_check()
            if budget["left"] <= 0:
                break

            if attempt >= 2 and execute:
                self._emit(
                    progress_callback,
                    {
                        "phase": "retry",
                        "step_num": step_num,
                        "attempt": attempt,
                        "message": f"第 {step_num} 步第 {attempt} 次尝试：滚动后重找控件",
                    },
                )
                tools.scroll_down()
                budget["left"] -= 1
                time.sleep(0.4)

            dump = tools.dump_ui(exclude_indices=exclude)
            budget["left"] -= 1
            if not dump.get("ok"):
                last_error = dump.get("error") or "dump UI 失败"
                attempts_log.append({"attempt": attempt, "error": last_error})
                continue

            candidates = dump["candidates"]
            briefs = dump["briefs"]
            if not briefs and attempt < MAX_STEP_RETRIES:
                last_error = "当前屏无可用控件"
                attempts_log.append({"attempt": attempt, "error": last_error})
                continue

            before_fp = dump.get("fingerprint") or ""
            decision = self._explorer._llm_pick_action(
                goal, case_context, briefs, session.package_name
            )
            budget["left"] -= 1
            last_decision = decision

            built = self._build_action_from_decision(
                tools, dump["parser"], candidates, decision, goal
            )
            if not built.get("ok"):
                last_error = built.get("error") or "无法构建动作"
                ei = decision.get("element_index")
                try:
                    if ei is not None:
                        exclude.add(int(ei))
                except (TypeError, ValueError):
                    pass
                attempts_log.append(
                    {"attempt": attempt, "error": last_error, "decision": decision}
                )
                continue

            action_type = built["action_type"]
            coordinates = built.get("coordinates")
            value = built.get("value")
            wait_after = built.get("wait_after") or 800
            selector = built.get("selector")
            description = built.get("description") or goal
            picked_index = built.get("element_index")
            weak_locator = bool(built.get("weak_locator"))

            exec_ok = True
            exec_error = ""
            if execute:
                if action_type == "assertion":
                    # 断言不点击，只校验控件/文本存在
                    assert_val = value
                    if not assert_val and selector:
                        assert_val = f"exists:{selector.value}"
                    exec_ok, _, exec_error = tools.assert_selector(selector, assert_val)
                    tools.invalidate_dump_cache()
                else:
                    exec_ok, exec_error = self._explorer._execute_action(
                        tools.controller, action_type, coordinates, value, wait_after
                    )
                    tools.invalidate_dump_cache()
                budget["left"] -= 1

            verified = True
            verify_reason = ""
            if execute and exec_ok and action_type in ("click", "long_press", "swipe", "input"):
                verified, verify_reason = tools.verify_action_success(
                    action_type=action_type,
                    goal=goal,
                    before_fp=before_fp,
                    input_value=value,
                    settle_ms=400,
                )
                budget["left"] -= 1
                if not verified:
                    exec_error = exec_error or verify_reason or "步骤校验未通过"
                    if picked_index is not None:
                        exclude.add(int(picked_index))
            elif execute and exec_ok and action_type == "assertion":
                verify_reason = "assertion_ok"

            attempts_log.append(
                {
                    "attempt": attempt,
                    "action_type": action_type,
                    "description": description,
                    "exec_ok": exec_ok,
                    "verified": verified,
                    "verify_reason": verify_reason or None,
                    "weak_locator": weak_locator,
                    "error": exec_error or None,
                }
            )

            if exec_ok and verified:
                display = description
                strategy = (selector.strategy if selector else "") or ""
                if selector and strategy == "text":
                    display = f"{action_type}: {selector.value}"
                elif selector and strategy == "resource_id":
                    display = f"{action_type}: {str(selector.value).split('/')[-1]}"

                screenshot_path = self.storage.get_screenshot_path(session.id, step_num)
                try:
                    tools.controller.screenshot(screenshot_path)
                except Exception:
                    screenshot_path = None
                ui_tree_path = self.storage.get_ui_tree_path(session.id, step_num)
                try:
                    import os

                    os.makedirs(os.path.dirname(ui_tree_path), exist_ok=True)
                    with open(ui_tree_path, "w", encoding="utf-8") as f:
                        f.write(dump.get("xml") or "")
                except Exception:
                    ui_tree_path = None

                if attach_preview:
                    try:
                        thumb = tools.preview_jpeg_b64(max_width=280)
                        if thumb:
                            self._emit(
                                progress_callback,
                                {
                                    "phase": "preview",
                                    "step_num": step_num,
                                    "preview_image": thumb,
                                    "message": f"第 {step_num} 步界面预览",
                                },
                            )
                    except Exception:
                        pass

                action = UIAction(
                    step_num=step_num,
                    action_type=action_type,
                    selector=selector,
                    value=str(value) if value is not None else None,
                    coordinates=coordinates,
                    screenshot=screenshot_path,
                    ui_tree=ui_tree_path,
                    timestamp=time.time(),
                    wait_after=int(wait_after),
                    description=description,
                    display=display,
                    status="completed",
                )
                session.actions.append(action)
                self.storage.save_recording(session)
                return {
                    "status": "ok",
                    "goal": goal,
                    "action_type": action_type,
                    "description": description,
                    "selector": selector.to_dict() if selector else None,
                    "selector_strategy": strategy,
                    "weak_locator": weak_locator,
                    "wait_after": wait_after,
                    "verify_reason": verify_reason,
                    "attempts": attempts_log,
                    "decision": decision,
                    "abort": False,
                }

            last_error = exec_error or "执行失败"
            if picked_index is not None:
                try:
                    exclude.add(int(picked_index))
                except (TypeError, ValueError):
                    pass

        screenshot_path = self.storage.get_screenshot_path(session.id, step_num)
        try:
            tools.controller.screenshot(screenshot_path)
        except Exception:
            screenshot_path = None
        fail_action = UIAction(
            step_num=step_num,
            action_type=(last_decision.get("action_type") or "click"),
            selector=UISelector(strategy="coordinates", value="0,0"),
            value=None,
            coordinates=None,
            screenshot=screenshot_path,
            ui_tree=None,
            timestamp=time.time(),
            wait_after=0,
            description=goal,
            display=f"失败: {goal}",
            status="failed",
        )
        session.actions.append(fail_action)
        self.storage.save_recording(session)
        return {
            "status": "failed",
            "goal": goal,
            "error": last_error or "步骤失败",
            "attempts": attempts_log,
            "decision": last_decision,
            "abort": False,
        }

    def _build_action_from_decision(
        self,
        tools: DeviceToolkit,
        parser,
        candidates: List,
        decision: Dict[str, Any],
        goal: str,
    ) -> Dict[str, Any]:
        action_type = (decision.get("action_type") or "click").lower().strip()
        wait_after = int(decision.get("wait_after") or 1000)
        description = (decision.get("description") or goal).strip()
        value = decision.get("value")
        element_index = decision.get("element_index")

        selector = None
        coordinates = None
        picked_index = None
        weak_locator = False

        if action_type in ("click", "long_press", "input", "assertion") and element_index is not None:
            try:
                ei = int(element_index)
                if 0 <= ei < len(candidates):
                    picked = candidates[ei]
                    picked_index = ei
                    selector = tools.build_selector(parser, picked)
                    if picked.bounds:
                        coordinates = {
                            "x": picked.bounds["center_x"],
                            "y": picked.bounds["center_y"],
                        }
                    if action_type == "assertion" and not value:
                        # 固化为 exists:文本|id，便于脚本与验收
                        label = picked.text or picked.content_desc or picked.resource_id or ""
                        value = f"exists:{label}" if label else "exists:"
            except (TypeError, ValueError):
                pass

        # 目标像「断言/验证」但 LLM 没标 assertion 时纠正
        if action_type != "assertion" and any(
            m in (goal or "") for m in ("断言", "验证", "检查出现", "应出现", "应该看到")
        ):
            if selector or element_index is not None:
                action_type = "assertion"
                if not value and selector:
                    value = f"exists:{selector.value}"
            elif not value:
                # 无控件时用目标关键词做文本断言
                keys = goal_keywords(goal)
                if keys:
                    action_type = "assertion"
                    value = f"text_contains:{keys[-1]}"
                    selector = selector or UISelector(strategy="text", value=keys[-1])
                    weak_locator = False

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
            weak_locator = True
        elif action_type == "wait":
            wait_after = int(value or wait_after or 1000)
            selector = UISelector(strategy="coordinates", value="0,0")
        elif action_type == "key":
            selector = UISelector(strategy="coordinates", value=str(value or "4"))
        elif not selector:
            cx = decision.get("x")
            cy = decision.get("y")
            if cx is not None and cy is not None:
                coordinates = {"x": int(cx), "y": int(cy)}
                selector = UISelector(strategy="coordinates", value=f"{cx},{cy}")
                weak_locator = True
            else:
                return {"ok": False, "error": "LLM 未给出可用控件或坐标"}

        if selector and selector.strategy not in STRONG_SELECTOR_STRATEGIES:
            if action_type in ("click", "long_press", "input", "assertion"):
                weak_locator = True

        return {
            "ok": True,
            "action_type": action_type,
            "selector": selector,
            "coordinates": coordinates,
            "value": value,
            "wait_after": wait_after,
            "description": description,
            "element_index": picked_index,
            "weak_locator": weak_locator,
        }

    def _replan_remaining(
        self,
        *,
        case_text: str,
        failed_goal: str,
        failed_error: str,
        remaining: List[str],
        tools: DeviceToolkit,
        budget: Dict[str, int],
    ) -> List[str]:
        dump = tools.dump_ui()
        budget["left"] -= 1
        briefs = (dump.get("briefs") or [])[:30]
        prompt = (
            "你是 Android UI 自动化重规划助手。上一步失败了，请根据当前界面重写「剩余步骤」。\n"
            "只输出 JSON 数组（字符串步骤），最多 12 步；不要重复已成功的前半段。\n"
            f"整段用例：{case_text[:400]}\n"
            f"失败步骤：{failed_goal}\n"
            f"失败原因：{failed_error}\n"
            f"原剩余步骤：{json.dumps(remaining, ensure_ascii=False)}\n"
            f"当前控件摘要：{json.dumps(briefs, ensure_ascii=False)}\n"
        )
        try:
            raw = call_llm(
                [{"role": "user", "content": prompt}],
                timeout=60,
                temperature=0.2,
                max_tokens=800,
            )
            budget["left"] -= 1
            data = _extract_json_payload(raw)
            if isinstance(data, list):
                steps = [str(x).strip() for x in data if str(x).strip()]
                if steps:
                    return steps[:MAX_PLANNED_STEPS]
        except Exception as e:
            logger.warning(f"重规划失败，保留原剩余步骤: {e}")
        return remaining

    @staticmethod
    def _emit(cb: ProgressCallback, event: Dict[str, Any]) -> None:
        if not cb:
            return
        try:
            event = dict(event)
            event.setdefault("ts", time.time())
            cb(event)
        except Exception:
            pass
