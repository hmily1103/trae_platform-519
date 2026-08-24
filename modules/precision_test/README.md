# 精准回归模块

精准回归用于从 `Git Diff + 需求说明` 出发，自动生成 VOD 变更影响分析、风险评估、最小验证集、执行器映射、质量门禁和测试报告。

📄 **完整说明书**：[精准回归功能说明书.md](./精准回归功能说明书.md)

它不是单纯生成测试用例，而是帮助测试负责人回答三个问题：

- 这次改动真正影响什么？
- 最少要验证哪些高价值场景？
- 当前结果是否建议上线？

## 使用入口

主平台入口：

```text
/precision_test/
```

常用流程：

```text
填写需求说明
粘贴 Git Diff 或填写 Git 仓库信息
点击生成精准回归方案
查看测试负责人解读、风险模型、最小验证集
打开对应执行器执行
同步绑定结果或人工回填结果
生成正式报告或发送飞书结论
```

## 适配场景

模块面向雷石 KTV/VOD 业务，风险分类固定收敛为：

- 点歌：搜索、点歌、收藏、队列、切歌、优先级
- 播放：起播、暂停、继续、卡顿、黑屏、无声、音画同步、解码
- 设备：启动、重启、升级、固件、ADB、机顶盒型号
- 服务端：接口、数据库、缓存、并发、房台状态
- 跨端：机顶盒、Pad、移动端、中控、服务器状态一致性
- 异常：断网、弱网、超时、重试、断电、恢复

## 核心链路

```text
Git Diff / 需求说明
-> Diff 摘要与变更类型识别
-> CodeGraph 可选影响分析
-> Agent 风险分析
-> 风险归一化
-> 最小验证集
-> 执行器映射
-> 执行结果同步
-> 质量门禁
-> 报告 / 飞书
```

## 主要文件

| 文件 | 作用 |
|---|---|
| `views.py` | 页面入口、API、Git Diff 拉取、飞书推送、执行结果同步 |
| `engine.py` | 风险归一化、测试点生成、执行器映射、质量门禁、报告生成 |
| `agent.py` | ReAct / Mastra / 本地三步分析降级链 |
| `tools.py` | Agent 只读代码检索工具 |
| `git_source.py` | GitLab 只读拉取 Diff、发布基线识别 |
| `codegraph_client.py` | 可选静态影响分析 |
| `store.py` | 分析结果落盘到 `data/precision_test/` |
| `templates/precision_test_index.html` | 前端页面 |

## 执行器映射

执行器映射不是只看风险大类，会综合测试点标题、步骤、预期、风险描述和代码证据。

| 命中内容 | 优先执行器 |
|---|---|
| 点歌、搜索、收藏、队列 | 点歌与搜索 |
| 接口、参数、幂等、并发、缓存、数据库 | API 压测 |
| 播放、播控栏、卡顿、黑屏、无声、图层、遮挡 | 播放器压测 |
| 页面、按钮、弹框、背景图、布局、扫码、Pad | UI 自动化 |
| CPU、内存、FPS、资源占用 | 性能监控 |
| Crash、ANR、异常日志、错误码 | 日志监控 |
| 跨端状态、主盒、Pad、中控、重启恢复、断网弱网 | 组合测试 |
| 启动、冷启动、升级、固件、断电 | 中控重启 |
| 稳定性、随机操作、长稳 | Monkey |
| 服务高负载、ARM、QPS、并发房台 | ARM 服务器压测 |

执行计划中会展示 `映射依据`，方便测试同学判断是否需要改成人工验证或换执行器。

## 质量门禁

状态枚举：

```text
PASS / FAIL / BLOCKED / SKIPPED / ENV_ERROR
```

门禁结论：

```text
PASS / CONDITIONAL_PASS / BLOCKED / REVIEW_REQUIRED
```

默认是观察模式：

```text
gate_mode = observe
```

观察模式只给建议，不实际阻断发布。稳定运行一段时间后，再考虑开启强制门禁。

## 评审后推进原则

当前结论是“修订后推进”，暂不建议直接开启 `enforce`。

试点前必须优先保证：

- 证据可信：执行器报告必须带 `precision_analysis_id` 和 `precision_execution_id` / `precision_test_point_id` 才能自动回填。
- 门禁确定：质量结论由确定性规则计算，结果中包含 `rule_version`。
- 修改可追溯：人工回填、确认项更新和执行器同步都会写入 `audit_log`。
- 确认项闭环：高风险 `must_confirm` 会生成 `confirmations`；未关闭时门禁为 `REVIEW_REQUIRED`，且不能 finalize。
- 旧报告隔离：同步时会忽略早于本次分析窗口的旧报告；同一执行项多份报告只取最新。
- 冲突保护：人工结果与自动同步结果冲突时，默认保留人工结果，并在 `sync_report.manual_conflicts` 中提示。
- 存储可靠：分析 JSON 和索引使用原子写入，降低并发写坏文件风险。

建议先用 3～6 个月真实变更做 observe 回放，统计召回率、漏测率、耗时和人工成本，达标后再评估强制门禁。

## 非运行时变更

模块会识别以下低风险变更：

- `.claude/`、`.codex/` 等 AI 协作配置
- Markdown / README / docs 文档
- 测试代码或测试数据

这类改动会降为 P2，并生成 3 个轻量验证点，不再推荐完整 VOD 回归。

## 常用 API

| API | 作用 |
|---|---|
| `POST /precision_test/api/analyze` | 根据 Diff 和需求生成精准回归方案 |
| `POST /precision_test/api/git/preview` | 从 Git 仓库只读拉取 Diff |
| `POST /precision_test/api/git/detect_baseline` | 自动识别发布基线 |
| `GET /precision_test/api/analyses` | 查询历史分析 |
| `GET /precision_test/api/analyses/<analysis_id>` | 查询分析详情 |
| `POST /precision_test/api/analyses/<analysis_id>/collect` | 采集执行器最近状态 |
| `POST /precision_test/api/analyses/<analysis_id>/sync_results` | 同步绑定执行结果 |
| `POST /precision_test/api/analyses/<analysis_id>/results` | 手动回填执行结果 |
| `POST /precision_test/api/analyses/<analysis_id>/finalize` | 生成正式报告 |
| `POST /precision_test/api/analyses/<analysis_id>/push_feishu` | 发送飞书质量结论 |

## 扩展建议

新增执行器映射时，优先修改 `engine.py` 中的 `EXECUTOR_ROUTING_RULES`。

新增风险分类时，需要同步调整：

- `VOD_SIGNALS`
- `CATEGORY_EXECUTORS`
- 测试点模板 `_test_templates`
- 执行器路由规则 `EXECUTOR_ROUTING_RULES`

新增执行结果自动同步时，需要调整 `views.py`：

- `_EXECUTOR_REPORT_ENDPOINTS`
- `_normalize_executor_status`
- `_report_binding`
- `_report_evidence`

当前已支持绑定结果同步的执行器：

- `ui_automation`
- `player_stress`
- `monkey`
- `log_monitor`
- `song_order`
- `combined_test`

## 测试

运行精准回归模块测试：

```bash
python -m pytest tests/test_precision_test.py -q
```

语法检查：

```bash
python -m py_compile modules/precision_test/engine.py modules/precision_test/views.py
```
