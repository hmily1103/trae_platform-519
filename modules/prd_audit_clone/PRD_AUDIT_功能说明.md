## PRD 审计模块功能说明（`modules/prd_audit`）

### 一、模块整体概述

- **模块名称**：`modules/prd_audit`（PRD 审计独立模块）  
- **定位**：对 PRD 文档进行结构解析、漏洞扫描与报告生成的全流程引擎，**自包含实现**，不依赖 `test_case` 代码，可单独部署。  
- **主要能力**：
  - **PRD 输入多形态支持**：飞书链接、纯文本、PDF、Word。
  - **三段式流水线审计**：Stage1 结构解析 → Stage2 漏洞扫描（规则 + LLM）→ Stage3 报告生成（L1/L2/L3）。
  - **附加分析能力**：测试矩阵、系统图、知识图谱推理、平台影响分析、依赖分析、质量 5 维评分、测试点生成。
  - **报告产物**：Markdown 报告、可下载 Word、功能导图 XMind。
  - **规则库 & 学习 MVP**：规则库维护、快照学习、候选规则生成、应用/发布/回滚。
  - **知识卡片**：功能能力知识卡的导入/导出与维护。
  - **代码生成**：基于测试矩阵生成 pytest 自动化测试代码。
  - **与用例管理联动**：支持“保存为测试用例”跳转 `test_case` 模块。

---

### 二、前端页面与入口功能

- **独立页面入口**
  - **URL**：`GET /prd_audit/`
  - **处理函数**：`index()`（`views.py`）
  - **功能**：
    - 渲染 `prd_audit_index.html`，提供左侧 PRD 输入区、右侧审计报告展示区。
    - 页面右上角有“LLM 配置”入口（实际使用的是全平台 LLM 配置）。

- **页面主要交互功能（由后端 API 支撑）**
  - 输入 PRD（文本/链接/文件上传）
  - 选择是否 **使用大模型**（只用规则库 / 规则库 + LLM）
  - 触发 **开始分析**，实时显示流水线进度
  - 查看 **L1/L2/L3 三层报告**
  - 一键 **复制报告**、导出 `.md` / `.docx`
  - 按层级或整体 **下载**
  - **保存为测试用例**，跳转到用例管理 PRD 分析位
  - 面向运营/平台的 **学习 MVP & 规则库运营** 能力（通过一系列 `/api/learning/*` 接口）

---

### 三、对外 HTTP API 功能清单（`views.py`）

#### 1. PRD 审计主流程相关

- **流式生成审计报告**
  - **路由**：`POST /prd_audit/api/generate`
  - **函数**：`api_generate()`
  - **入参（JSON）**：
    - `type`: 必须为 `"prd_review"`。
    - `content`: PRD 内容或飞书链接。
    - `use_llm`: 是否启用大模型（默认 `true`）。
    - `prompt`: 自定义 Prompt（存在时走“极简单次审计”分支）。
    - `report_level`: 报告层级标记（内部目前统一生成 L1/L2/L3）。
  - **核心能力**：
    - 自动识别 **飞书链接**，通过 `is_feishu_doc_url` + `fetch_feishu_doc_content` 拉取正文。
    - 根据 `use_llm` 和 LLM 配置文件判断是否启用大模型：
      - 无 Key / 配置异常 / 显式 `use_llm=false` → 切至 **本地规则体检模式**（`__llm_disabled__.json`）。
    - 调用 `pipeline.run_prd_audit_stream(...)`，**以 NDJSON 流式输出**：
      - `{"type":"status","text":"Stage1..."}`
      - `{"type":"content","text":"报告内容分片"}`
      - 或 `{"type":"bundle", ...}`（最终结果包）。
    - 支持两种模式：
      - **极简单次审计**：有 `custom_prompt` → 单次 LLM 调用，直接返回一份报告。
      - **三段式完整审计**：不传 `custom_prompt` → Stage1~Stage5+学习快照。

