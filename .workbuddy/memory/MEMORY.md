# Thunderstone 测试平台 - 项目记忆

## 项目概况

**项目名称**: Thunderstone (Trae Platform)  
**类型**: 设备与服务器测试运维平台  
**技术栈**: Python 3.8+, Flask, ADB, SSH, LLM (DeepSeek/Gemini/火山引擎)  
**代码规模**: ~226 个 Python 文件，~3.2MB 代码量

## 核心模块

| 模块 | 路径 | 主要功能 |
|------|------|---------|
| PRD 审计 | `modules/prd_audit/` | PRD 解析、规则引擎、架构扫描、CI Gate |
| 性能监控 | `modules/performance_monitor/` | CPU/内存/FPS 监控、数据存储 |
| 日志监控 | `modules/log_monitor/` | 实时日志与告警 |
| Monkey 测试 | `modules/monkey/` | 稳定性测试、批次报告 |
| UI 自动化 | `modules/ui_automation/` | 录制回放、用例管理 |
| 播放器压测 | `modules/player_stress/` | 性能压测、硬件监控 |
| ARM 压测 | `modules/server_stress/` | 服务器 CPU 压测 |

## 关键技术决策

### LLM 配置策略（2026-03-31）
- **统一入口**: `utils/llm_client.py` 提供 `call_llm()` 和 `stream_llm()`
- **多 Provider 支持**: DeepSeek（默认）、Gemini、火山引擎（豆包）
- **安全优先**: API Key 优先从环境变量读取
  - `LLM_API_KEY`: 通用
  - `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`: DeepSeek/OpenAI
  - `VOLCENGINE_API_KEY` / `ARK_API_KEY`: 火山引擎
  - `GEMINI_API_KEY`: Gemini
- **Profile 激活**: 支持 `profiles[default_profile]` 配置切换

### PRD 审计架构（2026-03-31 大版本升级）
**核心流水线**: PRD上传 → AI解析 → 结构化 → 规则校验 → 状态机生成 → 冲突检测 → 解释生成 → 策略生成 → 报告输出

**新增能力**:
- CI Gate: 质量评分<80分或存在P0级风险时阻断
- 架构扫描器: 自动提取功能架构视图、接口依赖、状态机
- Prompt 中心: 提示词版本管理、A/B测试
- 规则插件系统: 动态加载、命中统计
- 学习引擎: 基于历史Bug数据训练规则

### 配置管理
- **环境变量**: `FLASK_SECRET_KEY`, `ENABLE_API_AUTH`, `LOG_DIR` 等
- **推荐引擎**: `config/recommendation_rules.json`
- **Gate 配置**: `modules/prd_audit/gate_config.json`
- **LLM 配置**: `modules/test_case/llm_config.json`（已删除 `prd_audit/llm_config.json`）

## 用户偏好（楠姐）

**职业**: 软件测试工程师，专攻VOD点播系统测试  
**技术栈**: Android机顶盒（测试端）+ ARM服务器（后端测试）  
**工作方式**: 喜欢每天聚焦一个小功能逐步推进，不要一次性给太多信息  
**主要工作**: 功能测试、自动化测试、性能测试、缺陷定位

## 待优化事项

### 高优先级
1. **测试覆盖**: PRD 审计新增模块（`ci_gate.py`、`architecture_scanner.py`）需要补充单元测试
2. **前端优化**: `prd_audit_index.html` 单文件 2254+ 行，建议组件化拆分
3. **配置统一**: LLM 配置和规则配置分散，建议集中管理

### 中优先级
1. **文档补充**: 新增模块使用说明
2. **性能优化**: 大JSON文件（`vector_data.json`、`bug_raw.json`）考虑按需加载或数据库存储
3. **安全审计**: 定期运行 `pip-audit` 检查依赖漏洞

## 项目健康度（2026-03-31）

**整体评分**: 8.5/10  
**亮点**:
- PRD 审计平台化升级完成
- LLM 能力增强（多Provider、流式调用、安全策略）
- 性能监控深化（硬件监控、PID追踪）

**待改进**:
- 测试覆盖率不足
- 前端组件化程度低
- 配置管理分散

## 历史重要变更

### 2026-03-31
- PRD 审计系统大版本升级（CI Gate、架构扫描器、Prompt 中心、规则插件系统、学习引擎）
- LLM 统一客户端支持多Provider（DeepSeek/Gemini/火山引擎）
- 播放器压测监控增强（Rockchip 硬件监控、PID 生命周期追踪）
- 性能监控数据存储优化（日志分片、线程安全）
- 新增 `PRD_AUDIT_PLATFORM_ARCHITECTURE.md` 架构文档

### 2026-03-31 数据增长
- `bug_raw.json`: +2016 行历史Bug数据
- `vector_data.json`: +2352 行向量数据
- `knowledge_cards.json`: +578 行知识卡片
- 总体代码变更: 31个文件，+11,414 行

## 部署信息

**启动方式**:
```bash
python app.py
```

**Docker 部署**:
```bash
docker-compose up -d
```

**访问地址**: http://localhost:5000  
**API 文档**: http://localhost:5000/docs

**环境要求**:
- Python 3.8+
- ADB（设备运维相关模块）

---

### 2026-04-02
- PRD 审计变更影响分析升级（LLM 推理逻辑，views.py +130行）
- 语音录入功能上线（PRD 审计 + 克隆模块）
- 精准测试模块完善：回归用例输出、导航解耦
- 修复 JS 语法错误（文件上传）、克隆模块同步
- precision_test 模块创建（4/1），今日继续完善
- 测试状况：77 用例，72 通过（3 个 CollectorManager mock 历史遗留，2 个设备环境问题）

### 2026-04-09
- 新增 Guardrail 引擎：质量护栏，支持提示注入检测、报告结构校验
- 新增 Job Queue：异步任务队列，支持离线学习任务
- LLM 客户端增强：多Provider支持（DeepSeek/Gemini/火山引擎）、流式调用、指标采集
- PRD 审计前端升级：语音录入、审计仪表盘、分类过滤芯片
- 健康度评分：8.9/10（代码悬空8天、前端文件过大、13处裸except待清理）

*最后更新: 2026-04-09*
