# PRD 审计模块 — 功能说明与使用文档

> 模块路径：`D:\trae-code\trae_platform\modules\prd_audit`  
> 本文档汇总该目录下的功能清单、API、配置与使用方式。

---

## 一、模块概览

`prd_audit` 是 **PRD 审计独立模块**：对 PRD 文档进行结构解析、漏洞扫描与报告生成的全流程引擎。**自包含实现**，不依赖 `test_case` 代码，可单独部署；可选与用例管理联动（“保存为测试用例”跳转）。

### 1.1 访问入口

| 类型     | URL / 说明 |
|----------|------------|
| 独立页   | `GET /prd_audit/`（侧栏：**PRD 审计**） |
| 知识卡片 | `GET /prd_audit/knowledge` |
| 学习 MVP | `GET /prd_audit/learning_mvp` |
| 规则库   | `GET /prd_audit/rules` |
| Bug 模式 | `GET /prd_audit/bug_patterns` |
| 矩阵视图 | `GET /prd_audit/matrix_view` |

### 1.2 主要能力一览

- **PRD 输入**：飞书链接、纯文本、PDF、Word
- **三段式流水线**：Stage1 结构解析 → Stage2 漏洞扫描（规则 + 可选 LLM）→ Stage3 报告生成（L1/L2/L3）
- **增值分析**：测试矩阵、系统图、知识图谱推理、平台影响、依赖分析、质量 5 维、测试点、风险预测、理解卡片、发布门禁
- **报告与导出**：Markdown、Word、XMind 功能导图
- **规则与学习**：规则库维护、快照学习、候选规则生成/应用/发布/回滚
- **知识沉淀**：功能能力知识卡、Bug 模式库、向量检索
- **与平台集成**：共用 LLM 配置、保存为测试用例、从测试矩阵生成 pytest 代码

---

## 二、核心功能说明

### 2.1 PRD 输入与解析

支持三种输入方式（前端同一输入区）：

| 方式           | 说明 |
|----------------|------|
| 飞书文档链接   | 粘贴 URL，后端自动拉取正文（`feishu_client.py`） |
| 直接粘贴全文   | 最稳妥，无依赖 |
| 上传文件       | PDF / Word(.docx)，解析后填入输入框 |

**相关 API：**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/prd_audit/api/parse_pdf` | 解析 PDF，返回 `{ "text": "...", "warning": "..." }` |
| POST | `/prd_audit/api/parse_docx` | 解析 Word，返回 `{ "text": "..." }` |

**依赖：** PDF 需 `pypdf` 或 `PyPDF2`；Word 解析/导出需 `python-docx`。缺库时接口会返回安装提示。

---

### 2.2 三段式审计流水线（Stage1 / 2 / 3）

核心逻辑在 `pipeline.py`。

| 阶段   | 内容 |
|--------|------|
| **Stage1** | 结构解析：抽取模块、状态、流程、业务规则、数据结构、权限、异常/边界、依赖、非功能需求等（`system_model.py`） |
| **Stage2** | 漏洞扫描：规则库（v1 + v2）+ 可选 LLM 扫描；合并去重、锚点定位、建议补全；输出缺陷列表与异常/边界覆盖矩阵（`prd_rule_engine.py`） |
| **Stage3** | 报告生成：L3 技术审计（优先 LLM 八段三表，不合格则 Python 兜底）；L2 产品分析、L1 管理摘要（本地生成，可选 LLM 润色） |

**增值阶段（失败不影响主报告）：**

- Stage4：测试矩阵生成与质量评估  
- Stage5：系统图生成与质量评估  
- Stage2.2：PRD 理解大纲  
- Stage2.3：平台影响分析  
- Stage2.4：需求依赖分析  
- Stage2.5：知识图谱推理（缺陷→根因/传播链）  
- Stage2.6：PRD 质量 5D 评分  
- Stage4.5：测试点生成  
- Stage4.6：风险预测  
- Stage4.7：理解卡片生成  
- Stage4.8：发布门禁决策  

---

### 2.3 报告与导出

| 能力       | 方式 |
|------------|------|
| 流式生成   | `POST /prd_audit/api/generate`，返回 NDJSON（status/content/bundle） |
| 同步分析   | `POST /prd_audit/api/analyze_prd`，返回完整 JSON（raw_report_markdown、stage1/2/3、summary 等） |
| 导出 Word  | `POST /prd_audit/api/export_report_docx`，传 `stage3_json` 或 `content`（Markdown） |
| 导出 XMind | `POST /prd_audit/api/export_feature_xmind`，传 `nodes: [{ title, level }]`，下载 `PRD功能导图.xmind` |

---

### 2.4 保存为测试用例（可选）

- **API**：`POST /prd_audit/api/prepare_save_to_cases`  
- **入参**：`report_md` 或 `content`（当前报告 Markdown）  
- **行为**：写入 `session["prd_audit_report_md"]`，返回跳转 URL（`test_case` 的 PRD 分析位）  
- **前提**：平台需注册 `test_case` 蓝图；PRD 审计本身不依赖其代码。

---

## 三、规则库与学习 MVP

### 3.1 规则库文件

| 文件/目录 | 说明 |
|-----------|------|
| `prd_scan_rules_v2.json` | 正式规则库（v2），当前生效 |
| `prd_scan_rules.json`     | 旧版规则库（v1） |
| `learning_repo/snapshots/` | 每次审计快照 |
| `learning_repo/index.json` | 快照索引 |
| `learning_repo/rule_candidates.json` | 候选规则 |
| `learning_repo/prd_scan_rules_v2.draft.json` | 草案规则库 |
| `learning_repo/prd_scan_rules_v2.applied.json` | 已应用（待发布） |
| `learning_repo/prd_scan_rules_v2.backup.*.json` | 发布前备份（用于回滚） |

### 3.2 学习 MVP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/prd_audit/api/learning/status` | 学习仓库状态 |
| GET  | `/prd_audit/api/learning/lane_stats?limit=5000` | 分轨统计（local_only/llm_only/hybrid） |
| GET  | `/prd_audit/api/learning/quality_dashboard?limit=5000` | 采纳率/发布率/回滚率/Top 缺陷等 |
| POST | `/prd_audit/api/learning/build_rule_draft` | 从快照生成候选与草案（`min_count`、`max_new_rules`） |
| GET  | `/prd_audit/api/learning/rule_candidates` | 读取候选规则 |
| POST | `/prd_audit/api/learning/apply_candidates` | 应用勾选规则（`selected_rule_names`、`max_new_rules`） |
| POST | `/prd_audit/api/learning/publish_applied` | 发布为正式规则（`create_backup`） |
| GET  | `/prd_audit/api/learning/backups?limit=20` | 备份列表 |
| POST | `/prd_audit/api/learning/rollback_backup` | 按备份回滚（`backup_file_name`、`create_backup`） |

