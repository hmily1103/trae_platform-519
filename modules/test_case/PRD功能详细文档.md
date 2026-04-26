# PRD 评审功能详细文档

> 本文档描述 Trae Platform 测试用例模块中的 **PRD（产品需求文档）评审** 功能，包含产品说明与技术实现。

---

## 一、功能概述

### 1.1 简介

PRD 评审功能用于对产品需求文档进行**结构化解析 + 漏洞扫描 + 评审报告生成**，通过「三段式」流程（结构解析 → 漏洞扫描 → 报告生成），结合**规则引擎**与 **LLM**，自动发现需求中的逻辑漏洞、状态机缺失、流程断裂、权限问题等风险，并输出可执行的审计报告。

### 1.2 核心能力

| 能力 | 说明 |
|------|------|
| 结构解析 | 将自然语言 PRD 转为结构化需求模型（背景、模块、状态、流程、规则等） |
| 漏洞扫描 | 规则库（30 条）+ LLM 双重扫描，输出缺陷列表（含 P0/P1/P2 分级） |
| 报告生成 | 合并同源问题、计算质量评分、生成 Markdown/Word 报告 |
| 飞书集成 | 支持飞书文档链接直接拉取正文进行分析 |
| 文档解析 | 支持 PDF、Word（.docx）上传解析 |
| 自定义 Prompt | 支持单次调用模式（自定义完整提示词）或极简八段报告模式 |

### 1.3 入口与访问路径

- **主页面**：`/test_case/`
- **PRD 评审**：在用例管理首页选择「PRD 评审」标签
- **提示词配置**：`/test_case/prompts`
- **PRD 规则管理**：`/test_case/prd_rules`
- **知识中心**：`/test_case/knowledge`

---

## 二、三段式流程说明

### 2.1 流程概览

```
PRD 输入（文本/飞书链接/PDF/Word）
        ↓
┌───────────────────────────────────────────────────────────────┐
│ Stage1：结构解析                                                │
│ 角色：资深产品架构师与测试架构师                                  │
│ 输出：结构化 JSON（background、modules、states、flows 等）       │
└───────────────────────────────────────────────────────────────┘
        ↓
┌───────────────────────────────────────────────────────────────┐
│ Stage2：漏洞扫描                                                │
│ 角色：10 年经验的测试架构师                                      │
│ 输入：Stage1 结构 + PRD 原文                                    │
│ 来源：规则库（prd_scan_rules.json）+ LLM 漏洞检测                │
│ 输出：defects 列表（id、type、module、anchor、risk_level 等）   │
└───────────────────────────────────────────────────────────────┘
        ↓
┌───────────────────────────────────────────────────────────────┐
│ Stage3：报告生成                                                │
│ 默认：Python 合并 + Markdown 渲染（六段格式）                    │
│ 可选：LLM 极简审计报告（prd_audit_prompt_stage3_minimal.txt）   │
│       角色：极简主义需求审计报告生成专家                          │
│       输出：八段格式 Markdown                                   │
└───────────────────────────────────────────────────────────────┘
        ↓
最终报告（Markdown / Word 导出）
```

### 2.2 各阶段角色与职责

| 阶段 | 角色 | 职责 | 实现位置 |
|------|------|------|----------|
| Stage1 | 资深产品架构师与测试架构师 | 将 PRD 转为结构化需求模型，提取 13 类信息 | `system_model.py` → `STAGE1_STRUCTURE_PROMPT` |
| Stage2 | 10 年经验的测试架构师 | 基于结构进行漏洞扫描，输出 defects 列表 | `prd_rule_engine.py` → `STAGE2_DEFECT_SCAN_PROMPT` |
| Stage3（默认） | 无 LLM 角色 | Python 逻辑合并、评分、渲染六段 Markdown | `views.py` → `_build_stage3_report`、`_render_stage4_markdown` |
| Stage3（极简） | 极简主义需求审计报告生成专家 | LLM 按八段格式生成报告（七维评分、核心问题矩阵等） | `prd_audit_prompt_stage3_minimal.txt` |

### 2.3 单次调用模式

当用户在前端选择「自定义提示词」并填入完整 prompt 时，走**单次调用**，不经过 Stage1/Stage2，由 LLM 一气呵成完成审计与报告。此时需在 prompt 末尾加占位符 `{content}` 以注入 PRD 内容。

---

## 三、技术架构

### 3.1 模块结构

