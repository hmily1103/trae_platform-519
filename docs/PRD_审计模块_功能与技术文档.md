# PRD 审计模块（prd_audit）— 功能与技术文档

更新时间：2026-04-07  
代码位置：`modules/prd_audit/`  
Web 入口：`GET /prd_audit/`

本文档基于当前代码实现汇总（不包含规划中未落地功能），重点描述：页面能力、后端 API、流水线输出、数据落盘与依赖约束。

---

## 1. 模块定位与边界

- 模块目标：把 PRD（文本/飞书链接/文档）转为结构化模型，并输出多层报告、风险点、测试资产与可复用知识（规则库/快照/知识卡片等）。
- 技术形态：Flask Blueprint（`prd_audit_bp`）+ 后端流水线（`pipeline.py`）+ 规则/学习仓库（`prd_scan_rules*.json` + `learning_repo/`）+ 前端单页（`templates/prd_audit_index.html`）。
- LLM 配置：与全平台共用一份配置文件（`modules/test_case/llm_config.json`），由 PRD 审计页面内「LLM 配置」读写（见 [views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L41-L46)）。

---

## 2. 页面入口与主要 UI 功能

页面路由定义在 [views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py)。

### 2.1 页面入口（GET）

- `GET /prd_audit/`：主页面（输入 PRD、生成报告、多视图切换、导出/推送/规则管理/质量大盘等）  
  模板：[prd_audit_index.html](file:///d:/trae-code/trae_platform/modules/prd_audit/templates/prd_audit_index.html)
- `GET /prd_audit/knowledge`：知识卡片页面（能力卡片读写与导入导出）
- `GET /prd_audit/learning_mvp`：学习 MVP 页面（快照学习、候选规则、发布/回滚等）
- `GET /prd_audit/rules`：规则页（规则库相关页面）
- `GET /prd_audit/bug_patterns`：Bug 模式库页面
- `GET /prd_audit/matrix_view`：矩阵视图页面
- `GET /prd_audit/platform_center`：平台中心页面

### 2.2 主页面（prd_audit_index.html）核心交互

- 输入方式：文本粘贴 / 飞书链接 / 语音输入（浏览器侧转写）/ 上传 PDF 或 Word（`.pdf`/`.docx` 后端解析回填）。
- 生成按钮：「一键体检 / 深度审计」触发 `POST /prd_audit/api/generate`，前端通过 NDJSON 流式显示进度与结果。
- 报告层级切换（按钮组）：
  - OUTLINE：认知大纲
  - L1：管理摘要
  - L2：产品分析
  - L3：技术审计（支持“文档模式/卡片模式”切换）
  - VALIDATION：验证大纲
  - TEST_CASES：测试用例生成（表格展示 + 导出 CSV/XLSX）
  - SHIFT_LEFT：测试左移资产（测试数据建议 / API 契约与 Mock / 可观测性建议）
  - IMPACT：变更影响分析
- 工具按钮：
  - 质量大盘（历史审计趋势/分布）
  - 历史记录（快照列表、两条对比）
  - 分析矩阵（跳转/展示矩阵页或矩阵区域）
  - 规则管理（规则库可视化编辑与保存）
  - 学习 MVP（跳转）
  - LLM 配置（配置主/回退模型 + 飞书 Webhook）
  - 推送摘要到飞书（将当前审计摘要通过 Webhook 推送到飞书群）
  - AI 一键优化 PRD（基于审计缺陷列表对 PRD 原文做润色补全，并提供对比预览/一键替换输入框）

前端依赖（CDN 引入）：
- `marked`（Markdown 渲染）
- `mermaid`（流程/图渲染）
- `chart.js`（质量大盘图表）

---

## 3. 后端主流程与流水线（pipeline）

### 3.1 生成接口（流式 NDJSON）

- `POST /prd_audit/api/generate`  
  入口函数：`api_generate()`（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L646)）  
  调用：`pipeline.run_prd_audit_stream(...)`（[pipeline.py](file:///d:/trae-code/trae_platform/modules/prd_audit/pipeline.py#L1464)）

输出协议：NDJSON（每行一个 JSON）
- `{"type":"status","text":"..."}`：阶段进度
- `{"type":"content","text":"..."}`：报告片段（极简模式会分块输出）
- `{"type":"bundle", ...}`：最终汇总包（见下节）

### 3.2 Bundle（最终输出包）字段

Bundle 在 [pipeline.py](file:///d:/trae-code/trae_platform/modules/prd_audit/pipeline.py#L1790-L1872) 组装，关键字段如下：

**主报告**
- `L1`：管理摘要（Markdown）
- `L2`：产品分析（Markdown）
- `L3`：技术审计（Markdown）

**结构化/增值结果**
- `test_matrix`：测试矩阵
- `diagrams`：系统图/图形类产物
- `kg`：知识图谱推理产物（如有）
- `outline_engine` / `outline_llm`：本地/LLM 的大纲产物
- `platform_impact`：平台影响分析（如有）
- `dependency_analysis`：依赖分析（如有）
- `prd_quality`：PRD 质量评估（如有）
- `test_points`：测试点集合（如有）
- `validation_outline`：验证大纲（如有）
- `risk_prediction`：风险预测（如有）
- `understanding_cards`：理解卡片（如有）
- `release_gate`：发布门禁（如有）
- `architecture_scan`：架构级扫描（如有）
- `shift_left`：测试左移资产（见第 5 节）
- `test_cases`：自动生成的测试用例数组（见第 4 节）
- `shared_summary`：共享摘要（结构化）
- `reader_guide`：导读（结构化）
- `parse_meta`：Stage1 解析元数据（blocks/parse_quality/required_elements/conflict_candidates）
- `extras_quality`：附加阶段质量信息

**用于总览仪表盘**
- `summary`：评分/数量等总览结构（dict）
- `defects`：缺陷列表（list）
- `scan_meta`：Stage2 LLM 扫描元信息（dict）

### 3.3 同步分析接口（结构化结果）

- `POST /prd_audit/api/analyze_prd`  
  用途：以一次性 JSON 返回 Stage1/2/3 的结构化结果，便于与其他模块对接（如测试用例管理）。

---

## 4. 测试用例自动生成（Test Case Generation）

### 4.1 生成方式

- 生成发生在主流水线并发阶段（与测试矩阵/系统图等并行）：  
  入口：`run_test_case_generation(...)`（[prd_rule_engine.py](file:///d:/trae-code/trae_platform/modules/prd_audit/prd_rule_engine.py)）  
  汇总字段：`bundle.test_cases`（[pipeline.py](file:///d:/trae-code/trae_platform/modules/prd_audit/pipeline.py#L1811-L1813)）

输出结构：数组，每条用例包含：
- `case_id`、`priority`、`module`、`feature`、`precondition`、`steps`、`expected`

### 4.2 导出接口

- `POST /prd_audit/api/export_test_cases`（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L2483)）
  - `format=csv`：直接用标准库 `csv` 导出（不依赖 pandas）
  - `format=xlsx`：依赖 `pandas` + `openpyxl`（缺失会返回 400）

---

## 5. 测试左移资产（Shift-Left Assets）

流水线输出字段：`bundle.shift_left`（[pipeline.py](file:///d:/trae-code/trae_platform/modules/prd_audit/pipeline.py#L1811-L1812)）

当前资产结构（dict）：
- `test_data_advisor`：测试数据建议（字段/规则/建议值）
- `api_contracts`：API 契约建议（method/path/swagger_snippet/mock_response）
- `observability_points`：可观测性建议（关键路径/日志字段/指标/告警）

展示入口：主页面「测试左移资产」页签（[prd_audit_index.html](file:///d:/trae-code/trae_platform/modules/prd_audit/templates/prd_audit_index.html)）。

---

## 6. PRD 版本对比（基于历史快照）

### 6.1 快照存储

快照保存于学习仓库目录（[audit_learning.py](file:///d:/trae-code/trae_platform/modules/prd_audit/audit_learning.py#L10-L20)）：
- `modules/prd_audit/learning_repo/snapshots/<snapshot_id>.json`
- `modules/prd_audit/learning_repo/index.json`

### 6.2 API

- `GET /prd_audit/api/history/snapshots`：列出快照索引（用于主页面“历史记录”弹窗）
- `GET /prd_audit/api/history/snapshot/<snapshot_id>`：读取单条快照
- `POST /prd_audit/api/diff_snapshots`：对比两条快照并输出 Markdown 报告（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L2422)）  
  说明：当前实现先对两个版本做 `unified diff`（每段上下文 3 行），将“差异段”作为主要输入喂给 LLM，避免长 PRD 被前缀截断导致信息丢失；并对 diff 输入做长度上限控制（默认 12000 字符，可通过 `max_chars` 参数调整）。

---

## 7. 审计规则库与学习 MVP

### 7.1 规则库文件（本地）

规则库文件位于模块目录（同 [views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L101-L107)）：
- `modules/prd_audit/prd_scan_rules_v2.json`（v2）
- `modules/prd_audit/prd_scan_rules.json`（v1）

### 7.2 规则管理 API（可视化编辑）

- `GET /prd_audit/api/rules`：读取 v2 规则库（返回 `{rules:[...]}`）
- `POST /prd_audit/api/rules`：覆盖写入 v2 规则库

写入安全性：
- `_save_json_file` 使用 `threading.Lock + tempfile + os.replace` 实现同进程并发保护与原子替换（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L144-L182)）。

### 7.3 学习 MVP（快照→候选规则→发布）

学习仓库目录：`modules/prd_audit/learning_repo/`
- 快照索引/候选规则/草稿/已应用/备份/回滚均在该目录下管理（详见 [audit_learning.py](file:///d:/trae-code/trae_platform/modules/prd_audit/audit_learning.py)）。

相关 API：
- `/api/learning/status`
- `/api/learning/lane_stats`
- `/api/learning/quality_dashboard`
- `/api/learning/build_rule_draft`
- `/api/learning/rule_candidates`
- `/api/learning/apply_candidates`
- `/api/learning/publish_applied`
- `/api/learning/backups`
- `/api/learning/rollback_backup`
- `/api/learning/outline_owner_correction`

---

## 8. LLM 配置与离线模式

### 8.1 配置文件

- 全平台共用配置文件：`modules/test_case/llm_config.json`（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L41-L46)）。

### 8.2 配置 API

- `GET /prd_audit/api/llm_config`
- `POST /prd_audit/api/llm_config`：支持更新 `llm_provider/base_url/model/api_key`，并支持 fallback 配置与 `feishu_webhook`（用于飞书推送）。

### 8.3 离线模式触发

以下情况会进入本地体检/兜底路径（以保证流程不崩）：
- 前端选择 `use_llm=false`
- LLM 配置缺失/不可用或扫描阶段返回“扫描异常”

表现：流式输出会出现“已切换为本地规则体检模式”的提示（[pipeline.py](file:///d:/trae-code/trae_platform/modules/prd_audit/pipeline.py#L1508-L1517)）。

---

## 9. 飞书推送（Webhook）

### 9.1 配置项

- `feishu_webhook`：存入 `modules/test_case/llm_config.json`（由 LLM 配置弹窗填写）。

### 9.2 推送接口

- `POST /prd_audit/api/push_to_feishu`：发送飞书交互卡片（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L2295)）。

前端入口：主页面审计报告区域的「推送摘要到飞书」按钮。

---

## 10. 文档解析与导出

### 10.1 文档解析

- `POST /prd_audit/api/parse_pdf`：PDF 解析（10MB 限制）
- `POST /prd_audit/api/parse_docx`：Word 解析（10MB 限制）

### 10.2 报告/导图导出

- `POST /prd_audit/api/export_report_docx`：导出 Word 报告
- `POST /prd_audit/api/export_feature_xmind`：导出 XMind 功能导图

---

## 11. 依赖检查

- `GET /prd_audit/api/check_dependencies`：检查 `pypdf/python-docx/requests` 是否安装（[views.py](file:///d:/trae-code/trae_platform/modules/prd_audit/views.py#L2542)）。

说明：XLSX 导出依赖 `pandas/openpyxl`，当前已纳入该接口检查项，并额外返回 `xlsx_ok` 字段用于前端提示。

---

## 12. 其他内置能力（已实现 API）

以下能力存在后端 API 与页面入口，但是否参与主页面默认流程取决于页面逻辑：

- 变更影响分析：`POST /prd_audit/api/analyze_impact`
- 聊天/问答：`POST /prd_audit/api/chat`
- 大纲相关：`POST /prd_audit/api/outline_llm`
- Bug 模式库与规则：`/api/bug/*`
- 向量检索：`POST /prd_audit/api/vector/search`
- 组合审计（PRD + bug_patterns + vector_hits）：`POST /prd_audit/api/prd/audit`
- 测试代码生成：`POST /prd_audit/api/generate_test_code`
- 规则插件与提示词中心：`/api/rule_plugins*`、`/api/prompt_center*`
- 发布门禁评估：`POST /prd_audit/api/gate/evaluate`
