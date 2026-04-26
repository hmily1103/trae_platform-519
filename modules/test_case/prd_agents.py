# -*- coding: utf-8 -*-
"""
PRD 多角色评审：逻辑审计员、技术方案师、测试负责人 三个 Agent 协作，
各自输出负责的章节，再合并为一份八方面报告。
"""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

logger = logging.getLogger(__name__)

# 截断 PRD 过长时保留的最大字符数（避免超上下文）
PRD_CONTENT_MAX_LEN = 120000

# 规则库路径
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'knowledge_rules.json')

def _load_rules() -> dict:
    """加载外部规则库，如果失败则返回默认空规则"""
    try:
        if os.path.exists(RULES_FILE):
            with open(RULES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载规则库失败: {e}")
    return {}

def _format_rules(rules: list) -> str:
    """将规则列表格式化为字符串"""
    if not rules:
        return ""
    return "\n".join([f"- {r}" for r in rules])

def _truncate_content(content: str) -> str:
    if not content or len(content) <= PRD_CONTENT_MAX_LEN:
        return content or ""
    return content[:PRD_CONTENT_MAX_LEN] + "\n\n[文档已截断，仅分析前 {} 字]".format(PRD_CONTENT_MAX_LEN)


def get_logic_auditor_prompt(prd_content: str) -> str:
    """逻辑审计员：总体结论、漏洞与风险（逻辑/功能/描述/可测试性）、待确认清单"""
    c = _truncate_content(prd_content)
    rules_data = _load_rules()
    common_rules = _format_rules(rules_data.get('common_rules', []))
    focus_points = _format_rules(rules_data.get('review_focus', {}).get('logic_auditor', []))

    return f"""你是**逻辑审计员**，只从逻辑与需求维度分析 PRD，不涉及技术实现或测试计划。

请参考以下通用评审规则：
{common_rules}

请阅读以下 PRD 文档，并**只**输出以下三部分，使用以下二级标题（Markdown），不要输出其他内容：

## 一、总体结论
- 可评审性/可测试性结论（1～2 句话）
- 综合质量评分（X/10）及简要理由（基于逻辑与需求维度）

## 二、漏洞与风险清单（逻辑与需求维度）
按以下子维度分条列出，每条包含：问题描述、涉及位置/模块、建议（或标为【待确认】）。
重点关注：
{focus_points}

- 逻辑矛盾（前后不一致、规则冲突、状态死循环）
- 功能/场景缺失（缺流程、缺异常流、缺边界定义）
- 描述模糊与歧义（可多种理解、未定义术语）
- 可测试性不足（无法验证、无验收标准、无判定条件）

## 三、待确认清单
汇总所有需产品/业务确认的问题，便于评审会逐条过。

约束：不臆测；拿不准的标为【待确认】。直接输出上述三部分，不要开场白。

PRD 文档内容如下：

{c}"""


def get_tech_architect_prompt(prd_content: str) -> str:
    """技术方案师：研发评估 + 漏洞与风险中的技术与实现风险"""
    c = _truncate_content(prd_content)
    rules_data = _load_rules()
    focus_points = _format_rules(rules_data.get('review_focus', {}).get('tech_architect', []))

    return f"""你是**技术方案师**，只从研发与实现维度分析 PRD，不写测试计划或需求逻辑。

请阅读以下 PRD 文档，并**只**输出以下两部分，使用以下二级标题（Markdown），不要输出其他内容：

## 五、研发评估
从研发视角输出：
- 实现难度/工作量粗估
- 技术风险、依赖与阻塞
- 建议前期澄清或预研的点

## 二（续）、漏洞与风险清单 — 技术与实现维度
在「漏洞与风险清单」中仅补充**技术与实现风险**相关条目。
重点关注：
{focus_points}

例如：
- 接口/表结构变动风险、性能瓶颈、安全与数据一致性未约束等。
每条包含：问题描述、涉及位置/模块、建议。

约束：不臆测。直接输出上述两部分，不要开场白。

PRD 文档内容如下：

{c}"""


def get_test_lead_prompt(prd_content: str) -> str:
    """测试负责人：测试重点、评审计划与排期建议、计划建议、优先级与责任、后续动作 + 体验与流程风险（不输出用例/步骤）"""
    c = _truncate_content(prd_content)
    rules_data = _load_rules()
    focus_points = _format_rules(rules_data.get('review_focus', {}).get('test_lead', []))
    module_rules = rules_data.get('module_rules', {})
    
    # 构造特定模块规则提示
    module_rules_str = ""
    for mod, rules in module_rules.items():
        module_rules_str += f"\n若涉及 {mod} 相关功能，请检查：\n" + _format_rules(rules)

    return f"""你是**测试负责人**和**资深测试项目经理（Test PM）**，只从测试与质量维度分析 PRD，不写研发实现或需求逻辑。

请参考以下测试关注点：
{focus_points}
{module_rules_str}

请阅读以下 PRD 文档，并**只**输出以下六部分，使用以下二级标题（Markdown），不要输出其他内容：

## 四、测试重点
建议的测试重点：核心场景、高风险模块、必测路径、边界与异常、性能/安全等专项；可按模块或优先级列。

## 4.5 评审计划与排期建议
根据业务复杂度、技术风险及依赖关系，输出：
1. **整体评级**：P0(核心)/P1(重要)/P2(普通)及理由。
2. **执行顺序**：建议先测试哪些核心/高风险模块，再测试哪些边缘模块。
3. **前置介入**：QA需提前准备的事项（如造数、接口联调）。

## 六、计划建议
建议的节奏：需求澄清 → 研发评估 → 测试设计 → 用例编写等时间顺序或里程碑建议。

## 七、优先级与责任建议
对前面各角色发现的问题做 P0/P1/P2 分级；责任归属建议（产品/开发/测试）。

## 八、后续动作建议
接下来几步该做什么（澄清哪些、谁先做、再做什么）。

## 二（续）、漏洞与风险清单 — 体验与流程维度
在「漏洞与风险清单」中仅补充**体验与流程风险**相关条目，例如：关键路径不完整、易误用、反馈不明确等。每条包含：问题描述、涉及位置/模块、建议。

约束：
- 严禁输出测试用例/测试步骤/Given-When-Then 等用例格式。
- 只输出测试重点、计划与建议、风险与责任归属。
- 不臆测。拿不准的写【待确认】。
直接输出上述六部分，不要开场白。

PRD 文档内容如下：

{c}"""


def _extract_sections_by_regex(text: str) -> dict:
    """按 ## 一、 ## 二、 等拆分，返回 { '1': content, '2': content, ... }"""
    sections = {}
    if not text:
        return sections
    # 增加对 4.5, 4.6 的支持
    # 匹配 ## 1. 或 ## 一、
    parts = re.split(r"\n(?=##\s+)", text)
    key_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8"}
    
    for part in parts:
        part = part.strip()
        if not part or not part.startswith("##"):
            continue
            
        # 尝试匹配 "## 4.5" 这种格式
        m_sub = re.match(r"^##\s*(\d+\.\d+)\s+([^\n]*)", part)
        if m_sub:
            key = m_sub.group(1).strip()
            sections[key] = part
            continue

        # 尝试匹配 "## 一、" 这种格式
        m = re.match(r"^##\s*([一二三四五六七八\d]+)[、.（续）\s]*([^\n]*)", part)
        if m:
            key = m.group(1).strip()
            key_n = key_map.get(key, key) if key in key_map else key
            # 只保留 1-8 的主标题，或者 4.5 这种子标题
            if (key_n.isdigit() and 1 <= int(key_n) <= 8) or ('.' in key_n):
                # 同一编号只保留第一次（逻辑优先），但「二」会合并多源
                if key_n not in sections:
                    sections[key_n] = part
    return sections


def _take_from_next_section_to_next(text: str, start_marker: str) -> str:
    """从包含 start_marker 的那一行开始截取到下一个 ## 或结尾"""
    if not text or not start_marker:
        return ""
    i = text.find(start_marker)
    if i == -1:
        return ""
    # 回溯到该行开头
    line_start = text.rfind("\n", 0, i) + 1
    j = text.find("\n## ", i + 1)
    if j == -1:
        j = len(text)
    return text[line_start:j].strip()


def merge_agent_outputs(logic_out: str, tech_out: str, test_out: str) -> str:
    """
    将三个 Agent 的输出合并为一份报告，顺序：一、二、三、四、4.5、五、六、七、八。
    二 由逻辑的「二」+ 技术的「二（续）」+ 测试的「二（续）」合并。
    """
    s1 = _extract_sections_by_regex(logic_out or "")
    s2 = _extract_sections_by_regex(tech_out or "")
    s3 = _extract_sections_by_regex(test_out or "")

    one = s1.get("1", "").strip() or _take_from_next_section_to_next(logic_out or "", "## 一、") or _take_from_next_section_to_next(logic_out or "", "总体结论")
    two_logic = s1.get("2", "").strip()
    two_tech = _take_from_next_section_to_next(tech_out or "", "二（续）") or _take_from_next_section_to_next(tech_out or "", "技术与实现")
    two_test = _take_from_next_section_to_next(test_out or "", "体验与流程")
    two_parts = [p for p in [two_logic, two_tech, two_test] if p]
    two = "\n\n".join(two_parts) if two_parts else two_logic or "## 二、漏洞与风险清单\n\n（见各角色分述）"
    three = s1.get("3", "").strip() or _take_from_next_section_to_next(logic_out or "", "## 三、") or _take_from_next_section_to_next(logic_out or "", "待确认")
    four = s3.get("4", "").strip() or _take_from_next_section_to_next(test_out or "", "## 四、") or _take_from_next_section_to_next(test_out or "", "测试重点")
    
    # 新增 4.5
    four_dot_five = s3.get("4.5", "").strip() or _take_from_next_section_to_next(test_out or "", "## 4.5") or _take_from_next_section_to_next(test_out or "", "评审计划")

    five = s2.get("5", "").strip() or _take_from_next_section_to_next(tech_out or "", "## 五、") or _take_from_next_section_to_next(tech_out or "", "研发评估")
    six = s3.get("6", "").strip() or _take_from_next_section_to_next(test_out or "", "## 六、") or _take_from_next_section_to_next(test_out or "", "计划建议")
    seven = s3.get("7", "").strip() or _take_from_next_section_to_next(test_out or "", "## 七、") or _take_from_next_section_to_next(test_out or "", "优先级与责任")
    eight = s3.get("8", "").strip() or _take_from_next_section_to_next(test_out or "", "## 八、") or _take_from_next_section_to_next(test_out or "", "后续动作")

    order = [
        ("一、总体结论", one),
        ("二、漏洞与风险清单", two),
        ("三、待确认清单", three),
        ("四、测试重点", four),
        ("4.5 评审计划与排期建议", four_dot_five),
        ("五、研发评估", five),
        ("六、计划建议", six),
        ("七、优先级与责任建议", seven),
        ("八、后续动作建议", eight),
    ]
    lines = []
    for title, body in order:
        if body:
            # 如果 body 已经自带标题，就不重复加
            if body.strip().startswith("##"):
                lines.append(body)
            else:
                lines.append("## " + title + "\n\n" + body.strip())
                
    return "\n\n".join(lines).strip() or (logic_out or "") + "\n\n---\n\n" + (tech_out or "") + "\n\n---\n\n" + (test_out or "")



def extract_report_sections(merged_report: str) -> dict:
    """
    将合并后的 Markdown 报告按标题编号拆分为 {1: '一、', 2: '二、', ...} 字典。
    供上层构造结构化 JSON 使用。
    """
    if not merged_report or not merged_report.strip():
        return {}
    text = merged_report.replace("\r\n", "\n")
    sections: dict[int, str] = {}
    order = ["一", "二", "三", "四", "五", "六", "七", "八"]
    # 找到所有一级小节的起始位置
    pattern = re.compile(r"^##\s*([一二三四五六七八])(?:、|（续）|\.)?[^\\n]*", re.MULTILINE)
    matches = []
    for m in pattern.finditer(text):
        num = order.index(m.group(1)) + 1
        matches.append({"num": num, "index": m.start()})
    # 按编号截取
    for n in range(1, 9):
        start_idx = -1
        end_idx = len(text)
        for m in matches:
            if m["num"] == n and start_idx == -1:
                start_idx = m["index"]
            if start_idx >= 0 and m["num"] != n and m["index"] > start_idx:
                end_idx = m["index"]
                break
        if start_idx >= 0:
            sections[n] = text[start_idx:end_idx].strip()
    return sections


def _call_logic_auditor(prd_content: str, llm_config_path: str, timeout: int) -> str:
    from utils.llm_client import call_llm
    try:
        return call_llm(
            [{"role": "user", "content": get_logic_auditor_prompt(prd_content)}],
            config_path=llm_config_path,
            stream=False,
            timeout=timeout
        )
    except Exception as e:
        logger.exception("Logic auditor call failed")
        return "## 一、总体结论\n\n（逻辑审计员调用异常：{}）\n\n## 二、漏洞与风险清单\n\n（见下方）\n\n## 三、待确认清单\n\n（见下方）".format(str(e))


def _call_tech_architect(prd_content: str, llm_config_path: str, timeout: int) -> str:
    from utils.llm_client import call_llm
    try:
        return call_llm(
            [{"role": "user", "content": get_tech_architect_prompt(prd_content)}],
            config_path=llm_config_path,
            stream=False,
            timeout=timeout
        )
    except Exception as e:
        logger.exception("Tech architect call failed")
        return "## 五、研发评估\n\n（技术方案师调用异常：{}）".format(str(e))


def _call_test_lead(prd_content: str, llm_config_path: str, timeout: int) -> str:
    from utils.llm_client import call_llm
    try:
        return call_llm(
            [{"role": "user", "content": get_test_lead_prompt(prd_content)}],
            config_path=llm_config_path,
            stream=False,
            timeout=timeout
        )
    except Exception as e:
        logger.exception("Test lead call failed")
        return "## 四、测试重点\n\n（测试负责人调用异常：{}）\n\n## 六、计划建议\n\n## 七、优先级与责任建议\n\n## 八、后续动作建议\n\n".format(str(e))


def run_prd_multi_agent(prd_content: str, llm_config_path: str, timeout: int = 90) -> Tuple[str, List[str]]:
    """
    并行调用三个 Agent，合并报告。相比串行可显著缩短等待时间。
    返回 (merged_report, status_messages)。
    """
    status_messages = ["逻辑审计员、技术方案师、测试负责人并行分析中…（约 1～2 分钟）"]
    logic_out = ""
    tech_out = ""
    test_out = ""

    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_logic = executor.submit(_call_logic_auditor, prd_content, llm_config_path, timeout)
        fut_tech = executor.submit(_call_tech_architect, prd_content, llm_config_path, timeout)
        fut_test = executor.submit(_call_test_lead, prd_content, llm_config_path, timeout)
        logic_out = fut_logic.result()
        tech_out = fut_tech.result()
        test_out = fut_test.result()

    status_messages.append("正在合并报告…")
    merged = merge_agent_outputs(logic_out or "", tech_out or "", test_out or "")
    return merged, status_messages
