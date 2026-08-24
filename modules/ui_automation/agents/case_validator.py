"""探索后自跑验收：用强定位重放，判断用例是否可回归。"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logger import setup_logger

from ..core.device_controller import DeviceController
from ..models import RecordingSession, UIAction, UISelector
from .device_tools import DeviceToolkit

logger = setup_logger("ui_case_validator")

STRONG = ("resource_id", "text", "content_desc", "xpath")
ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]


class CaseValidator:
    """在真机上按已固化动作重放；默认拒绝纯坐标作为可回归。"""

    def __init__(self, controller: Optional[DeviceController] = None, tools: Optional[DeviceToolkit] = None):
        if tools is not None:
            self.tools = tools
        elif controller is not None:
            self.tools = DeviceToolkit(controller)
        else:
            raise ValueError("controller 或 tools 必填其一")

    def validate(
        self,
        session: RecordingSession,
        *,
        relaunch: bool = True,
        require_strong: bool = True,
        progress_callback: ProgressCallback = None,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        actions = [a for a in (session.actions or []) if (a.status or "") != "failed"]
        if not actions:
            return {
                "ok": False,
                "regression_ready": False,
                "reason": "无可验收步骤（无成功固化动作）",
                "steps": [],
                "passed": 0,
                "failed": 0,
                "weak_only": 0,
            }

        if relaunch and session.package_name:
            self._emit(progress_callback, {"phase": "validate_launch", "message": "验收前重新拉起应用"})
            self.tools.launch_app(session.package_name)
            time.sleep(1.5)

        steps: List[Dict[str, Any]] = []
        passed = 0
        failed = 0
        weak_only = 0

        for action in actions:
            if stop_check:
                stop_check()
            self._emit(
                progress_callback,
                {
                    "phase": "validate_step",
                    "step_num": action.step_num,
                    "message": f"验收第 {action.step_num} 步：{action.description or action.action_type}",
                },
            )
            ok, detail = self._replay_one(action, require_strong=require_strong)
            steps.append(detail)
            if detail.get("weak_only"):
                weak_only += 1
            if ok:
                passed += 1
            else:
                failed += 1
                break

            wait_ms = int(action.wait_after or 0)
            if wait_ms > 0 and action.action_type != "wait":
                time.sleep(min(wait_ms, 2500) / 1000.0)

        regression_ready = failed == 0 and weak_only == 0 and passed > 0
        reason = ""
        if failed:
            reason = (steps[-1].get("error") if steps else None) or "验收失败"
        elif weak_only:
            reason = f"有 {weak_only} 步仅靠坐标/弱定位，不可标为可回归"
        elif regression_ready:
            reason = "强定位重放全部通过"
        else:
            reason = "验收未通过"

        result = {
            "ok": failed == 0,
            "regression_ready": regression_ready,
            "reason": reason,
            "steps": steps,
            "passed": passed,
            "failed": failed,
            "weak_only": weak_only,
            "total": len(actions),
        }
        self._emit(
            progress_callback,
            {
                "phase": "validated",
                "message": (
                    "验收通过，可回归"
                    if regression_ready
                    else (f"验收完成：{reason}")
                ),
                "regression_ready": regression_ready,
                "validation": {
                    "ok": result["ok"],
                    "passed": passed,
                    "failed": failed,
                    "weak_only": weak_only,
                },
            },
        )
        return result

    def _replay_one(self, action: UIAction, *, require_strong: bool) -> Tuple[bool, Dict[str, Any]]:
        at = (action.action_type or "").lower()
        detail: Dict[str, Any] = {
            "step_num": action.step_num,
            "action_type": at,
            "description": action.description or action.display or "",
            "ok": False,
            "strategy_used": None,
            "weak_only": False,
            "error": None,
        }

        try:
            if at == "wait":
                ms = int(action.value or action.wait_after or 500)
                time.sleep(max(ms, 0) / 1000.0)
                detail["ok"] = True
                detail["strategy_used"] = "wait"
                return True, detail

            if at == "key":
                ok, err = self.tools.key(int(action.value or 4))
                detail["ok"] = ok
                detail["strategy_used"] = "key"
                detail["error"] = err or None
                return ok, detail

            if at == "swipe":
                c = action.coordinates or {}
                ok, err = self.tools.swipe(
                    int(c.get("x") or 0),
                    int(c.get("y") or 0),
                    int(c.get("x2") or 0),
                    int(c.get("y2") or 0),
                )
                detail["ok"] = ok
                detail["strategy_used"] = "coordinates"
                detail["weak_only"] = True
                detail["error"] = err or None
                if require_strong:
                    detail["ok"] = False
                    detail["error"] = "滑动仅坐标策略，验收要求强定位"
                    return False, detail
                return ok, detail

            if at == "assertion":
                ok, used, err = self.tools.assert_selector(action.selector, action.value)
                detail["ok"] = ok
                detail["strategy_used"] = used
                detail["error"] = err or None
                if ok and used and used not in STRONG:
                    detail["weak_only"] = True
                return ok, detail

            # click / long_press / input
            strategies = _selector_strategies(action.selector)
            strong_first = [s for s in strategies if s[0] in STRONG]
            weak = [s for s in strategies if s[0] not in STRONG]
            ordered = strong_first + ([] if require_strong else weak)

            if not ordered and action.coordinates and not require_strong:
                ordered = [("coordinates", f"{action.coordinates.get('x')},{action.coordinates.get('y')}")]

            if not ordered:
                detail["error"] = "无强定位策略可验收（仅有坐标或空选择器）"
                detail["weak_only"] = True
                return False, detail

            last_err = ""
            for strategy, value in ordered:
                if at == "input":
                    ok, used, err = self.tools.act_by_strategy(
                        "input", strategy, value, input_value=action.value, coordinates=action.coordinates
                    )
                else:
                    ok, used, err = self.tools.act_by_strategy(
                        "click", strategy, value, coordinates=action.coordinates
                    )
                if ok:
                    detail["ok"] = True
                    detail["strategy_used"] = used or strategy
                    if (used or strategy) not in STRONG:
                        detail["weak_only"] = True
                    return True, detail
                last_err = err or last_err

            detail["error"] = last_err or "强定位重放失败"
            return False, detail
        except Exception as e:
            detail["error"] = str(e)
            return False, detail

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


def _selector_strategies(selector: Optional[UISelector]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if not selector:
        return out
    if selector.strategy and selector.value is not None:
        out.append((selector.strategy, str(selector.value)))
    for fb in selector.fallbacks or []:
        st = fb.get("strategy") or ""
        val = fb.get("value")
        if not st or val is None:
            continue
        pair = (st, str(val))
        if pair not in out:
            out.append(pair)
    return out