- **同步分析 PRD（返回完整 JSON 结构）**
  - **路由**：`POST /prd_audit/api/analyze_prd`
  - **函数**：`api_analyze_prd()`
  - **入参（JSON）**：
    - `prd_text` 或 `content`：PRD 文本。
  - **前置要求**：
    - 必须已配置 LLM（读取 `modules/test_case/llm_config.json`，无 API Key 直接返回 400）。
  - **功能**：
    - 调用 `pipeline.run_prd_audit_sync(...)` 执行 Stage1/2/3。
    - 后端组装一个适配用例管理/其他模块的 **结构化分析结果**，包括：
      - `raw_report_markdown`：完整 L3 报告。
      - `status`：各阶段完成状态。
      - `summary`：总体结论 Markdown 片段。
      - `modules`：Stage1 抽取的模块列表。
      - `prd_gaps` / `risks` / `rd_focus` / `test_focus` 等文本汇总。
      - `plan`：包含整体优先级（P0/P1/P2）和建议。
      - `quality`：质量分和维度信息。
      - `stage1_output` / `stage2_output` / `stage3_output`：各阶段原始输出。
      - `system_model`：状态、规则、数据结构、权限、边界等结构化视图。
      - `rule_analysis`：Stage2 输出。
      - `test_point_matrix`：预留（此接口当前只返回空结构）。

#### 2. 文件解析能力

- **解析 PDF**
  - **路由**：`POST /prd_audit/api/parse_pdf`
  - **功能**：
    - 检查上传文件是否存在 & 扩展名 `.pdf`。
    - 使用 `pypdf` 或回退到 `PyPDF2` 提取全部页面文本。
    - 返回：
      - 成功：`{"text": "...", "warning": "部分页面解析失败...（可选）"}`。
      - 若完全提取不到文字（扫描件）：返回导入提示与警告，鼓励改用可复制文本的 PDF / Word / 粘贴正文。
    - 若没有安装解析库，返回明确错误提示（告诉运维需要 `pip install pypdf`）。

- **解析 Word (.docx)**
  - **路由**：`POST /prd_audit/api/parse_docx`
  - **功能**：
    - 检查上传文件 & 扩展名 `.docx`。
    - 使用 `python-docx` 提取段落文本，并遍历表格，拼成一段可用文本。
    - 无文本时返回 400 错误提示。
    - 未安装库时返回安装指引（`pip install python-docx`）。

#### 3. 报告导出与格式转换

- **导出审计报告为 Word**
  - **路由**：`POST /prd_audit/api/export_report_docx`
  - **入参（JSON）**：
    - `content`: Markdown 报告文本（备选）。
    - `stage3_json`: 完整 `stage3_output` JSON（优先使用）。
  - **功能**：
    - 使用 `python-docx` 构造 Word 文档：
      - 标题：来自 `stage3_json.report_title` 或默认标题。
      - 分节结构：
        - 一、总体结论  
        - 二、漏洞与风险清单（含合并后的核心问题 + 详细漏洞）  
        - 三、测试重点  
        - 四、研发重点  
        - 五、项目风险  
      - 若仅有 Markdown，则简单按 `##` / `###` 分级写入段落。
    - 自动清理不兼容 XML 控制字符，保证 python-docx 不崩。
    - 最终作为附件返回 `PRD评审报告.docx`。

#### 4. Prompt & LLM 配置

- **获取默认 PRD 审计提示词**
  - **路由**：`GET /prd_audit/api/default_prd_prompt`
  - **功能**：
    - 读取 `prd_audit_prompt_default.txt`，文件存在且长度 > 100 则返回其内容。
    - 若文件不存在或读取异常，返回内置 `FALLBACK_PRD_PROMPT`（一段通用 PRD 审计 Prompt）。
  - **作用**：
    - 前端可用于提示词展示/编辑；后端审计时也可用作兜底。

- **获取全平台共用 LLM 配置**
  - **路由**：`GET /prd_audit/api/llm_config`
  - **功能**：
    - 读取 `modules/test_case/llm_config.json`。
    - 解析 `profiles` / `default_profile`，将当前激活 profile 展平为顶层字段：`llm_provider`、`base_url`、`model`、`api_key` 等。
    - 对 `api_key` / `fallback_api_key` 做部分掩码（前 3 + **** + 后 4）。
    - 返回完整配置（供前端展示/编辑）。

- **保存全平台共用 LLM 配置**
  - **路由**：`POST /prd_audit/api/llm_config`
  - **功能**：
    - 以“多 profile”方式更新 `llm_config.json`：
      - 读取旧配置（如有）。
      - 根据请求里的 `llm_provider` 决定 profile key。
      - 合并/更新字段：`llm_provider`、`base_url`、`model`、`api_key`。
      - `api_key` / `fallback_api_key` 支持掩码回填（前端传带 `****` 的不覆盖旧值）。
      - 支持配置回退大模型相关字段：`fallback_*`。
    - 统一写回 JSON 文件，返回“配置已保存”。

#### 5. 与用例管理联动

