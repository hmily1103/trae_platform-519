# PRD 审计（独立模块）— **prd_audit_clone 副本**

> 本目录为 **`prd_audit` 的可移植克隆**，与主站 `modules/prd_audit` 功能同步；URL 前缀为 **`/prd_audit_clone`**，独立运行说明见 **`STANDALONE_README.md`**。

PRD 漏洞分析功能**自包含实现**，可单独部署或分享：所有 PRD 逻辑与资源均在本目录；**独立包**不依赖 `test_case` 的代码实现。

## 访问

- **本副本独立页**：`/prd_audit_clone/`（或 `python -m modules.prd_audit_clone.standalone_app` 默认端口 5010）
- 主站对应路径：`/prd_audit/`

## 功能

- 输入：飞书文档链接 / 粘贴 PRD 全文 / 上传 PDF 或 Word
- 分析：三段式（Stage1 结构解析 → Stage2 漏洞扫描 → Stage3 八段报告，LLM 或 Python 兜底）
- 导出：复制、下载 .md、导出 .docx
- **保存为测试用例**：主站集成 test_case 时可用；**prd_audit_clone 独立包**未集成，请手动复制报告

## 本目录结构（迁移后）

- `views.py`：独立页与全部 API（生成、分析、解析 PDF/DOCX、导出 Word、默认提示词、LLM 配置、保存为测试用例）
- `pipeline.py`：Stage1/2/3 流水线及报告生成（`run_prd_audit_stream`、`run_prd_audit_sync`）
- `system_model.py`：PRD 结构解析（Stage1）
- `prd_rule_engine.py`：漏洞规则引擎（Stage2）
- `feishu_client.py`：飞书文档拉取
- `prd_scan_rules.json`：规则库
- `prd_scan_rules_v2.json`：规则库 v2
- `prd_audit_prompt_stage3_minimal.txt`：Stage3 极简八段报告 prompt
- `prd_audit_prompt_default.txt`：默认单次审计 prompt
- `llm_config.json`：本模块独立 LLM 配置（与用例管理分离）
- `audit_learning.py`：审计结果仓库与规则草案生成
- `build_rule_draft.py`：从历史样本一键生成规则候选与 `prd_scan_rules_v2.draft.json`

## 依赖

- **运行依赖**：`utils.llm_client`、`utils.response`、`utils.logger`（平台通用）
- **可选**：若需「保存为测试用例」跳转，平台需注册 `test_case` 蓝图；PRD 分析能力不依赖 test_case 代码

## 用例管理侧

- 用例管理（`test_case`）中的 PRD 分析入口（生成、分析）已改为调用本模块 `pipeline`，实现单源、避免重复维护。

## 本地学习 MVP

- 详细操作文档：`modules/prd_audit/LEARNING_MVP_GUIDE.md`
- 5分钟上手文档：`modules/prd_audit/LEARNING_MVP_QUICKSTART.md`
- 本地免费大模型部署（Ollama/LM Studio）：`modules/prd_audit/LOCAL_LLM_DEPLOY.md`
- 离线批量训练：`python -m modules.prd_audit.batch_train_offline --input-dir <DIR> --glob "**/*.txt"`
- 每次执行 PRD 审计会旁路保存快照到 `modules/prd_audit/learning_repo/snapshots/`（保存失败不影响原有分析链路）
- 索引文件：`modules/prd_audit/learning_repo/index.json`
- 生成规则草案命令：

```bash
python -m modules.prd_audit.build_rule_draft --min-count 2 --max-new-rules 30
```

- 产物：
  - `modules/prd_audit/learning_repo/rule_candidates.json`
  - `modules/prd_audit/learning_repo/prd_scan_rules_v2.draft.json`

- 可选 API：
  - `GET /prd_audit/api/learning/status`：查看学习仓库状态
  - `GET /prd_audit/api/learning/lane_stats`：分轨统计（local_only/llm_only/hybrid）
  - `GET /prd_audit/api/learning/quality_dashboard`：学习质量看板（采纳率/发布率/回滚率/Top缺陷）
  - `POST /prd_audit/api/learning/build_rule_draft`：服务端触发草案生成
  - `GET /prd_audit/api/learning/rule_candidates`：读取候选规则
  - `POST /prd_audit/api/learning/apply_candidates`：按候选规则名生成 `prd_scan_rules_v2.applied.json`
  - `POST /prd_audit/api/learning/publish_applied`：将 applied 文件发布为正式 `prd_scan_rules_v2.json`（自动备份）
  - `GET /prd_audit/api/learning/backups`：获取可回滚备份列表
  - `POST /prd_audit/api/learning/rollback_backup`：按备份文件名回滚正式规则库（可选先备份当前版本）
