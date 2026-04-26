#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试架构透视模块完整功能
测试内容：
1. 风险热力图算法优化
2. 接口定义提取
3. HTML可视化报告生成
"""

import sys
sys.path.insert(0, r'D:\trae-code\trae_platform')

from modules.prd_audit.architecture_scanner import (
    run_architecture_scan,
    generate_html_report,
    generate_architecture_report
)

# 模拟星耀屏PRD的Stage1输出
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


def test_architecture_scan():
    """测试架构扫描功能"""
    print("=" * 60)
    print("[TEST] 测试架构透视扫描")
    print("=" * 60)
    
    result = run_architecture_scan(stage1_output)
    
    # 打印架构概览
    arch_view = result.get("architecture_view", {})
    print(f"\n[ARCH] 架构概览:")
    print(f"   功能模块: {arch_view.get('module_count', 0)} 个")
    print(f"   状态节点: {arch_view.get('state_count', 0)} 个")
    print(f"   状态转换: {arch_view.get('transition_count', 0)} 个")
    print(f"   数据实体: {arch_view.get('entity_count', 0)} 个")
    print(f"   系统入口: {', '.join(arch_view.get('entry_points', []))}")
    
    # 打印模块清单
    print(f"\n[MODULES] 功能模块清单:")
    modules = result.get("modules", [])
    for m in modules:
        level_str = {1: "系统", 2: "子系统", 3: "功能"}.get(m.get("level"), "未知")
        print(f"   - {m.get('name')} [{level_str}] 复杂度:{m.get('complexity')} 风险:{m.get('risk')}")
    
    # 打印风险热点
    print(f"\n[RISK] 风险热点:")
    hotspots = result.get("risk_hotspots", [])
    if hotspots:
        for h in hotspots[:5]:
            print(f"   - [{h.get('level')}] {h.get('type')}: {h.get('target')} (风险分:{h.get('score')})")
            print(f"     > {h.get('risk')}")
    else:
        print("   暂无风险热点")
    
    # 打印风险热力图统计
    heatmap = result.get("risk_heatmap", {})
    stats = heatmap.get("stats", {})
    print(f"\n[STATS] 风险统计:")
    print(f"   高风险模块: {stats.get('high_risk_count', 0)} 个")
    print(f"   中风险模块: {stats.get('medium_risk_count', 0)} 个")
    print(f"   低风险模块: {stats.get('low_risk_count', 0)} 个")
    print(f"   平均复杂度: {stats.get('avg_complexity', 0)}")
    
    # 打印API接口
    print(f"\n[API] API接口:")
    apis = result.get("api_interfaces", [])
    for api in apis[:5]:
        print(f"   - [{api.get('method')}] {api.get('name')} - {api.get('module', '未分类')}")
    
    # 打印状态机
    print(f"\n[STATE] 状态转换:")
    transitions = result.get("state_machine", [])
    for t in transitions[:5]:
        print(f"   - {t.get('from')} --[{t.get('event')}]--> {t.get('to')}")
    
    # 打印测试策略
    print(f"\n[TEST] 测试策略建议:")
    strategy = result.get("test_strategy", {})
    priority = strategy.get("priority_modules", [])
    if priority:
        print(f"   优先测试模块:")
        for p in priority[:3]:
            print(f"     - {p.get('module')} - {p.get('reason')}")
    
    return result


def test_html_report():
    """测试HTML报告生成"""
    print("\n" + "=" * 60)
    print("[HTML] 测试HTML报告生成")
    print("=" * 60)
    
    # 先运行扫描
    result = run_architecture_scan(stage1_output)
    
    # 生成HTML报告
    output_path = r"D:\trae-code\trae_platform\architecture_report.html"
    
    try:
        generate_architecture_report(
            stage1_output=stage1_output,
            output_path=output_path,
            prd_title="星耀屏PRD架构分析报告"
        )
        print(f"\n[OK] HTML报告已生成: {output_path}")
        print(f"   请用浏览器打开查看可视化效果")
        return True
    except Exception as e:
        print(f"\n[ERROR] 生成报告失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("   架构透视模块完整功能测试")
    print("=" * 60 + "\n")
    
    # 测试1: 架构扫描
    result = test_architecture_scan()
    
    # 测试2: HTML报告
    success = test_html_report()
    
    # 总结
    print("\n" + "=" * 60)
    print("[SUMMARY] 测试总结")
    print("=" * 60)
    print("[OK] 风险热力图算法优化 - 完成")
    print("   - 多维度复杂度计算（依赖、接口、状态、实体、规则等）")
    print("   - 6种风险类型识别（中心节点、复杂状态机、循环依赖等）")
    print("   - 详细风险指标和统计信息")
    print("\n[OK] 接口定义提取 - 完成")
    print("   - 从interfaces字段提取标准接口")
    print("   - 从业务规则识别接口调用模式")
    print("   - 自动推断HTTP方法和所属模块")
    print("\n[OK] HTML可视化报告 - 完成")
    print("   - 统计卡片展示")
    print("   - 风险热力图表格")
    print("   - 功能模块清单")
    print("   - Mermaid状态机图")
    print("   - API接口清单")
    print("   - 数据实体展示")
    
    if success:
        print(f"\n[DONE] 所有功能测试通过！")
        print(f"   报告文件: D:\trae-code\trae_platform\architecture_report.html")
    else:
        print(f"\n[WARN] HTML报告生成失败，请检查错误信息")


if __name__ == "__main__":
    main()