- **将当前报告保存为测试用例草稿**
  - **路由**：`POST /prd_audit/api/prepare_save_to_cases`
  - **功能**：
    - 入参：`report_md` 或 `content`，为当前审计报告 Markdown。
    - 将报告写入 Flask session：`session["prd_audit_report_md"]`（截断至 500000 字符）。
    - 返回跳转 URL：
      - `url_for("test_case.index") + "?from_prd=1#prd_review"`
    - **用途**：
      - 前端点击“保存为测试用例” → 写入 session → 跳转用例管理页面，在对应入口读出报告并转换为测试用例。

#### 6. 学习 MVP & 规则库运营相关（`audit_learning` 系列）

- **学习仓库状态概览**
  - **路由**：`GET /prd_audit/api/learning/status`
  - **功能**：
    - 封装 `get_learning_status()`，返回快照数量、规则草案状态等全局状态。

- **学习轨道统计**
  - **路由**：`GET /prd_audit/api/learning/lane_stats`
  - **参数**：
    - `limit`：统计样本上限（1–20000，默认 5000）。
  - **功能**：
    - 调用 `get_learning_lane_stats()`，输出 `local_only / llm_only / hybrid` 等轨道的统计信息。

- **学习质量看板数据**
  - **路由**：`GET /prd_audit/api/learning/quality_dashboard`
  - **参数**：
    - `limit`：样本上限，同上。
  - **功能**：
    - 由 `get_learning_quality_dashboard()` 返回采纳率、发布率、回滚率、Top 缺陷等指标。

- **从快照生成规则草案**
  - **路由**：`POST /prd_audit/api/learning/build_rule_draft`
  - **参数**：
    - `min_count`：规则候选最小命中次数（默认 2）。
    - `max_new_rules`：最大新规则数（默认 30）。
  - **功能**：
    - 调用 `build_rule_draft_from_snapshots()`：
      - 从 `learning_repo/snapshots` 中抽取高频高危缺陷模式。
      - 生成 `rule_candidates.json` 和 `prd_scan_rules_v2.draft.json`。

- **读取候选规则列表**
  - **路由**：`GET /prd_audit/api/learning/rule_candidates`
  - **功能**：
    - 调用 `load_rule_candidates()`，返回当前候选规则列表与元数据。

- **应用候选规则**
  - **路由**：`POST /prd_audit/api/learning/apply_candidates`
  - **参数**：
    - `selected_rule_names`: 需要应用的规则名数组。
    - `max_new_rules`: 控制一次应用的最大数量（默认 100，上限 1000）。
  - **功能**：
    - 调用 `apply_selected_candidates()` 生成 `prd_scan_rules_v2.applied.json`。
    - 不直接改正式库，便于人工审核。

- **发布已应用规则为正式规则**
  - **路由**：`POST /prd_audit/api/learning/publish_applied`
  - **参数**：
    - `create_backup`: 是否先备份当前正式规则（默认 `true`）。
  - **功能**：
    - 调用 `publish_applied_rules()`：
      - 先将当前 `prd_scan_rules_v2.json` 备份到 `learning_repo/prd_scan_rules_v2.backup.*.json`。
      - 再把 applied 规则合并到正式库。
    - 若缺少文件/参数错误，返回明确 400/500 错误。

- **列出可回滚的规则备份**
  - **路由**：`GET /prd_audit/api/learning/backups`
  - **参数**：
    - `limit`：返回备份条数（1–200，默认 20）。
  - **功能**：
    - 使用 `list_rule_backups()` 返回备份文件列表（含文件名、时间等）。

- **按备份文件回滚正式规则库**
  - **路由**：`POST /prd_audit/api/learning/rollback_backup`
  - **参数**：
    - `backup_file_name`：备份文件名（必填）。
    - `create_backup`：回滚前是否再次备份当前版本（默认 `true`）。
  - **功能**：
    - 调用 `rollback_rules_from_backup()` 将正式规则回滚到指定版本。
    - 文件不存在 / 参数错误 / 其他异常分别返回 404 / 400 / 500。

#### 7. 知识卡片（功能能力知识库）

