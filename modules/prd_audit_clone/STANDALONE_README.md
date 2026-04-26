# prd_audit_clone 独立运行说明

本目录是主模块 `modules/prd_audit` 的**可移植副本**：功能与主站对齐，路由前缀为 **`/prd_audit_clone`**，默认使用本目录下的 **`llm_config.json`**（不读写 `test_case`）。

## 与主站的差异

| 项目 | 主站 `prd_audit` | 本目录 `prd_audit_clone` |
|------|------------------|---------------------------|
| URL 前缀 | `/prd_audit/` | `/prd_audit_clone/` |
| LLM 配置 | `modules/test_case/llm_config.json` | `modules/prd_audit_clone/llm_config.json` |
| 保存到用例管理 | 支持（需平台注册 test_case） | **未集成**，接口返回 501，请手动复制报告 |
| 学习快照 | `learning_repo/` 各自独立 | 同步时**不覆盖**本目录已有 `learning_repo/` |

## 在本仓库内启动（开发）

在**仓库根目录**执行：

```powershell
python -m modules.prd_audit_clone.standalone_app
```

默认：<http://127.0.0.1:5010/prd_audit_clone/>  
健康检查：<http://127.0.0.1:5010/healthz>

独立进程 **始终** 使用 `modules/prd_audit_clone/standalone_templates/base.html`（仅 PRD 顶栏 + 内容区），**不会**加载运维平台整站侧栏。

环境变量（可选）：

- `PRD_AUDIT_CLONE_HOST`（默认 `127.0.0.1`）
- `PRD_AUDIT_CLONE_PORT`（默认 `5010`）
- `PRD_AUDIT_CLONE_DEBUG`（默认 `1`）
- `PRD_AUDIT_CLONE_SECRET`（生产请设置）

## 一键打包（推荐给其他电脑）

在仓库根目录执行：

```powershell
.\modules\prd_audit_clone\package_clone.ps1
```

产物在 `dist/prd_audit_clone/`：内含 `modules/`、`utils/`、`static/`、`requirements.txt`，以及将 **`standalone_templates/base.html` 复制为 `templates/base.html`**（与本地 `standalone_app` 一致，仅 PRD 界面）。

解压后双击 **`run_clone.bat`**（或 `run_clone.ps1`），会自动创建 `.venv` 并安装依赖。

## 拷贝到其他电脑的要求

一键包或完整仓库需包含：

- `modules/prd_audit_clone/`（本模块）
- `utils/`（`llm_client`、`response`、`logger` 等）
- `static/`（CSS/JS）
- `templates/base.html`（一键包已用 `bundle_base.html` 生成）
- Python 3.8+，并按 `requirements.txt` 安装依赖

配置大模型：编辑包内 **`modules/prd_audit_clone/llm_config.json`**。

## 维护：从 `prd_audit` 再同步

可使用脚本（会保留 `learning_repo` 与下列固定文件）：

```powershell
.\modules\prd_audit_clone\sync_from_prd_audit.ps1
```

脚本会还原：`__init__.py`、`standalone_app.py`、`package_clone.ps1`、`bundle_base.html`、`STANDALONE_README.md` 等，并对模板做 `/prd_audit` → `/prd_audit_clone` 替换。

**注意**：`views.py` 需在主站与 clone 之间做差异合并（LLM 路径、`prepare_save_to_cases`、蓝图导入等）。若你在本分支已有一份可用的 `views.py`，同步后应用 **IDE 对比合并** 最稳妥。

模板替换时若曾出现 **`/prd_audit_clone_clone`**，请全局改回 **`/prd_audit_clone`**。

## 文件说明

- `standalone_app.py` — Flask 入口（已把仓库根加入 `sys.path`）
- `bundle_base.html` — 一键包用的精简 `base.html` 来源
- `package_clone.ps1` — 打包脚本
- `sync_from_prd_audit.ps1` — 从 `prd_audit` 拉取代码（维护用）