```
modules/test_case/
├── views.py                    # Flask 路由与视图
├── system_model.py             # Stage1 结构解析
├── prd_rule_engine.py          # Stage2 漏洞扫描（规则+LLM）
├── feishu_client.py            # 飞书文档拉取
├── models.py                   # 数据模型
├── storage.py                  # 存储层
├── llm_config.json             # LLM 配置
├── prd_scan_rules.json         # 规则库（30 条）
├── prd_audit_prompt_default.txt           # 默认单次审计 prompt
├── prd_audit_prompt_stage3_minimal.txt    # Stage3 极简八段报告 prompt
├── prd_review_prompt.txt       # 备用单次审计 prompt
├── feishu_config.json.example  # 飞书配置示例
└── templates/                  # 前端模板
```

### 3.2 数据流

```
用户输入（content / prd_text）
    → 飞书链接？fetch_feishu_doc_content()
    → PDF/Word？api_parse_pdf / api_parse_docx
    → 纯文本
    → extract_prd_structure(content) → stage1_output
    → run_stage2_defect_scan(stage1_output, prd_text) → stage2_output
    → _run_stage3_llm_report() 或 _build_stage3_report() + _render_stage4_markdown()
    → merged_report (Markdown)
    → 流式输出 / 导出 Word
```

### 3.3 Stage1 输出结构（PRD_STRUCTURE_SCHEMA）

| 字段 | 类型 | 说明 |
|------|------|------|
| background | string | 需求背景 |
| goal | string | 需求目标 |
| modules | string[] | 功能模块 |
| user_roles | string[] | 用户角色 |
| flows | string[] | 核心业务流程 |
| states | string[] | 状态机 |
| business_rules | string[] | 关键业务规则 |
| data_structures | string[] | 输入输出数据结构 |
| permissions | string[] | 权限控制规则 |
| exceptions | string[] | 异常处理机制 |
| edge_cases | string[] | 边界条件 |
| dependencies | string[] | 外部依赖系统 |
| non_functional_requirements | string[] | 非功能需求 |

未说明字段填充 `【PRD未说明】`。

### 3.4 Stage2 输出结构（defect）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 漏洞 ID（如 D001） |
| type | string | 问题类型 |
| module | string | 涉及模块 |
| anchor | string | PRD 原文锚点位置 |
| description | string | 问题描述 |
| risk_level | string | P0 / P1 / P2 |
| reason | string | 风险分析 |
| suggestion | string | 审计建议 |
| source | string | rule / llm / hybrid |

### 3.5 规则库（prd_scan_rules.json）

- **6 大类**：业务规则一致性、状态机完整性、流程完整性、数据完整性、可测试性、技术与系统风险
- **30 条规则**：每条含 id、category、name、description、example、risk、core、enabled
- **规则命中**：根据 Stage1 结构与 PRD 原文，匹配规则库输出 rule 类缺陷；LLM 输出 llm 类缺陷；合并去重后输出

---

## 四、API 接口

### 4.1 PRD 评审相关 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/test_case/api/generate` | 执行 AI 生成（PRD 评审 / 用例编写等），支持流式 |
| POST | `/test_case/api/analyze_prd` | 非流式 PRD 分析，返回完整 JSON |
| POST | `/test_case/api/parse_pdf` | 解析 PDF 上传，提取文本 |
| POST | `/test_case/api/parse_docx` | 解析 Word 上传，提取文本 |
| POST | `/test_case/api/export_report_docx` | 将报告 Markdown 导出为 Word |
| GET | `/test_case/api/default_prd_prompt` | 获取默认 PRD 审计提示词 |

### 4.2 api/generate 请求体（PRD 评审）

```json
{
  "type": "prd_review",
  "content": "PRD 正文或飞书文档链接",
  "prompt": "可选，自定义完整提示词，末尾需含 {content}"
}
```

- `type=prd_review` 时走三段式或单次调用（取决于 prompt 是否为空）
- `prompt` 非空：单次调用，替换 `{content}` 后调用 LLM
- `prompt` 为空：三段式（Stage1 → Stage2 → Stage3）

### 4.3 api/analyze_prd 请求体

```json
{
  "prd_text": "PRD 正文",
  "content": "可选，与 prd_text 等价"
}
```

返回结构：

```json
{
  "raw_report_markdown": "完整 Markdown 报告",
  "status": ["Stage1完成", "Stage2完成", "Stage3完成", "Stage4完成"],
  "summary": "一、总体结论",
  "prd_gaps": "漏洞摘要",
  "risks": "项目风险",
  "rd_focus": "研发重点",
  "test_focus": "测试重点",
  "plan": { "level": "P1", "order_advice": [], "prep_advice": [] },
  "quality": { "overall": 7.5, "dimensions": {}, "rule_engine_score": 7.5 },
  "stage1_output": { ... },
  "stage2_output": { "defects": [...] },
  "stage3_output": { ... }
}
```