- **获取/保存功能知识卡片**
  - **路由**：`GET|POST /prd_audit/api/knowledge_cards`
  - **GET 参数**：
    - `domain`: 领域过滤（目前仅支持 KTV/ktv点歌系统/all，非这些会返回空列表）。
  - **POST 入参**：
    - `items`: 知识卡片数组。
  - **主要结构**（每条能力卡，参考内置 `KTV_CAPABILITY_CARDS`）：
    - `capability_id`、`name`、`domain`、`category`、`priority`、`trigger`、
      `preconditions`、`system_behaviors`、`exceptions`、`logging`、`acceptance_criteria`、
      `required_evidence`、`bad_patterns`、`suggestion_template` 等。
  - **存储**：
    - 文件：`knowledge_cards.json`，内部以 `{ "items": [...] }` 结构保存。
  - **能力**：
    - 支持加载默认的 KTV 能力卡片（文件不存在或解析失败时使用内置 `KTV_CAPABILITY_CARDS`）。
    - 支持前端编辑并回写自定义能力库。

- **导出能力库为 JSON 文件**
  - **路由**：`GET /prd_audit/api/knowledge_cards/export`
  - **功能**：
    - 读取当前能力卡列表，打包为 JSON（`{"items":[...]}`）并作为附件下载 `knowledge_cards.json`。

#### 8. 测试代码生成 / XMind 导出

- **根据测试矩阵生成 pytest 测试代码**
  - **路由**：`POST /prd_audit/api/generate_test_code`
  - **入参**：
    - `test_matrix`: 测试矩阵 JSON（通常来自上游 `test_matrix_generator` 的输出）。
  - **功能**：
    - 构造详细 Prompt（约束使用 pytest、fixture、与矩阵 `case_id` 对应、一部分用 `@pytest.mark.skip`/`@pytest.mark.xfail` 标记“缺失/待确认”项）。
    - 使用 `utils.llm_client.stream_chat_content` 按文本流式输出 **纯 Python 代码**。
    - 前置要求：LLM 配置必须正确（否则直接 400 返回 “生成代码需要配置大模型 API Key”）。
    - **适用场景**：从 PRD 审计结果一键生成自动化测试初稿。

- **导出功能导图为 XMind**
  - **路由**：`POST /prd_audit/api/export_feature_xmind`
  - **入参**：
    - `nodes`: 功能节点列表，格式形如 `{ "title": "...", "level": 1-6 }`。
  - **功能**：
    - 通过 `_build_xmind_file(nodes)` 生成标准 XMind 文件（内部为 `content.json` + `metadata.json` + `manifest.json` 压缩包）。
    - 根节点名为“PRD功能清单”，工作表标题为“PRD功能导图”。
    - 作为附件下载 `PRD功能导图.xmind`，可直接用 XMind 打开。

---

### 四、审计流水线内部功能（`pipeline.py`）

#### 1. 核心入口函数

