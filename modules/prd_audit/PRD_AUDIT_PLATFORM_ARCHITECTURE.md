# PRD Audit 平台级架构蓝图（可执行版）

## 1. 终极定位

- 输入：PRD（docx / md / 文本）
- 输出：评审报告 + 风险 + 状态机 + 修复策略 + 架构建议
- 系统本质：AI理解层 + 规则引擎层 + 建模层 + 决策层

## 2. 平台架构分层

- 前端层：上传、查看、对比、可视化
- 接入层：API Gateway
- 核心引擎层：
  - PRD解析引擎
  - 规则引擎
  - 状态机引擎
  - 冲突检测引擎
  - 解释引擎
  - 修复策略引擎
- 平台能力层：
  - Rule Center
  - Prompt Center
  - Knowledge Base
  - PRD Versioning
- 数据与基础设施层：
  - PostgreSQL / 向量库 / 日志系统
  - 模型服务

## 3. 标准数据流

- PRD上传
- AI解析
- 结构化
- 规则校验
- 状态机生成
- 冲突检测
- 解释生成
- 策略生成
- 报告输出

## 4. 规则系统（平台标准）

- 分层：
  - L1 core_rules
  - L2 scheduling_rules
  - L3 business_rules
- 规则字段：
  - rule_id
  - type
  - severity
  - penalty
  - category
  - check_type
  - enabled
- 平台能力：
  - 动态开关
  - 版本控制
  - 命中统计
  - 模块聚类分析

## 5. Prompt Center（平台标准）

- Stage1 结构解析
- Stage2 规则抽取
- Stage3 风险分析
- Stage4 解释生成
- Stage5 策略生成
- 必备能力：
  - Prompt版本管理
  - A/B测试
  - 效果评估

## 6. 知识库定位

- 输入：历史PRD、历史Bug、业务规则
- 用途：增强业务语义、降低幻觉、提升稳定性

## 7. 输出标准（对外能力）

- 质量评分
- 缺陷列表
- 风险分析
- 测试关注点（非用例）
- 研发重点
- 架构改造方案
- 可视化：
  - 状态机图
  - 优先级拓扑图
  - 冲突路径图

## 8. 平台差异化能力

- 多PRD版本对比
- 规则命中热度分析
- 质量趋势分析
- 自动评审Gate
  - 低于80分阻断
  - 存在P0阻断

## 9. 技术建议

- 后端：Python + FastAPI
- 图计算：networkx
- 可视化：Mermaid / Graphviz
- 存储：PostgreSQL + 向量库
- 模型：GPT / Claude / 本地模型

## 10. 分阶段落地

- Phase 1：PRD解析 + 基础规则
- Phase 2：规则引擎 + 状态机
- Phase 3：冲突检测 + 解释
- Phase 4：修复策略 + 可视化
- Phase 5：规则中心 + Prompt中心 + 知识库

## 11. 当前仓库对齐情况

- 已具备：
  - 规则引擎（deterministic_rules）
  - 状态机建模与图分析
  - 冲突解释（explainable_report）
  - 修复策略（strategy_report）
- 下一步优先：
  - 规则插件系统（多业务接入）
  - 评分指标体系平台化
  - CI/CD Gate 接入

## 12. 执行准则

- 评审目标是降低决策成本，而不仅是发现缺陷
- 报告必须可执行：策略、架构、任务三层同时给出
- 所有高风险结论必须有路径证据与影响说明
