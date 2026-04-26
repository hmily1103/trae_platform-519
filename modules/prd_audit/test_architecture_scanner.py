# -*- coding: utf-8 -*-
"""
测试架构扫描器
"""

import json
from architecture_scanner import run_architecture_scan

# 模拟 Stage1 输出（模拟一个 KTV 点歌系统的 PRD 解析结果）
mock_stage1 = {
    "product_name": "KTV智能点歌系统",
    "background": "提升KTV用户点歌体验",
    "goal": "实现语音点歌、手机扫码点歌、智能推荐",
    "modules": [
        "语音点歌模块",
        "扫码点歌模块", 
        "歌曲播放引擎",
        "智能推荐系统",
        "用户管理",
        "订单系统",
        "支付系统"
    ],
    "features": ["语音搜索", "扫码点歌", "智能推荐", "歌曲收藏"],
    "user_roles": ["普通用户", "VIP用户", "管理员"],
    "flows": [
        "用户语音点歌流程：用户说出歌曲名 -> 系统识别 -> 返回搜索结果 -> 用户确认 -> 加入播放列表",
        "扫码点歌流程：用户扫描二维码 -> 进入点歌页面 -> 搜索歌曲 -> 确认点歌 -> 同步到包厢",
        "歌曲播放流程：从播放列表取歌 -> 加载歌曲资源 -> 开始播放 -> 显示歌词 -> 播放完成",
        "支付流程：选择套餐 -> 确认订单 -> 调起支付 -> 支付成功 -> 开通权益"
    ],
    "states": [
        "空闲状态",
        "搜索中",
        "播放中",
        "暂停",
        "缓冲中",
        "错误状态"
    ],
    "business_rules": [
        "VIP用户可优先点歌",
        "播放列表最多容纳50首歌曲",
        "同一用户30秒内不能重复点歌",
        "支付成功后权益立即生效",
        "网络异常时自动重试3次"
    ],
    "data_structures": [
        "歌曲信息：歌曲ID、歌曲名、歌手、时长、歌词URL",
        "用户订单：订单ID、用户ID、套餐类型、金额、状态、创建时间",
        "播放记录：记录ID、歌曲ID、用户ID、播放时间、播放时长"
    ],
    "exceptions": [
        "网络超时",
        "歌曲资源不存在",
        "支付失败",
        "语音识别失败"
    ],
    "dependencies": [
        "语音识别服务（Xunfei）",
        "支付网关",
        "歌曲资源服务器"
    ],
    "success_metrics": [
        "点歌成功率 > 95%",
        "语音识别准确率 > 90%",
        "页面加载时间 < 2s"
    ]
}

# 模拟 Stage2 输出
mock_stage2 = {
    "defects": [
        {
            "type": "状态机缺失",
            "description": "未定义从错误状态恢复的规则",
            "risk_level": "P1",
            "module": "歌曲播放引擎"
        },
        {
            "type": "并发处理缺失", 
            "description": "多人同时点歌的冲突处理未定义",
            "risk_level": "P1",
            "module": "订单系统"
        }
    ]
}

def test_architecture_scan():
    print("=" * 60)
    print("测试架构透视扫描器")
    print("=" * 60)
    
    result = run_architecture_scan(mock_stage1, mock_stage2)
    
    # 打印架构概览
    print("\n【架构概览】")
    view = result.get("architecture_view", {})
    print(f"  模块数: {view.get('module_count', 0)}")
    print(f"  状态数: {view.get('state_count', 0)}")
    print(f"  状态转换: {view.get('transition_count', 0)}")
    print(f"  数据实体: {view.get('entity_count', 0)}")
    
    # 打印模块清单
    print("\n【功能模块清单】")
    for m in result.get("modules", [])[:5]:
        level_str = {1: "系统", 2: "子系统", 3: "功能"}.get(m.get("level", 2), "模块")
        print(f"  - {m.get('name')} [{level_str}] 复杂度:{m.get('complexity', 0)}")
    
    # 打印风险热点
    print("\n【风险热力图】")
    for h in result.get("risk_hotspots", [])[:5]:
        print(f"  ⚠️ {h.get('type')} - {h.get('target')} ({h.get('level')})")
        print(f"     风险: {h.get('risk')}")
    
    # 打印测试策略
    print("\n【测试策略建议】")
    strategy = result.get("test_strategy", {})
    
    priority = strategy.get("priority_modules", [])
    if priority:
        print(f"  优先测试模块 ({len(priority)}个):")
        for p in priority[:3]:
            print(f"    - {p.get('module')}: {p.get('reason')}")
    
    auto = strategy.get("automation_candidates", [])
    if auto:
        print(f"  自动化候选 ({len(auto)}个): " + ", ".join([a.get('module') for a in auto[:3]]))
    
    # 打印状态机
    print("\n【核心状态机】")
    state_diagram = result.get("state_diagram", "")
    if state_diagram:
        print(state_diagram[:500] + "..." if len(state_diagram) > 500 else state_diagram)
    
    # 保存完整结果到文件
    output_file = "architecture_scan_result.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 完整结果已保存到: {output_file}")
    
    return result

if __name__ == "__main__":
    test_architecture_scan()
