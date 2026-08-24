"""设备侧工具：供 UI 闭环探索 Agent 调用（仍走 uiautomator2 / ADB）。"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import setup_logger

from ..core.device_controller import DeviceController
from ..core.element_locator import ElementLocator
from ..core.ui_tree_parser import UIElement, UITreeParser
from ..models import UISelector

logger = setup_logger("ui_explore_device_tools")

MAX_ELEMENTS = 80


def element_brief(idx: int, el: UIElement) -> Dict[str, Any]:
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


def collect_candidates(parser: UITreeParser, exclude_indices: Optional[set] = None) -> List[UIElement]:
    exclude_indices = exclude_indices or set()
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
        if score <= 0 or not el.bounds:
            continue
        scored.append((score, el))
    scored.sort(key=lambda x: (-x[0], x[1].bounds["center_y"] if x[1].bounds else 0))
    return [el for _, el in scored[:MAX_ELEMENTS]]


def goal_keywords(goal: str) -> List[str]:
    """从目标句提取用于校验的关键词。"""
    raw = (goal or "").strip()
    if not raw:
        return []
    stop = {
        "打开", "点击", "点", "进入", "选择", "输入", "搜索", "滑动", "返回",
        "页面", "按钮", "一下", "然后", "并且", "的", "了", "在", "到", "并",
        "第一", "第二个", "一下", "进行",
    }
    keys: List[str] = []
    for p in re.split(r"[\s,，。；;、/\\|]+", raw):
        p = p.strip()
        if len(p) < 2 or p in stop:
            continue
        for s in stop:
            p = p.replace(s, "")
        p = p.strip()
        if len(p) >= 2 and p not in keys:
            keys.append(p)
    for m in re.findall(r"[\u4e00-\u9fff]{2,8}", raw):
        if m not in stop and m not in keys:
            keys.append(m)
    return keys[:8]


class DeviceToolkit:
    """封装 dump / tap / swipe / input / key / scroll / launch / verify。"""

    def __init__(self, controller: DeviceController):
        self.controller = controller
        self._last_xml_fingerprint: str = ""
        self._dump_cache: Optional[Dict[str, Any]] = None
        self._dump_cache_at: float = 0.0

    def dump_ui(
        self,
        exclude_indices: Optional[set] = None,
        *,
        use_cache_ms: int = 0,
    ) -> Dict[str, Any]:
        now = time.time()
        if (
            use_cache_ms > 0
            and self._dump_cache
            and (now - self._dump_cache_at) * 1000 <= use_cache_ms
        ):
            cached = dict(self._dump_cache)
            candidates = cached.get("candidates") or []
            briefs = []
            for i, el in enumerate(candidates):
                if exclude_indices and i in exclude_indices:
                    continue
                briefs.append(element_brief(i, el))
            cached["briefs"] = briefs
            return cached

        xml = self.controller.get_ui_tree() or ""
        if not xml:
            return {"ok": False, "error": "无法获取 UI 树", "candidates": [], "briefs": [], "xml": ""}
        parser = UITreeParser(xml)
        candidates = collect_candidates(parser)
        briefs = []
        for i, el in enumerate(candidates):
            if exclude_indices and i in exclude_indices:
                continue
            briefs.append(element_brief(i, el))
        fp = self._fingerprint(briefs)
        self._last_xml_fingerprint = fp
        result = {
            "ok": True,
            "xml": xml,
            "parser": parser,
            "candidates": candidates,
            "briefs": briefs,
            "fingerprint": fp,
            "texts": self._collect_texts(candidates),
        }
        self._dump_cache = result
        self._dump_cache_at = now
        return result

    def invalidate_dump_cache(self) -> None:
        self._dump_cache = None
        self._dump_cache_at = 0.0

    @staticmethod
    def _collect_texts(candidates: List[UIElement]) -> List[str]:
        texts = []
        for el in candidates:
            for t in (el.text, el.content_desc, el.resource_id):
                if t:
                    texts.append(str(t)[:80])
        return texts

    @staticmethod
    def _fingerprint(briefs: List[Dict[str, Any]]) -> str:
        parts = []
        for b in briefs[:40]:
            parts.append(f"{b.get('i')}:{b.get('id') or ''}:{b.get('text') or ''}:{b.get('cx')}:{b.get('cy')}")
        return "|".join(parts)

    def tap(self, x: int, y: int) -> Tuple[bool, str]:
        self.invalidate_dump_cache()
        ok = self.controller.click(int(x), int(y))
        return ok, "" if ok else (self.controller.last_output or "点击失败")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> Tuple[bool, str]:
        self.invalidate_dump_cache()
        ok = self.controller.swipe(int(x1), int(y1), int(x2), int(y2), int(duration))
        return ok, "" if ok else (self.controller.last_output or "滑动失败")

    def input_text(self, text: str, tap_xy: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        self.invalidate_dump_cache()
        if tap_xy:
            self.controller.click(int(tap_xy[0]), int(tap_xy[1]))
            time.sleep(0.25)
        ok = self.controller.input_text(str(text or ""))
        return ok, "" if ok else (self.controller.last_output or "输入失败")

    def key(self, key_code: int) -> Tuple[bool, str]:
        self.invalidate_dump_cache()
        ok = self.controller.press_key(int(key_code))
        return ok, "" if ok else (self.controller.last_output or "按键失败")

    def scroll_down(self) -> Tuple[bool, str]:
        w, h = self.controller.get_display_size()
        if w <= 0 or h <= 0:
            w, h = 1080, 1920
        x = w // 2
        y1 = int(h * 0.72)
        y2 = int(h * 0.28)
        return self.swipe(x, y1, x, y2, 400)

    def scroll_up(self) -> Tuple[bool, str]:
        w, h = self.controller.get_display_size()
        if w <= 0 or h <= 0:
            w, h = 1080, 1920
        x = w // 2
        y1 = int(h * 0.28)
        y2 = int(h * 0.72)
        return self.swipe(x, y1, x, y2, 400)

    def launch_app(self, package_name: str) -> None:
        pkg = (package_name or "").strip()
        if not pkg:
            return
        self.invalidate_dump_cache()
        self.controller._run_adb_command(
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

    def ui_changed_since(self, before_fp: str, settle_ms: int = 500) -> bool:
        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)
        dump = self.dump_ui()
        if not dump.get("ok"):
            return False
        return (dump.get("fingerprint") or "") != (before_fp or "")

    def verify_action_success(
        self,
        *,
        action_type: str,
        goal: str,
        before_fp: str,
        input_value: Any = None,
        settle_ms: int = 400,
    ) -> Tuple[bool, str]:
        """
        比单纯指纹更稳的成功判断：
        - wait/key/assertion：执行成功即通过
        - input：界面出现输入文本，或指纹变化
        - click/swipe：指纹变化，或目标关键词出现在新界面
        - 同页点击（勾选等）指纹不变时弱通过，避免误杀
        """
        at = (action_type or "").lower()
        if at in ("wait", "key", "assertion"):
            return True, "skip_verify"

        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)
        after = self.dump_ui()
        if not after.get("ok"):
            return True, "dump_failed_assume_ok"

        after_fp = after.get("fingerprint") or ""
        after_texts = after.get("texts") or []
        joined = " ".join(after_texts)

        if at == "input" and input_value:
            needle = str(input_value).strip()
            if needle and needle in joined:
                return True, "input_text_visible"
            if after_fp != (before_fp or ""):
                return True, "ui_changed"
            return False, "输入后未看到文本且界面未变"

        if after_fp != (before_fp or ""):
            return True, "ui_changed"

        keywords = goal_keywords(goal)
        if keywords:
            hit = [k for k in keywords if k in joined]
            if hit:
                return True, f"goal_keyword_visible:{','.join(hit[:3])}"

        if at in ("click", "long_press"):
            return True, "same_page_click_accepted"

        return False, "界面未见变化且未匹配目标关键词"

    def preview_jpeg_b64(self, max_width: int = 360) -> Optional[str]:
        """探索进度用小图，失败忽略。"""
        try:
            from ..core.preview_image import encode_preview_payload

            png = self.controller.screenshot_png_bytes(timeout=12)
            if not png:
                return None
            payload = encode_preview_payload(png, max_width=max_width, quality=45)
            return (payload or {}).get("image")
        except Exception as e:
            logger.debug(f"preview_jpeg_b64 failed: {e}")
            return None

    def build_selector(self, parser: UITreeParser, el: UIElement):
        locator = ElementLocator(parser)
        return locator._build_selector(el)

    def find_by_strategy(
        self,
        strategy: str,
        value: str,
        *,
        dump: Optional[Dict[str, Any]] = None,
    ) -> Optional[UIElement]:
        dump = dump or self.dump_ui()
        if not dump.get("ok"):
            return None
        candidates: List[UIElement] = dump.get("candidates") or []
        st = (strategy or "").lower()
        needle = str(value or "")
        for el in candidates:
            if st == "resource_id" and el.resource_id and (
                el.resource_id == needle or el.resource_id.endswith(needle)
            ):
                return el
            if st == "text" and el.text and (el.text == needle or needle in el.text):
                return el
            if st == "content_desc" and el.content_desc and (
                el.content_desc == needle or needle in el.content_desc
            ):
                return el
            if st == "xpath":
                # dump 候选已过滤；xpath 精确匹配留给 u2，这里用文本/id 近似
                if needle and (
                    (el.resource_id and needle in el.resource_id)
                    or (el.text and needle in el.text)
                ):
                    return el
            if st == "coordinates" and el.bounds and "," in needle:
                try:
                    x_s, y_s = needle.split(",", 1)
                    x, y = int(float(x_s)), int(float(y_s))
                    b = el.bounds
                    if b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]:
                        return el
                except (TypeError, ValueError):
                    pass
        return None

    def assert_selector(
        self,
        selector: Optional[UISelector],
        value: Any = None,
    ) -> Tuple[bool, Optional[str], str]:
        """断言控件存在或文本出现。返回 (ok, strategy_used, error)。"""
        dump = self.dump_ui()
        if not dump.get("ok"):
            return False, None, dump.get("error") or "dump UI 失败"

        expected = str(value or "").strip()
        assertion_type = "exists"
        expected_text = expected
        if ":" in expected:
            assertion_type, expected_text = expected.split(":", 1)
            assertion_type = assertion_type.strip().lower()
            expected_text = expected_text.strip()

        joined = " ".join(dump.get("texts") or [])

        if assertion_type == "text_contains" and expected_text:
            if expected_text in joined:
                return True, "text_contains", ""
            return False, None, f"界面未出现文本：{expected_text}"

        if assertion_type == "not_exists":
            # 有 selector 则找不到才过
            if selector:
                el = self.find_by_strategy(selector.strategy, selector.value, dump=dump)
                if el is None:
                    return True, selector.strategy, ""
                return False, None, "断言不存在失败：控件仍在"
            if expected_text and expected_text not in joined:
                return True, "text_absent", ""
            return False, None, f"断言不存在失败：仍看到 {expected_text}"

        # exists / default：优先 selector，其次期望文本
        if selector and selector.strategy:
            el = self.find_by_strategy(selector.strategy, str(selector.value), dump=dump)
            if el is not None:
                return True, selector.strategy, ""
            for fb in selector.fallbacks or []:
                st = fb.get("strategy") or ""
                val = fb.get("value")
                if not st or val is None:
                    continue
                el = self.find_by_strategy(st, str(val), dump=dump)
                if el is not None:
                    return True, st, ""

        if expected_text and expected_text in joined:
            return True, "text_contains", ""

        # 从 selector value 再试文本
        if selector and selector.value and str(selector.value) in joined:
            return True, "text_contains", ""

        return False, None, "断言失败：未找到目标控件/文本"

    def act_by_strategy(
        self,
        action_type: str,
        strategy: str,
        value: str,
        *,
        input_value: Any = None,
        coordinates: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """按策略定位并执行 click/input。返回 (ok, strategy_used, error)。"""
        at = (action_type or "click").lower()
        st = (strategy or "").lower()

        if st == "coordinates":
            try:
                if coordinates:
                    x, y = int(coordinates["x"]), int(coordinates["y"])
                else:
                    x_s, y_s = str(value).split(",", 1)
                    x, y = int(float(x_s)), int(float(y_s))
            except (TypeError, ValueError, KeyError):
                return False, None, "坐标无效"
            if at == "input":
                ok, err = self.input_text(str(input_value or ""), tap_xy=(x, y))
                return ok, "coordinates", err
            ok, err = self.tap(x, y)
            return ok, "coordinates", err

        dump = self.dump_ui()
        el = self.find_by_strategy(st, value, dump=dump)
        if el is None or not el.bounds:
            return False, None, f"未找到控件 strategy={st}"

        x, y = int(el.bounds["center_x"]), int(el.bounds["center_y"])
        if at == "input":
            ok, err = self.input_text(str(input_value or ""), tap_xy=(x, y))
            return ok, st, err
        ok, err = self.tap(x, y)
        return ok, st, err