---

## 五、配置说明

### 5.1 LLM 配置（llm_config.json）

```json
{
  "llm_provider": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-xxx",
  "model": "deepseek-chat",
  "fallback_enabled": false,
  "fallback_api_key": "",
  "fallback_base_url": "https://api.deepseek.com/v1",
  "fallback_model": "deepseek-chat"
}
```

- 支持 `deepseek`、`gemini`、`openai`、`custom`
- `fallback_enabled=true` 时主 provider 失败可切换 fallback

### 5.2 飞书配置（feishu_config.json）

从 `feishu_config.json.example` 复制为 `feishu_config.json`：

```json
{
  "app_id": "xxx",
  "app_secret": "xxx"
}
```

或环境变量 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。配置后可在 PRD 评审中输入飞书文档链接直接分析。

### 5.3 Stage3 极简报告（可选）

- 文件：`modules/test_case/prd_audit_prompt_stage3_minimal.txt`
- 存在且有效时，Stage3 使用 LLM 按八段格式生成报告
- 占位符：`{prd_content}`、`{structure_json}`、`{defects_json}`

---

## 六、报告格式

### 6.1 默认六段格式（Python 渲染）

1. 一、总体结论（质量评分、风险等级、主要问题）
2. 二、漏洞与风险清单（合并后核心问题 + 详细漏洞清单）
3. 三、测试重点
4. 四、研发重点
5. 五、项目风险
6. 六、计划建议

### 6.2 极简八段格式（Stage3 LLM）

1. 一、总体结论（审计结论、综合质量评分、核心风险摘要）
2. 二、核心问题矩阵（合并版）
3. 三、详细漏洞清单（原始版）
4. 四、待确认清单
5. 五、测试重点
6. 六、研发重点
7. 七、项目风险
8. 八、计划建议

---

## 七、风险等级与质量评分

### 7.1 风险等级（P0/P1/P2）

| 等级 | 说明 |
|------|------|
| P0 | 阻断级：核心逻辑冲突、流程断裂、关键模块缺失 |
| P1 | 严重级：规则歧义、边界遗漏、异常流程未定义 |
| P2 | 改进级：细节不精准、文案待优化、非核心交互缺失 |

### 7.2 质量评分计算

- 基于 defects 中 P0/P1/P2 数量计算
- 公式：`score = 10.0 - P0×2.0 - P1×1.0 - P2×0.5`，最低 0
- 等级：≥9 高质量，≥7 基本可开发，≥5 存在明显风险，<5 不具备开发条件

---

## 八、扩展与定制

### 8.1 修改规则库

编辑 `prd_scan_rules.json`，可增加/禁用规则，或通过页面 `/test_case/prd_rules` 管理（若已实现）。

### 8.2 自定义 Stage3 报告格式

编辑 `prd_audit_prompt_stage3_minimal.txt`，调整八段内容与表格结构即可。

### 8.3 单次调用完整审计

在提示词配置中新增类型为 `prd_review` 的 prompt，内容为完整审计指令，末尾加：

```
## 输入 PRD 文档

{content}
```

前端选择该 prompt 时，将走单次调用模式。

---

## 九、依赖与运行要求

- **Python**：3.8+
- **依赖**：Flask、requests、python-docx、pydantic
- **LLM**：需配置 API Key（DeepSeek / Gemini / OpenAI 等）
- **可选**：pypdf 或 PyPDF2（PDF 解析）、飞书配置（飞书文档链接）

---

## 十、相关文件索引

| 文件 | 说明 |
|------|------|
| `views.py` | 路由、api_generate、api_analyze_prd、_build_stage3_report、_render_stage4_markdown、_run_stage3_llm_report |
| `system_model.py` | extract_prd_structure、STAGE1_STRUCTURE_PROMPT、PRD_STRUCTURE_SCHEMA |
| `prd_rule_engine.py` | run_stage2_defect_scan、STAGE2_DEFECT_SCAN_PROMPT、规则库匹配 |
| `feishu_client.py` | is_feishu_doc_url、fetch_feishu_doc_content |
| `utils/llm_client.py` | call_llm、load_llm_config |

---

*文档版本：基于 trae_platform 当前代码整理*