- **流式审计：`run_prd_audit_stream(...)`**
  - **签名**：
    - `run_prd_audit_stream(content, llm_config_path, llm_config_override=None, timeout=90, custom_prompt=None, report_level="L3")`
  - **行为**：
    - 当 `custom_prompt` 有值时：
      - 走 **极简单次审计**，调用一次 LLM，按分片输出报告。
    - 否则执行完整流水线：

  - **Stage1：结构解析**
    - 调用 `extract_prd_structure()`（`system_model.py`）生成：
      - 功能模块列表、状态机、流程、业务规则、数据结构、权限、异常/边界、依赖、非功能需求等结构化信息。

  - **Stage2：漏洞扫描**
    - 调用 `run_stage2_defect_scan()`（`prd_rule_engine.py`）：
      - 本地规则库扫描：`prd_scan_rules.json` + `prd_scan_rules_v2.json`。
      - LLM 扫描：根据 `STAGE2_DEFECT_SCAN_PROMPT` 从结构化 PRD 提取缺陷。
      - 合并去重缺陷、补全锚点（在原文中的定位行）、补充整改建议。
      - 构建异常/边界覆盖矩阵 `coverage`。

  - **Stage3：报告生成（L3/L2/L1）**
    - 尝试使用 `prd_audit_prompt_stage3_minimal.txt` + LLM 生成符合“三表”要求的 **L3 技术审计报告**。
    - 若 LLM 报告不符合结构要求或处于 offline 模式：
      - 使用 Python 兜底 `_build_stage3_report + _render_stage4_markdown`。
    - 始终在本地构建：
      - `L2 产品分析`（`_build_l2_local_report`）
      - `L1 管理摘要`（`_build_l1_local_report`）

  - **Stage4：测试矩阵生成**
    - 调用 `TestMatrixGenerator.generate()` 与 `evaluate_test_matrix()`：
      - 生成状态/并发/边界等维度的测试矩阵 JSON，并给出质量评估。

  - **Stage5：系统图生成**
    - 调用 `DiagramGenerator.generate_all()` 与 `evaluate_diagrams()`：
      - 生成系统图（如状态图、时序图等）及其质量评估。

  - **Stage2.5：知识图谱推理**
    - `kg_inference.infer_kg()`：
      - 从缺陷 ID（尤其 v2 规则 ID）推理根因与风险传播链路。

  - **Stage2.2：PRD 理解大纲**
    - `run_outline_engine(...)`：
      - 生成结构化的大纲视图（章节、模块、锚点），为后续分析/展示提供结构。

  - **Stage2.3：平台影响分析**
    - `run_platform_impact_analysis(...)`：
      - 分析该需求对平台已有能力、配置、服务的影响。

  - **Stage2.4：需求依赖分析**
    - `run_dependency_analysis(...)`：
      - 识别对其他系统/模块/配置依赖，并给出风险提示。

  - **Stage2.6：PRD 质量 5D 引擎**
    - `run_prd_quality_5d(...)`：
      - 基于多维信息（结构 + 缺陷 + 大纲 + 依赖 + 矩阵）生成更精细的质量评分。

  - **Stage4.5：测试点生成引擎**
    - `run_test_points_engine(...)`：
      - 结合前述所有结果生成可直接用于测试设计的测试点集。

  - **学习快照保存**
    - `audit_learning.save_audit_snapshot(...)`：
      - 保存本次审计的 PRD、Stage1/2 结果、三层报告，以及测试矩阵、系统图、知识图谱、依赖分析等增量信息到 `learning_repo/snapshots`，并更新 `index.json`。

  - **最终 Bundle 输出**
    - 输出 JSON 包含：
      - `L1` / `L2` / `L3` 报告文本
      - `test_matrix` / `diagrams` / `kg` / `outline_engine` / `platform_impact` / `dependency_analysis` / `prd_quality` / `test_points`
      - `parse_meta`：解析质量信息、必备元素完成度、潜在冲突候选。
      - `extras_quality`：Stage4/5 质量标签。

- **同步审计：`run_prd_audit_sync(...)`**
  - **签名**：
    - `run_prd_audit_sync(prd_text, llm_config_path, llm_config_override=None, timeout=90)`
  - **功能**：
    - 顺序执行 Stage1/2/3（与流式版相同逻辑）。
    - 返回：
      - `merged_report`：最终 L3 报告 Markdown。
      - `stage1_output` / `stage2_output` / `stage3_output`。
    - 同样会调用 `audit_learning.save_audit_snapshot` 进行快照存储。

---

### 五、规则引擎功能（`prd_rule_engine.py`）

#### 1. 规则库驱动的缺陷检测

- **本地规则库加载**
  - 文件：
    - `prd_scan_rules.json`（v1）
    - `prd_scan_rules_v2.json`（v2）
  - `_load_rule_library()`：
    - 兼容文件中存在多 JSON 片段的情况（使用 `_extract_first_json_object` 取第一段）。
    - 只加载 `enabled == true` 的规则。

- **规则匹配逻辑**
  - **v1 规则**：基于规则名 + Stage1 结构字段/文本，内置大量启发式检测，例如：
    - `异常流程缺失`：异常/边界/覆盖不足。
    - `中断流程缺失`：有中断字样，无恢复/回到。
    - `规则边界缺失/边界条件缺失`：`edge_cases` 未写或覆盖不足。
    - `权限控制缺失`、`外部依赖未定义`、`字段定义缺失`。
    - `状态孤岛/死路/非法跳转`（状态过少或缺失）。
    - `并发操作未定义/高并发风险`、`重试机制缺失`、`接口幂等性缺失`。
    - `模糊词检测/不可测试描述`（如“适当/尽量/及时/可能/快速”等）。
    - `成功标准缺失`、`日志记录缺失`、`安全防护缺失`、`数据来源不明确`、`数据一致性风险`。
  - **v2 规则**：通过 `detector` 字段与 `_V2_DETECTORS` 对应，例如：
    - `state.missing_global_states`：全局状态集合缺失。
    - `state.missing_transitions_text`：有状态但没有切换/打断/恢复等描述。
    - `flow.success_only`：只有成功路径，异常/边界覆盖不足。
    - `flow.interrupt_without_resume`：提到中断/退出但没有恢复路径。
    - `conc.missing_arbitration`：并发/抢占/优先级规则缺失。
  - `_is_rule_applicable()`：结合 PRD 原文内容判断规则是否适用（避免“强行命中”）。

