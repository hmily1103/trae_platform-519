#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRD 审计平台 CLI 工具 (Git Hook / CI 演示)
用法: python prd_audit_cli.py --diff "git diff HEAD~1"
"""

import argparse
import requests
import json
import sys

BASE_URL = "http://127.0.0.1:5000/prd_audit"

def analyze_git_diff(diff_content):
    print("🚀 [CLI] 正在发送代码变更到精准测试引擎...")
    try:
        # 这里演示调用精准测试的代码影响分析接口
        url = f"http://127.0.0.1:5000/precision_test/api/analyze"
        resp = requests.post(url, json={
            "project_type": "backend",
            "code_diff": diff_content
        }, timeout=60)
        
        if resp.status_code == 200:
            result = resp.json()
            report = result.get("data", {}).get("impact_report", "")
            print("\n" + "="*50)
            print("🔍 变更影响分析报告 (Git Hook 自动触发)")
            print("="*50)
            print(report)
            print("="*50)
            
            if "P0" in report or "致命风险" in report:
                print("\n❌ [拦截] 检测到高风险变更，请务必先确认回归测试用例！")
                # sys.exit(1) # 在实际 Git Hook 中取消注释以拦截 commit
            else:
                print("\n✅ [通过] 变更影响已评估，风险可控。")
        else:
            print(f"❌ 分析失败: {resp.status_code}")
    except Exception as e:
        print(f"❌ 连接平台失败: {e}")

def audit_prd_file(file_path):
    print(f"🚀 [CLI] 正在审计 PRD 文件: {file_path}")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        url = f"{BASE_URL}/api/generate"
        # 模拟同步审计调用
        resp = requests.post(url, json={
            "type": "prd_review",
            "content": content,
            "use_llm": True
        }, timeout=120)
        
        if resp.status_code == 200:
            # 简单处理流式 NDJSON 的最后一行 bundle
            lines = resp.text.strip().split('\n')
            for line in reversed(lines):
                obj = json.loads(line)
                if obj.get("type") == "bundle":
                    summary = obj.get("summary", {})
                    print("\n" + "="*50)
                    print(f"📊 PRD 审计摘要: {file_path}")
                    print(f"📈 质量评分: {summary.get('score', 0)}/10")
                    print(f"🚨 P0 风险: {summary.get('p0_count', 0)}")
                    print(f"⚠️ P1 漏洞: {summary.get('p1_count', 0)}")
                    print("="*50)
                    break
        else:
            print(f"❌ 审计失败: {resp.status_code}")
    except Exception as e:
        print(f"❌ 处理失败: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PRD Audit CLI")
    parser.add_argument("--diff", help="Git Diff 内容")
    parser.add_argument("--file", help="PRD 文件路径")
    
    args = parser.parse_args()
    
    if args.diff:
        analyze_git_diff(args.diff)
    elif args.file:
        audit_prd_file(args.file)
    else:
        parser.print_help()