### 3.3 命令行（规则草案 / 离线训练）

```bash
# 从快照生成规则草案
python -m modules.prd_audit.build_rule_draft --min-count 2 --max-new-rules 30

# 离线批量训练（若项目提供）
python -m modules.prd_audit.batch_train_offline --input-dir <DIR> --glob "**/*.txt"
```

---

## 四、知识卡片与 Bug 模式库

### 4.1 能力知识卡片（Knowledge Cards）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/prd_audit/api/knowledge_cards?domain=ktv|all` | 读取能力卡（缺文件时用内置示例） |
| POST | `/prd_audit/api/knowledge_cards` | 保存能力卡（`items`）到 `knowledge_cards.json` |
| POST | `/prd_audit/api/knowledge_cards/import` | 导入（文件 CSV/TXT 或 JSON `items`） |
| GET  | `/prd_audit/api/knowledge_cards/export?format=json|csv` | 导出能力库 |

### 4.2 Bug 模式库（历史 Bug → 规则/模式）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/prd_audit/api/bug/import?preview=true|false` | 导入 Bug（文件/文本/JSON），可选 AI 预审 |
| GET  | `/prd_audit/api/bug/patterns` | 读取 Bug 模式 |
| POST | `/prd_audit/api/bug/patterns` | 保存 Bug 模式（`items`） |
| POST | `/prd_audit/api/bug/patterns/auto_from_bugs` | 从 bug_desc 自动生成模式 |
| POST | `/prd_audit/api/bug/analyze` | 对 bug_desc 命中模式并生成规则 |
| GET  | `/prd_audit/api/bug/rules` | 读取 Bug 规则 |
| POST | `/prd_audit/api/bug/rules` | 保存 Bug 规则 |
| GET  | `/prd_audit/api/bug/rules/export` | 导出规则 JSON |
| GET  | `/prd_audit/api/bug/patterns/export` | 导出模式 JSON |
| GET  | `/prd_audit/api/bug/raw/export` | 导出原始 Bug JSON |
| GET  | `/prd_audit/api/bug/import_template_csv` | 下载导入模板 CSV |
| GET  | `/prd_audit/api/bug/raw` | 已导入 Bug 列表 |
| POST | `/prd_audit/api/vector/search` | 本地检索（query、top_k、type、board_type） |

