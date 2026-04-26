# PRD 审计平台

面向 KTV 行业的 PRD（产品需求文档）智能分析平台，基于"测试左移"理念，在需求阶段自动识别逻辑漏洞、缺失点、冲突等问题。

## 访问

- **独立页**：`/prd_audit/`
- 侧栏入口：**PRD 审计**

## 核心功能

### 一、输入方式

| 方式 | 描述 |
|------|------|
| 飞书文档链接 | 自动拉取飞书文档内容（需配置 APP_ID/SECRET） |
| 粘贴 PRD 全文 | 直接粘贴文本内容 |
| 上传 PDF | 自动解析 PDF 文本 |
| 上传 Word | 自动解析 DOCX 文本 |

---

### 二、分析流水线（六段式）

```
Stage1: 结构解析 → Stage2: 漏洞扫描 → Stage3: 大纲生成 → Stage4: 测试矩阵 → Stage5: 系统图 → Stage6: 报告输出
```

| Stage | 模块 | 功能 |
|-------|------|------|
| **Stage1** | `system_model` | 解析 PRD 模块结构、业务流、接口、规则 |
| **Stage2** | `prd_rule_engine` | 基于规则扫描 8 类漏洞（规则/状态机/接口/权限/校验/异常/歧义/其他） |
| **Stage3** | `outline_engine` | 生成八段式报告（大纲 + 缺陷列表 + 质量评分 + 策略建议） |
| **Stage4** | `test_matrix_generator` | 生成测试点矩阵（正常/异常/边界/并发/中断恢复） |
| **Stage5** | `diagram_generator` | 生成 Mermaid 系统图（状态机/权限矩阵/并发冲突） |
| **Stage6** | `pipeline` | 最终报告输出 + LLM 增强（可选） |

---

### 三、分析引擎（子模块）

| 引擎 | 功能 |
|------|------|
| `system_model` | PRD 结构解析（模块/业务流程/字段/规则） |
| `prd_rule_engine` | 漏洞规则引擎（多规则库支持） |
| `rules_engine` | 通用规则执行引擎 |
| `dependency_engine` | 模块依赖分析 |
| `platform_impact_engine` | 平台影响分析（KTV 硬件/软件栈） |
| `quality_engine` | PRD 质量五维评估（完整性/一致性/可测试性/风险/依赖） |
| `test_points_engine` | 测试点自动生成（功能/性能/异常/边界） |
| `test_matrix_generator` | 测试矩阵生成器 |
| `risk_prediction_engine` | 风险预测（基于历史模式） |
| `understanding_cards_engine` | PRD 理解卡片生成 |
| `architecture_scanner` | 架构透视（模块/接口/状态机/实体） |
| `diagram_generator` | 系统图生成（Mermaid） |
| `kg_inference` | 知识图谱推理 |
| `release_gate_engine` | 发布门禁评估 |
| `strategy_engine` | 修复策略报告 |
| `explain_engine` | 可解释性报告 |
| `rule_plugin_engine` | 规则插件系统 |
| `explicit_outline_engine` | 显式大纲生成 |
| `outline_llm` | LLM 大纲生成 |
| `platform_retriever` | 平台知识检索 |
| `platform_knowledge` | KTV 平台知识库 |

---

### 四、学习功能

| 功能 | 描述 |
|------|------|
| 快照保存 | 每次审计旁路保存快照到 `learning_repo/snapshots/` |
| 规则学习 | 从历史样本生成规则候选 |
| 候选管理 | 规则候选审核/应用/发布 |
| 备份回滚 | 规则库版本管理 |
| 质量看板 | 采纳率/发布率/回滚率统计 |

---

### 五、配置与集成

| 配置 | 描述 |
|------|------|
| LLM 配置 | `llm_config.json`，支持 OpenAI / 本地模型（Ollama/LM Studio） |
| Prompt 模板 | `prompt_center.json` 管理多版本 prompt |
| 规则库 | `prd_scan_rules.json` / `prd_scan_rules_v2.json` |
| 飞书集成 | `feishu_client.py`，需配置 FEISHU_APP_ID/SECRET |

---

### 六、输出/导出

- 在线阅读报告
- 复制报告内容
- 下载 .md 文件
- 导出 .docx（带格式）
- 导出 Xmind 思维导图
- **保存为测试用例** - 跳转「用例管理」模块

---

## API 列表

### 核心分析

| API | 方法 | ���能 |
|-----|------|------|
| `/api/generate` | POST | 生成 PRD 审计报告 |
| `/api/analyze_prd` | POST | PRD 完整分析 |
| `/api/chat` | POST | 交互式问答 |
| `/api/outline_llm` | POST | LLM 大纲生成 |
| `/api/analyze_impact` | POST | 影响分析 |

### 文档解析

| API | 方法 | 功能 |
|-----|------|------|
| `/api/parse_pdf` | POST | 解析 PDF |
| `/api/parse_docx` | POST | 解析 Word |
| `/api/export_report_docx` | POST | 导出 Word 报告 |

### 缺陷管理

| API | 方法 | 功能 |
|-----|------|------|
| `/api/bug/import` | POST | 批量导入缺陷 |
| `/api/bug/import/jira` | POST | 从 JIRA 导入 |
| `/api/bug/patterns` | GET/POST | 缺陷模式管理 |
| `/api/bug/patterns/auto_from_bugs` | POST | 从缺陷自动生成分类 |
| `/api/bug/analyze` | POST | 缺陷分析 |
| `/api/bug/rules` | GET/POST | 规则管理 |
| `/api/bug/patterns/export` | GET | 导出缺陷模式 |

