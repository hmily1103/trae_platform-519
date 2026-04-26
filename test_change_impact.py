# -*- coding: utf-8 -*-
"""变更影响分析测试脚本"""

import json
import sys
sys.path.insert(0, '.')

from modules.prd_audit.architecture_scanner import (
    run_architecture_scan,
    analyze_impact,
    generate_impact_report_html
)

# 模拟星耀屏PRD的Stage1输出（与test_architecture_complete.py相同）
stage1_output = {
    "modules": ["投屏功能", "游戏功能", "广告管理", "AI数字人", "营销活动", "明星墙", "手机扫码点歌"],
    "functional_modules": [
        {"name": "投屏功能", "priority": "P0", "states": ["投屏中状态", "投屏暂停", "投屏结束"]},
        {"name": "游戏功能", "priority": "P0", "states": ["游戏中状态", "游戏暂停", "游戏结束"]},
        {"name": "广告管理", "priority": "P1", "states": ["广告展示", "广告暂停", "广告结束"]},
        {"name": "AI数字人", "priority": "P2", "states": ["AI展示", "AI移动", "AI隐藏"]},
        {"name": "营销活动", "priority": "P2", "states": ["活动展示", "活动结束"]},
        {"name": "明星墙", "priority": "P3", "states": ["明星展示"]},
        {"name": "手机扫码点歌", "priority": "P2", "states": ["扫码中", "点歌成功", "点歌失败"]},
    ],
    "states": ["空闲状态", "默认展示页面"],
    "business_rules": [
        "优先级：投屏>游戏>广告>营销活动>明星墙>AI数字人",
        "进入投屏：如此时正在播放广告，则直接切断广告，进入投屏",
        "退出投屏：检测广告，如有广告展示，则播放广告",
        "广告展示完毕：展示完毕后，则继续播放广告",
        "广告被打断：退出后继续广告展示",
        "指定模式场景：此时正在播放广告，则被打断",
        "AI数字人移动至星耀屏展示",
        "星耀屏被使用时，在tv端展示",
        "进入游戏：如此时正在播放广告，则直接切断广告，进入游戏",
        "退出游戏：检测广告，如有广告展示，则播放广告",
        "调用投屏接口获取设备列表",
        "请求游戏数据",
        "获取广告配置信息",
        "提交点歌请求",
        "同步播放状态",
    ],
    "interfaces": [
        {"name": "投屏控制", "caller": "手机端", "callee": "星耀屏", "method": "POST"},
        {"name": "游戏启动", "caller": "TV端", "callee": "游戏引擎", "method": "POST"},
        {"name": "广告下发", "caller": "广告系统", "callee": "星耀屏", "method": "GET"},
    ],
    "data_structures": [
        {"name": "投屏任务", "fields": ["task_id", "device_id", "status", "start_time"]},
        {"name": "广告任务", "fields": ["ad_id", "content", "duration", "priority"]},
        {"name": "游戏任务", "fields": ["game_id", "player_id", "score", "state"]},
    ],
    "flows": [
        "用户扫码 -> 发起投屏 -> 投屏中状态",
        "投屏结束 -> 检测广告 -> 广告展示",
        "用户启动游戏 -> 游戏中状态",
    ],
}

# 先运行架构扫描获取基础数据
print("=" * 60)
print("Step 1: 运行架构扫描...")
print("=" * 60)

scan_result = run_architecture_scan(stage1_output)

print(f"扫描结果:")
print(f"  - 功能模块: {scan_result['architecture_view']['module_count']}")
print(f"  - 状态节点: {scan_result['architecture_view']['state_count']}")
print(f"  - API接口: {len(scan_result['api_interfaces'])}")
print(f"  - 数据实体: {scan_result['architecture_view']['entity_count']}")
print(f"  - 风险热点: {len(scan_result['risk_hotspots'])}")

# 测试不同的变更场景
test_changes = [
    {
        "desc": "新增语音点歌功能模块",
        "change": "新增语音点歌功能模块，支持用户通过语音指令搜索和点播歌曲"
    },
    {
        "desc": "修改广告播放逻辑",
        "change": "修改广告播放逻辑，优化退出投屏后的广告展示流程"
    },
    {
        "desc": "新增接口对接第三方音乐平台",
        "change": "新增接口对接第三方音乐平台，获取歌曲资源"
    },
    {
        "desc": "完整需求变更测试",
        "change": "在星耀屏新增游戏功能，同时优化广告展示策略，支持游戏场景下广告被打断后恢复播放"
    }
]

print("\n" + "=" * 60)
print("Step 2: 测试变更影响分析...")
print("=" * 60)

for i, test in enumerate(test_changes, 1):
    print(f"\n--- 测试 {i}: {test['desc']} ---")
    print(f"变更描述: {test['change']}")
    
    # 执行影响分析
    impact_result = analyze_impact(test['change'], scan_result)
    
    # 输出摘要
    print(f"\n影响摘要: {impact_result['summary']}")
    
    # 输出风险评估
    risk = impact_result['risk_assessment']
    print(f"\n风险评估:")
    print(f"  - 风险等级: {risk['risk_level']} ({risk['risk_desc']})")
    print(f"  - 影响分数: {risk['total_score']}")
    print(f"  - 建议: {risk['recommendation']}")
    
    if risk.get('high_risk_items'):
        print(f"  - 高风险项: {', '.join(risk['high_risk_items'][:3])}")
    
    # 输出受影响模块
    if impact_result['affected_modules']:
        print(f"\n受影响模块 ({len(impact_result['affected_modules'])}):")
        for m in impact_result['affected_modules'][:3]:
            print(f"  - {m['module']} ({m['action']}, {m['impact_scope']}影响)")
    
    # 输出测试建议
    test_recs = impact_result['test_recommendations']
    if test_recs.get('priority_test_areas'):
        print(f"\n优先测试区域:")
        for p in test_recs['priority_test_areas'][:2]:
            print(f"  - {p['module']}: {p['reason']}")
    
    print()

# 生成完整的HTML报告
print("=" * 60)
print("Step 3: 生成HTML报告...")
print("=" * 60)

test_change = test_changes[3]['change']  # 使用最后一个完整测试
impact_result = analyze_impact(test_change, scan_result)

html_content = generate_impact_report_html(impact_result, "星耀屏PRD变更影响分析")

# 保存报告
output_path = "change_impact_report.html"
with open(output_path, "w", encoding="utf-8") as f:
    f.write(html_content)

print(f"\nHTML报告已生成: {output_path}")
print(f"\n文件预览: file:///{output_path.replace(chr(92), '/')}")
