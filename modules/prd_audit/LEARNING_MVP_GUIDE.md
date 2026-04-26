# PRD 审计学习 MVP 使用说明（详细版）

本文档用于说明 `PRD 审计 -> 学习MVP` 的完整使用方法，覆盖：

- 本地规则学习（关闭大模型）
- 混合学习（开启大模型）
- 候选规则生成、人工应用、发布、回滚
- 分轨统计与学习质量看板解读

---

## 1. 功能目标

学习 MVP 的目标是把每次 PRD 审计结果沉淀为可复用资产，形成闭环：

1. 审计产生样本（快照）
2. 聚合高频缺陷（候选规则）
3. 人工选择候选（应用到 applied 文件）
4. 发布到正式规则库
5. 出现问题可回滚
6. 用看板持续观察学习质量

---

## 2. 你会看到的核心区域

进入 `PRD 审计` 页面后，点击右上角 `学习MVP`，弹窗中有 5 块：

1. **学习仓库状态**  
   显示样本数、累计 P0/P1/P2、最近快照。

2. **分轨统计（新增）**  
   显示 `local_only / llm_only / hybrid / none` 的样本与风险分布。

3. **学习质量 KPI（新增）**  
   显示样本、候选、已应用、已发布、回滚，以及应用率/采纳率/发布率/回滚率。

4. **候选规则表**  
   查看并勾选候选规则，执行“应用勾选规则”或“应用全部候选”。

5. **规则备份与回滚**  
   查看备份列表，必要时回滚到某个备份版本。

---

## 3. 是否要关闭大模型

不需要强制关闭，建议按目标选择：

- **关闭大模型（use_llm=false）**：适合打规则基线，结果稳定、可复现。
- **开启大模型（use_llm=true）**：适合补充语义问题，形成 `hybrid` 样本。

最佳实践：

- 日常建议开启（拿到更多语义样本）
- 规则基线治理/噪声排查时关闭
- 两种模式都跑，确保分轨统计不是单一轨道

---

## 4. 快速上手（推荐流程）

### 步骤 A：先产出审计样本

1. 打开 `PRD 审计` 页面
2. 输入 PRD（粘贴内容、飞书链接或上传 PDF/Word）
3. 选择是否启用大模型
4. 点击 `开始分析`

每次分析结束后会自动写入快照到：

- `modules/prd_audit/learning_repo/snapshots/`
- `modules/prd_audit/learning_repo/index.json`

### 步骤 B：打开学习MVP

1. 点击右上角 `学习MVP`
2. 查看状态、分轨、KPI
3. 点击 `生成草案`

系统会生成：

- `rule_candidates.json`（候选规则）
- `prd_scan_rules_v2.draft.json`（草案）

### 步骤 C：人工筛选并应用

1. 勾选你认可的候选规则
2. 点击 `应用勾选规则`
   - 或点击 `应用全部候选`
3. 生成：
   - `prd_scan_rules_v2.applied.json`

### 步骤 D：发布到正式规则库

1. 点击 `发布为正式规则`
2. 二次确认
3. 系统自动备份旧规则，再发布新规则

发布目标文件：

- `modules/prd_audit/prd_scan_rules_v2.json`

### 步骤 E：问题回滚

1. 在备份列表中找到目标备份
2. 点击 `回滚`
3. 二次确认后完成回滚

---

## 5. 看板指标怎么解读

### 5.1 分轨统计

- `local_only`：样本只包含本地规则贡献
- `llm_only`：样本只包含 LLM 贡献
- `hybrid`：样本同时包含本地规则与 LLM 贡献
- `none`：样本没有可学习缺陷（或全被噪声过滤）

建议：

- 长期只有 `local_only`：说明你几乎没吃到语义收益
- 长期只有 `llm_only`：说明规则库覆盖可能偏弱
- `hybrid` 占比逐步上升：通常是较健康的状态

### 5.2 KPI

- 应用率（apply_rate）= 已应用自动规则 / 候选规则
- 采纳率（adoption_rate）= 已发布自动规则 / 候选规则
- 发布率（publish_rate）= 已发布自动规则 / 已应用自动规则
- 回滚率（rollback_rate）= 回滚次数 / 发布次数

建议阈值（可按团队调整）：

- 回滚率长期 > 20%：发布前审查不够严
- 发布率过低且积压高：候选质量不足或评审流程卡顿

### 5.3 Top 缺陷类型

用来确定“下一批重点治理项”：

1. 先看高频
2. 再看风险等级（P0/P1）
3. 优先沉淀为强规则

---

## 6. API 参考（可用于自动化）

### 6.1 学习状态与看板

- `GET /prd_audit/api/learning/status`
- `GET /prd_audit/api/learning/lane_stats?limit=5000`
- `GET /prd_audit/api/learning/quality_dashboard?limit=5000`

### 6.2 规则草案与候选

- `POST /prd_audit/api/learning/build_rule_draft`
  - body: `{ "min_count": 2, "max_new_rules": 30 }`
- `GET /prd_audit/api/learning/rule_candidates`
- `POST /prd_audit/api/learning/apply_candidates`
  - body: `{ "selected_rule_names": ["规则A", "规则B"], "max_new_rules": 100 }`

### 6.3 发布与回滚

- `POST /prd_audit/api/learning/publish_applied`
  - body: `{ "create_backup": true }`
- `GET /prd_audit/api/learning/backups?limit=20`
- `POST /prd_audit/api/learning/rollback_backup`
  - body: `{ "backup_file_name": "prd_scan_rules_v2.backup.xxxxx.json", "create_backup": true }`

---

## 7. 推荐运行策略（团队版）

### 每天

1. 业务同学跑 PRD 审计（建议开 LLM）
2. 测试/架构同学在学习MVP里看候选并筛选
3. 当天可应用，不一定当天发布

### 每周

1. 审查 Top 缺陷类型
2. 统一发布一次规则库
3. 观察回滚率与采纳率
4. 复盘误报，优化规则描述和建议文案

### 每月

1. 统计 `local_only / hybrid / llm_only` 轨道变化
2. 清理低价值候选
3. 固化高价值规则为长期启用项

---

## 8. 常见问题

### Q1：为什么看板里一直是 local_only？

A：你当前大概率一直关闭大模型，或大模型不可用。  
建议开启 `使用大模型` 并确认 LLM 配置可用，再观察是否出现 `hybrid`。

### Q2：候选规则很多，但不敢发布？

A：先“应用”不“发布”，跑几轮真实 PRD 回放；  
确认有效后再发布，发布前后都可用备份回滚。

### Q3：会不会把异常噪声学进去？

A：已过滤典型噪声（如扫描引擎异常、LLM 禁用占位错误），不会进入候选规则。

### Q4：发布后出问题怎么办？

A：直接在备份区回滚，系统支持一键恢复并可对当前版本再备份。

---

## 9. 最佳实践总结

1. 不要只跑一种模式，尽量形成混合样本
2. 候选规则必须人工过一遍再发布
3. 关注回滚率，回滚多就先优化候选质量
4. 以 Top 缺陷为中心滚动治理，避免规则库膨胀
5. 每次发布都保留备份，确保可逆