### 测试生成

| API | 方法 | 功能 |
|-----|------|------|
| `/api/generate_test_code` | POST | 生成测试代码 |
| `/api/knowledge_cards` | GET/POST | 知识卡片 |
| `/api/test_matrix` | - | 测试矩阵视图 |

### 学习功能

| API | 方法 | 功能 |
|-----|------|------|
| `/api/learning/status` | GET | 学习状态 |
| `/api/learning/lane_stats` | GET | 分轨统计 |
| `/api/learning/quality_dashboard` | GET | 质量看板 |
| `/api/learning/build_rule_draft` | POST | 生成规则草案 |
| `/api/learning/rule_candidates` | GET | 查看候选规则 |
| `/api/learning/apply_candidates` | POST | 应用候选规则 |
| `/api/learning/publish_applied` | POST | 发布规则 |
| `/api/learning/backups` | GET | 备份列表 |
| `/api/learning/rollback_backup` | POST | 回滚备份 |

### 向量搜索

| API | 方法 | 功能 |
|-----|------|------|
| `/api/vector/search` | POST | 向量搜索（知识库） |

### 历史/快照

| API | 方法 | 功能 |
|-----|------|------|
| `/api/history/snapshots` | GET | 快照列表 |
| `/api/history/snapshot/<id>` | GET | 快照详情 |

### 配置管理

| API | 方法 | 功能 |
|-----|------|------|
| `/api/default_prd_prompt` | GET | 默认提示词 |
| `/api/llm_config` | GET/POST | LLM 配置 |
| `/api/prepare_save_to_cases` | POST | 保存为用例 |

### Prompt 管理

| API | 方法 | 功能 |
|-----|------|------|
| `/api/prompt_center` | GET/POST | Prompt 中心 |

---

## 本目录结构

```
prd_audit/
├── views.py                  # API 入口与页面路由
├── pipeline.py               # 核心流水线（Stage1-6 并行）
├── system_model.py          # Stage1 PRD 结构解析
├── prd_rule_engine.py       # Stage2 漏洞规则引擎
├── rules_engine.py          # 通用规则引擎
├── outline_engine.py       # Stage3 大纲引擎
├── outline_llm.py           # LLM 大纲生成
├── explicit_outline_engine.py # 显式大纲生成
├── quality_engine.py        # 质量五维评估
├── test_points_engine.py    # 测试点生成
├── test_matrix_generator.py # Stage4 测试矩阵
├── risk_prediction_engine.py # 风险预测
├── understanding_cards_engine.py # 理解卡片
├── architecture_scanner.py  # 架构透视分析
├── diagram_generator.py       # Stage5 系统图生成
├── kg_inference.py         # 知识图谱推理
├── dependency_engine.py   # 依赖分析
├── platform_impact_engine.py # 平台影响分析
├── platform_retriever.py   # 平台知识检索
├── platform_knowledge.py # 平台知识库
├── release_gate_engine.py # 发布门禁
├── strategy_engine.py      # 策略报告
├── explain_engine.py      # 可解释报告
├── rule_plugin_engine.py  # 规则插件
├── prompt_center.py        # Prompt 管理
├── feishu_client.py       # 飞书文档拉取
��── audit_learning.py        # 学习仓库
├── build_rule_draft.py   # 规则草案生成
├── batch_train_offline.py # 离线批量训练
├── prd_scan_rules.json    # 规则库 v1
├── prd_scan_rules_v2.json # 规则库 v2
├── llm_config.json        # LLM 配置
├── prompt_center.json      # Prompt 模板库
├── README.md             # 本文档
└── learning_repo/        # 学习仓库目录
    ├── snapshots/       # 审计快照
    ├── index.json       # 向量索引
    ├── rule_candidates.json # 候选规则
    └── backups/        # 规则库备份
```

---

## 依赖

- **运行依赖**：`utils.llm_client`、`utils.response`、`utils.logger`（平台通用）
- **可选**：飞书 API（FEISHU_APP_ID、FEISHU_APP_SECRET）

---

## 使用示例

### 1. 基础审计

```bash
curl -X POST http://localhost:5000/prd_audit/api/analyze_prd \
  -H "Content-Type: application/json" \
  -d '{"content": "PRD 内容..."}'
```

### 2. 飞书文档审计

```bash
curl -X POST http://localhost:5000/prd_audit/api/analyze_prd \
  -H "Content-Type: application/json" \
  -d '{"feishu_url": "https://xxx.feishu.cn/docx/xxx"}'
```

### 3. 生成测试矩阵

```bash
curl -X POST http://localhost:5000/prd_audit/api/generate \
  -H "Content-Type: application/json" \
  -d '{"content": "PRD 内容...", "options": ["test_matrix"]}'
```

---

## 与其他模块联动

| 模块 | 联动方式 |
|------|----------|
| `test_case` | PRD 分析结果保存为测试用例 |
| `precision_test` | 回归测试用例生成 |
| `combined_test` | 多模块组合测试 |

---

## 本地开发

```bash
# 运行审计
python -c "from modules.prd_audit.pipeline import run_prd_audit_sync; print(run_prd_audit_sync('PRD内容'))"

# 离线批量训练
python -m modules.prd_audit.batch_train_offline --input-dir <DIR> --glob "**/*.txt"

# 生成规则草案
python -m modules.prd_audit.build_rule_draft --min-count 2 --max-new-rules 30
```