**组合接口**：`POST /prd_audit/api/prd/audit` — 执行完整审计后，再返回 bug_patterns 命中与 vector_hits。

---

## 五、LLM 配置与“离线模式”

- **配置入口**：页面右上角「LLM 配置」读写 **全平台共用** 的 `modules/test_case/llm_config.json`。  
- **API**：`GET /prd_audit/api/llm_config`、`POST /prd_audit/api/llm_config`。

**何时进入“本地规则体检模式”（无大模型）：**

- 前端传 `use_llm=false`；或  
- 未配置 API Key / 配置文件不存在 / 加载失败  

此时报告标题会带「本地规则体检版，无大模型推理」，缺陷仍来自规则库。

---

## 六、主流程 API 速查

### 6.1 生成（流式）

**POST** `/prd_audit/api/generate`

| 参数 | 类型 | 说明 |
|------|------|------|
| type | string | 必填 `"prd_review"` |
| content | string | PRD 内容或飞书链接 |
| use_llm | boolean | 是否启用大模型，默认 true |
| prompt | string | 可选，自定义 prompt 时走极简单次审计 |
| report_level | string | 内部用，默认 L3 |

**响应**：`application/x-ndjson`，每行 JSON：`{"type":"status","text":"..."}` 或 `{"type":"content","text":"..."}` 或 `{"type":"bundle", ...}`（最终结果包）。

### 6.2 分析（同步，返回完整 JSON）

**POST** `/prd_audit/api/analyze_prd`

| 参数 | 类型 | 说明 |
|------|------|------|
| prd_text / content | string | PRD 文本 |

**前置**：需已配置 LLM（有 API Key）。  
**响应**：`raw_report_markdown`、`stage1_output`、`stage2_output`、`stage3_output`、`summary`、`modules`、`prd_gaps`、`risks`、`plan`、`quality`、`system_model`、`rule_analysis` 等。

### 6.3 默认提示词

**GET** `/prd_audit/api/default_prd_prompt`  
返回默认 PRD 审计提示词（来自 `prd_audit_prompt_default.txt` 或内置兜底）。

### 6.4 测试代码生成

**POST** `/prd_audit/api/generate_test_code`  
**入参**：`test_matrix`（测试矩阵 JSON）。  
**说明**：需配置大模型；流式返回 pytest 代码。

---

## 七、本目录结构（关键文件）

| 文件/目录 | 说明 |
|-----------|------|
| `views.py` | 独立页与全部 API |
| `pipeline.py` | Stage1/2/3 流水线，`run_prd_audit_stream`、`run_prd_audit_sync` |
| `system_model.py` | Stage1 结构解析 |
| `prd_rule_engine.py` | Stage2 规则引擎 |
| `feishu_client.py` | 飞书文档拉取 |
| `prd_scan_rules.json` / `prd_scan_rules_v2.json` | 规则库 |
| `prd_audit_prompt_stage3_minimal.txt` | Stage3 八段报告 prompt |
| `prd_audit_prompt_default.txt` | 默认单次审计 prompt |
| `llm_config.json` | 本模块可选的独立 LLM 配置（实际页面用 test_case 共用配置） |
| `audit_learning.py` | 学习仓库与快照、规则草案 |
| `build_rule_draft.py` | 从历史快照生成规则候选与 draft |
| `templates/` | 各页面 HTML 模板 |

---

## 八、使用流程（从 0 到报告）

1. **启动**：项目根目录执行 `python app.py`，访问 `http://127.0.0.1:5000/prd_audit/`。  
2. **输入 PRD**：粘贴文本 / 飞书链接 / 上传 PDF 或 Word。  
3. **配置**：右上角「LLM 配置」填写提供方、Base URL、Model、API Key（全平台共用）。  
4. **选项**：勾选或取消「使用大模型」。  
5. **分析**：点击「开始分析」，右侧查看 L1/L2/L3，可复制、下载 .md、导出 .docx、保存为测试用例（若启用 test_case）。  

**建议节奏**：评审前跑审计；每周用学习 MVP 生成/应用少量规则；定期维护知识卡片与 Bug 模式。

---

## 九、依赖说明

- **运行依赖**：`utils.llm_client`、`utils.response`、`utils.logger`（平台通用）。  
- **可选**：若需「保存为测试用例」，平台需注册 `test_case` 蓝图；PRD 分析能力不依赖 test_case 代码。  
- **可选库**：`pypdf` 或 `PyPDF2`（PDF）、`python-docx`（Word）。

---

*文档基于当前代码整理，如有接口或文件变更请以实际代码为准。*
