#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#28 regex 告警规则单元测试。

覆盖：
- regex 分支真实生效（此前缺失，规则静默永不触发）
- 非法正则安全降级（不抛异常、不匹配）
- 编译缓存生效
- validate_rule_pattern 保存预检口径
- 引擎级端到端：regex 规则能产出告警
- 既有类型（keyword/exception/crash/level）行为不回归
"""

import unittest

from modules.log_monitor.alert_engine import (
    AlertEngine,
    AlertRule,
    validate_rule_pattern,
    _get_compiled_regex,
    _REGEX_CACHE,
)

FATAL_LINE = "07-29 10:00:00.123  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main"
NPE_LINE = "07-29 10:00:00.200  1234  1234 E AndroidRuntime: java.lang.NullPointerException: null obj"
NORMAL_LINE = "07-29 10:00:01.000  2000  2000 I ActivityManager: Displayed com.demo/.MainActivity"


class TestRegexRuleMatch(unittest.TestCase):
    """AlertRule.matches 的 regex 分支"""

    def _rule(self, pattern, rule_type="regex"):
        return AlertRule(id="r1", name="测试", type=rule_type,
                         pattern=pattern, severity="high")

    def test_regex_basic_match(self):
        """基础正则命中：此前该分支缺失，恒 False"""
        rule = self._rule(r"FATAL\s+EXCEPTION")
        self.assertTrue(rule.matches(FATAL_LINE))
        self.assertFalse(rule.matches(NORMAL_LINE))

    def test_regex_case_insensitive(self):
        """与 keyword 分支口径一致：忽略大小写"""
        rule = self._rule(r"fatal\s+exception")
        self.assertTrue(rule.matches(FATAL_LINE))

    def test_regex_complex_pattern(self):
        """复杂正则：捕获组/字符类/量词"""
        rule = self._rule(r"E\s+AndroidRuntime:\s+(java\.lang\.\w+Exception)")
        self.assertTrue(rule.matches(NPE_LINE))
        self.assertFalse(rule.matches(NORMAL_LINE))

    def test_invalid_regex_safe_degrade(self):
        """非法正则：不抛异常、恒不匹配（安全降级）"""
        rule = self._rule(r"[unclosed")
        try:
            result = rule.matches(FATAL_LINE)
        except Exception as e:
            self.fail(f"非法正则不应抛异常: {e}")
        self.assertFalse(result)

    def test_disabled_rule_never_matches(self):
        rule = self._rule(r"FATAL")
        rule.enabled = False
        self.assertFalse(rule.matches(FATAL_LINE))

    def test_regex_cache_effective(self):
        """编译缓存：同 pattern 第二次直接取缓存对象"""
        pattern = r"CACHE_TEST_\d+"
        _REGEX_CACHE.pop(pattern, None)
        c1 = _get_compiled_regex(pattern)
        c2 = _get_compiled_regex(pattern)
        self.assertIsNotNone(c1)
        self.assertIs(c1, c2)

    def test_invalid_regex_cached_as_none(self):
        pattern = r"([bad"
        _REGEX_CACHE.pop(pattern, None)
        self.assertIsNone(_get_compiled_regex(pattern))
        self.assertIn(pattern, _REGEX_CACHE)


class TestValidateRulePattern(unittest.TestCase):
    """保存前预检 validate_rule_pattern"""

    def test_valid_regex_passes(self):
        self.assertIsNone(validate_rule_pattern("regex", r"FATAL\s+EXCEPTION"))

    def test_invalid_regex_rejected(self):
        err = validate_rule_pattern("regex", r"[unclosed")
        self.assertIsNotNone(err)
        self.assertIn("正则表达式非法", err)

    def test_empty_regex_rejected(self):
        self.assertIsNotNone(validate_rule_pattern("regex", "   "))

    def test_keyword_type_not_enforced(self):
        """keyword 类型不强制正则合法（会自动降级为字符串包含）"""
        self.assertIsNone(validate_rule_pattern("keyword", r"[unclosed"))

    def test_other_types_not_enforced(self):
        for t in ("exception", "anr", "crash", "level", "frequency", ""):
            self.assertIsNone(validate_rule_pattern(t, "whatever"))


class TestEngineEndToEnd(unittest.TestCase):
    """引擎级：regex 规则能真实产出告警"""

    def setUp(self):
        self.engine = AlertEngine()
        # 清掉默认规则，只留待测 regex 规则，避免干扰
        self.engine.rules.clear()

    def test_regex_rule_fires_alert(self):
        self.engine.add_rule(AlertRule(
            id="rx1", name="播放器超时", type="regex",
            pattern=r"PlayerManager.*timeout\s*=\s*\d{4,}", severity="high",
        ))
        line = "07-29 11:00:00.000 3000 3000 E PlayerManager: prepare timeout=8000 ms url=http://x"
        alerts = self.engine.check_log(line, device_id="dev1")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].rule_id, "rx1")
        self.assertEqual(alerts[0].type, "regex")

    def test_regex_rule_no_false_fire(self):
        self.engine.add_rule(AlertRule(
            id="rx2", name="超时", type="regex",
            pattern=r"timeout=\d{4,}", severity="medium",
        ))
        alerts = self.engine.check_log(
            "07-29 11:00:00.000 3000 3000 I PlayerManager: prepare ok in 120 ms",
            device_id="dev1",
        )
        self.assertEqual(alerts, [])

    def test_invalid_regex_rule_never_fires_but_no_crash(self):
        self.engine.add_rule(AlertRule(
            id="rx3", name="坏规则", type="regex",
            pattern=r"([bad", severity="high",
        ))
        try:
            alerts = self.engine.check_log(FATAL_LINE, device_id="dev1")
        except Exception as e:
            self.fail(f"非法正则规则不应让引擎崩溃: {e}")
        # 坏规则不触发（FATAL_LINE 若命中其他规则也已被 clear 掉）
        self.assertEqual([a for a in alerts if a.rule_id == "rx3"], [])


class TestExistingTypesNoRegression(unittest.TestCase):
    """既有匹配类型行为不回归"""

    def test_keyword_still_works(self):
        rule = AlertRule(id="k1", name="NPE", type="keyword",
                         pattern="NullPointerException", severity="medium")
        self.assertTrue(rule.matches(NPE_LINE))

    def test_exception_still_works(self):
        rule = AlertRule(id="e1", name="异常", type="exception",
                         pattern="", severity="high")
        self.assertTrue(rule.matches(FATAL_LINE))

    def test_crash_still_works(self):
        rule = AlertRule(id="c1", name="崩溃", type="crash",
                         pattern="", severity="high")
        self.assertTrue(rule.matches(FATAL_LINE))

    def test_unknown_type_still_false(self):
        rule = AlertRule(id="u1", name="未知", type="nonexistent",
                         pattern="FATAL", severity="low")
        self.assertFalse(rule.matches(FATAL_LINE))


if __name__ == "__main__":
    unittest.main()