- **缺陷结构增强**
  - **锚点定位：`_find_anchor(...)`**
    - 优先使用 Stage1 的 `source_map`（模块/流程/状态对应到原文行）。
    - 否则在原 PRD 文本中基于关键词（模块名/类型/描述）多指标评分选择最合适的行，返回 `Lxx: ...` 提示。
  - **动态建议：`_dynamic_suggestion(...)`**
    - 按模块与缺陷类型生成针对性的整改建议（状态机/并发/权限/数据/异常/安全/外部依赖等维度）。

- **结果合并**
  - `run_stage2_defect_scan()`：
    - 本地规则缺陷 + LLM 缺陷合并：
      - 去重、标准化风险级别、补齐锚点与建议。
    - 构建异常/边界覆盖矩阵 `coverage`。

#### 2. 结构化引擎 `PRDRuleEngine`（基于 `SystemModel`）

- 在规则库之外，还提供传统规则引擎功能（主要用于内部质量评估）：
  - **`detect_conflicts`**：检测规则描述中的优先级冲突（如“优先级最高”同时又可被打断）。
  - **`detect_state_missing`**：遍历 状态 × 事件，对未定义转移生成问题。
  - **`detect_resume_missing`**：有打断/中断描述但没有恢复/回到说明。
  - **`detect_priority_cycle`**：优先级图中存在环（A > B, B > C, C > A）。
  - **`detect_concurrency`**：多事件并存但未定义并发行为。
  - **`detect_edge_cases`**：缺少异常/失败/超时/网络等边界描述。
  - **`analyze()`**：综合上述问题，输出：
    - `issues` 列表（含影响度/概率/风险分）。
    - `quality_score`、`dimension_scores` 与 `weighted_score`。

---

### 六、学习仓库与文件结构（高层）

- **学习仓库目录**：`learning_repo/`
  - `snapshots/`：每次审计的快照（含 PRD/缺陷/质量等）。
  - `index.json`：快照索引。
  - `rule_candidates.json`：从历史快照抽取的候选规则。
  - `prd_scan_rules_v2.draft.json`：学习生成的草案规则库。
  - `prd_scan_rules_v2.applied.json`：人工挑选后准备发布的规则。
  - `prd_scan_rules_v2.backup.*.json`：每次发布前的正式库备份。

- **正式规则库文件**
  - `prd_scan_rules_v2.json`：当前生效的规则列表。
  - `prd_scan_rules.json`：旧版规则库（v1，通常不再改）。

---

### 七、功能总结（精简视图）

- **PRD 输入与解析**
  - 支持文本、飞书链接、PDF、Word，多路径将 PRD 转为统一文本。

- **三段式审计流水线**
  - Stage1：结构解析（模块/状态/流程/规则/数据/权限/异常/依赖/非功能）。
  - Stage2：漏洞扫描（本地规则库 v1+v2 + LLM 扫描）+ 异常/边界覆盖矩阵。
  - Stage3：L3 技术审计报告（优先 LLM 八段，兜底 Python）、L2 产品分析、L1 管理摘要。

- **增值分析能力**
  - 测试矩阵生成与质量评估。
  - 系统图生成与质量评估。
  - 基于缺陷 ID 的知识图谱推理。
  - PRD 理解大纲、平台影响分析、需求依赖分析。
  - PRD 质量 5 维评分引擎。
  - 测试点自动生成。

- **报告与导出**
  - 实时流式报告输出（NDJSON）。
  - 完整 JSON 分析结果（`api/analyze_prd`）。
  - Markdown 报告。
  - `.docx` 报告导出。
  - PRD 功能导图 `.xmind` 导出。

- **规则库与学习 MVP**
  - 正式规则库（v2 + v1）加载与命中。
  - 审计快照自动保存。
  - 从历史快照生成规则候选/草案。
  - 勾选候选规则 → 应用文件 → 正式库发布 → 备份/回滚。
  - 学习状态与质量看板、分轨统计接口。

- **团队协作与知识沉淀**
  - 功能知识卡片（能力库）的读取、修改与导出。
  - 与“测试知识库”/测试策略/历史 Bug 模式联动（通过规则/建议文本体现）。

- **与平台其他模块集成**
  - 共用 LLM 配置（`modules/test_case/llm_config.json`，带多 profile 与主/备模型）。
  - “保存为测试用例”：把报告写入 session，跳转 `test_case` 模块。
  - 从测试矩阵一键生成 **pytest 自动化代码**。

