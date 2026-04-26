# Trae Platform (Thunderstone)

设备与服务器测试运维平台，集成 Monkey 测试、UI 自动化、性能监控、日志监控、ARM 压测、点歌等模块。

## 功能概览

| 模块 | 路径 | 说明 |
|------|------|------|
| 仪表盘 | `/` | KPI、快捷入口、模块状态 |
| 一键任务 | `/unified/` | Monkey + 性能 + 日志 + UI 套件 + ARM 压测 串行运行 |
| 报告中心 | `/unified/reports` | 统一报告列表，支持筛选 |
| Monkey 测试 | `/monkey/` | 稳定性测试 |
| UI 自动化 | `/ui_automation/` | 录制回放、用例管理 |
| 性能监控 | `/performance_monitor/` | CPU/内存/FPS 监控 |
| 日志监控 | `/log_monitor/` | 实时日志与告警 |
| ARM 压测 | `/server_stress/` | 服务器 CPU 压测 |
| 点歌与搜索 | `/song_order/` | KTV 点歌接口 |
| 中控重启 | `/reboot/` | 设备循环重启测试 |
| 播放器压测 | `/player_stress/` | 播放器性能压测 |
| 用例管理 | `/test_case/` | 用例与套件管理 |

## 快速开始

### 环境要求

- Python 3.8+
- ADB（设备运维相关模块）

### 安装

```bash
pip install -r requirements.txt
```

### 启动

```bash
python app.py
```

访问 http://localhost:5000

### Docker 部署

```bash
docker-compose up -d
```

或构建镜像：

```bash
docker build -t trae-platform .
docker run -p 5000:5000 -v $(pwd)/data:/app/data trae-platform
```

### 配置（环境变量）

| 变量 | 说明 | 默认 |
|------|------|------|
| `LOG_DIR` | 日志目录 | `./logs` |
| `LOG_MAX_BYTES` | 单日志文件最大字节 | 10485760 (10MB) |
| `LOG_BACKUP_COUNT` | 日志备份数量 | 5 |
| `SONG_ORDER_HOST` | 点歌服务主机 | 192.168.0.208 |
| `SONG_ORDER_ROOMINFO` | 点歌房间信息 | - |
| `FLASK_SECRET_KEY` | Flask 密钥 | - |
| `ENABLE_API_AUTH` | 启用 API 认证 (1/true/yes) | 关闭 |
| `API_KEY` | API 密钥，启用认证时需在请求头携带 `X-API-Key` | - |
| `RATELIMIT_ENABLED` | 启用 API 限流（Flask-Limiter） | 开启，默认 300/分钟 |
| `RUNTIME_DATA_DIR` | Runtime 持久化目录（SQLite） | `./data/runtime` |
| `ENABLE_NEXT_TEST_RECOMMENDATION` | 压测完成后自动生成「下次测试方向」推荐 | 1（开启） |
| `RECOMMENDATION_USE_LLM` | 推荐结论是否用 LLM 润色（需配置 LLM） | 0（关闭） |

推荐引擎阈值可在 `config/recommendation_rules.json` 中调整（cpu_high_avg、mem_delta_mb_leak 等）。平台还提供 `GET /api/runs/active`（运行中一键任务）、`GET /api/devices/usage`（设备占用列表）供前端或脚本使用。

## API 文档

- **Swagger UI**：http://localhost:5000/docs
- **OpenAPI JSON**：http://localhost:5000/api/openapi.json

## 自动化测试

```bash
pytest tests/ -v
```

或运行指定测试：

```bash
pytest tests/test_api_platform.py tests/test_api_unified.py tests/test_api_song_order.py -v
```

## 项目结构

```
trae_platform/
├── app.py              # 主入口
├── config/             # 配置（公告等）
├── docs/               # 文档（OpenAPI 等）
├── modules/            # 功能模块
│   ├── monkey/
│   ├── unified/
│   ├── song_order/
│   ├── server_stress/
│   └── ...
├── shared/             # 共享组件
│   ├── core/           # 模块加载、StreamBus 等
│   └── unified/        # 报告存储
├── templates/          # 页面模板
├── static/             # 静态资源
├── tests/              # 自动化测试
└── requirements.txt
```

## 更多文档

- [**部署与分享指南**](docs/部署与分享指南.md) - 如何让别人访问你的平台
- [模块功能说明](模块功能说明.md)
- [用例管理快速入门](用例管理快速入门.md)
- [访问说明](访问说明.md)
- [Thunderstone 系统架构](docs/Thunderstone系统架构与落地对照.md)

## License

内部使用
