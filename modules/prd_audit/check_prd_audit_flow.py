#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRD 审计流程自检：JSON 抽取、配置加载、offline_mode 判定等。
在项目根目录执行: python -m modules.prd_audit.check_prd_audit_flow
"""
import json
import os
import sys
import tempfile

# 确保项目根在 path 中
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

def test_extract_first_json_system_model():
    """system_model._extract_first_json_object：markdown 代码块、多段 JSON、纯 JSON"""
    from modules.prd_audit.system_model import _extract_first_json_object
    ok = 0
    # 1) 纯 JSON
    r = _extract_first_json_object('{"a":1,"b":"x"}')
    assert r.get("a") == 1 and r.get("b") == "x", r
    ok += 1
    # 2) ```json ... ```
    text = '说明如下：\n```json\n{"defects":[{"type":"A"}]}\n```'
    r = _extract_first_json_object(text)
    assert isinstance(r.get("defects"), list) and r["defects"][0].get("type") == "A", r
    ok += 1
    # 3) 多段 JSON（只取第一段，避免 Extra data）
    text = '{"first":1}\n{"second":2}'
    r = _extract_first_json_object(text)
    assert r.get("first") == 1 and "second" not in r, r
    ok += 1
    # 4) 无 { }
    r = _extract_first_json_object("no json here")
    assert r == {}, r
    ok += 1
    print("[OK] system_model._extract_first_json_object: %d cases" % ok)


def test_extract_first_json_llm_client():
    """utils.llm_client._extract_first_json_object：配置用"""
    from utils.llm_client import _extract_first_json_object
    r = _extract_first_json_object('{"api_key":"sk-xxx","model":"deepseek-chat"}')
    assert r.get("api_key") == "sk-xxx", r
    print("[OK] llm_client._extract_first_json_object")


def test_load_llm_config_robust():
    """load_llm_config：正常 JSON + 双段 JSON 容错"""
    from utils.llm_client import load_llm_config
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write('{"api_key":"sk-test","model":"deepseek-chat"}')
        path = f.name
    try:
        cfg = load_llm_config(path)
        assert cfg.get("api_key") == "sk-test", cfg
        # 双段 JSON（模拟损坏文件）
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"api_key":"sk-first"}\n{"api_key":"sk-second"}')
        cfg2 = load_llm_config(path)
        assert cfg2.get("api_key") == "sk-first", cfg2
        print("[OK] load_llm_config: normal + double-JSON fallback")
    finally:
        os.unlink(path)


def test_pick_llm_config_path():
    """_pick_llm_config_path：统一使用平台级 config/llm_config.json"""
    from modules.prd_audit.views import _pick_llm_config_path
    path = _pick_llm_config_path()
    assert "config" in path.replace("\\", "/") and path.endswith("llm_config.json"), path
    print("[OK] _pick_llm_config_path: %s" % path)


def test_offline_mode_condition():
    """offline_mode 仅当缺陷列表中存在 扫描引擎+扫描异常"""
    # 有该缺陷 -> offline
    defects_with_scan_err = [
        {"module": "扫描引擎", "type": "扫描异常", "description": "漏洞扫描阶段执行失败"},
    ]
    offline = any(
        isinstance(d, dict) and str(d.get("module") or "") == "扫描引擎" and str(d.get("type") or "") == "扫描异常"
        for d in defects_with_scan_err
    )
    assert offline is True
    # 无该缺陷 -> 非 offline
    defects_clean = [{"module": "订单模块", "type": "逻辑矛盾"}]
    offline2 = any(
        isinstance(d, dict) and str(d.get("module") or "") == "扫描引擎" and str(d.get("type") or "") == "扫描异常"
        for d in defects_clean
    )
    assert offline2 is False
    print("[OK] offline_mode 判定逻辑")


def test_prd_rule_engine_extract():
    """prd_rule_engine 使用 _extract_first_json_object 解析 Stage2 返回"""
    from modules.prd_audit.prd_rule_engine import _extract_json_dict
    text = '```json\n{"defects":[{"type":"缺失状态","module":"订单"}]}\n```'
    r = _extract_json_dict(text)
    assert r.get("defects") and r["defects"][0].get("type") == "缺失状态", r
    print("[OK] prd_rule_engine._extract_json_dict 使用首段 JSON")


def test_parse_llm_defects_only():
    """大模型专用：只解析 defects，与本地规则 JSON 分离"""
    from modules.prd_audit.prd_rule_engine import _parse_llm_defects_response
    # 正常
    r = _parse_llm_defects_response('{"defects":[{"type":"逻辑矛盾","module":"订单","risk_level":"P1"}]}')
    assert len(r) == 1 and r[0]["type"] == "逻辑矛盾" and r[0]["source"] == "llm", r
    # 多段/代码块
    r2 = _parse_llm_defects_response('```json\n{"defects":[{"type":"A"}]}\n```')
    assert len(r2) == 1 and r2[0]["type"] == "A", r2
    # 非 JSON / 无 defects 不抛错，返回 []
    assert _parse_llm_defects_response("not json") == []
    assert _parse_llm_defects_response('{"other":1}') == []
    print("[OK] _parse_llm_defects_response：仅大模型返回，不依赖本地规则 JSON")


def test_local_rule_json_robust():
    """本地规则库：双段/损坏 JSON 只取第一段"""
    from modules.prd_audit.prd_rule_engine import _load_json_file_first_object
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write('{"rules":[{"id":"R1"}]}\n{"rules":[{"id":"R2"}]}')
        path = f.name
    try:
        data = _load_json_file_first_object(path)
        assert data.get("rules") and len(data["rules"]) == 1 and data["rules"][0].get("id") == "R1", data
        print("[OK] 本地规则 JSON 容错：只取第一段")
    finally:
        os.unlink(path)


def main():
    print("PRD 审计流程自检 (project root: %s)" % ROOT)
    test_extract_first_json_system_model()
    test_extract_first_json_llm_client()
    test_load_llm_config_robust()
    test_pick_llm_config_path()
    test_offline_mode_condition()
    test_prd_rule_engine_extract()
    test_parse_llm_defects_only()
    test_local_rule_json_robust()
    print("All checks passed.")


if __name__ == "__main__":
    main()
