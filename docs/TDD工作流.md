# TDD 工作流（Trae Platform）

本文档说明如何在当前项目中结合 **TDD（测试驱动开发）**：先写测试、再写实现，用测试驱动设计并保护回归。

---

## 1. 红-绿-重构循环

| 步骤 | 做法 |
|------|------|
| **Red（红灯）** | 先写一个**会失败**的测试，描述「期望的行为」 |
| **Green（绿灯）** | 写**最少、最简单**的实现，让该测试通过 |
| **Refactor（重构）** | 在测试保护下重构，不改变行为 |

原则：**一次只让一个测试从红变绿**，小步前进。

---

## 2. 在本项目中的约定

- **测试目录**：`tests/`
- **命名**：`test_<模块或功能>.py`，类 `Test*`，方法 `test_*`
- **运行**：在项目根目录执行  
  `pytest` 或 `pytest tests/test_xxx.py -v`
- **优先为「纯逻辑」写测试**：如 `shared/unified/orchestrator`、工具函数、解析逻辑等，不依赖 Flask 请求、ADB、外部服务，便于快速红-绿循环。

---

## 3. 新功能时的 TDD 步骤（示例）

假设要新增「根据 run_id 判断是否包含性能监控」的辅助函数：

**① Red：先写测试（此时函数还不存在或行为不符合）**

```python
# tests/test_unified_orchestrator.py
def test_run_has_performance_monitor_when_child_present():
    from shared.unified.orchestrator import create_run, set_child, get_run
    run_id = create_run({"modules": ["monkey", "performance_monitor"]})
    set_child(run_id, "performance_monitor", {"task_id": "perf_1", "session_id": "s1"})
    assert run_has_performance_monitor(run_id) is True

def test_run_has_performance_monitor_when_absent():
    run_id = create_run({"modules": ["monkey"]})
    assert run_has_performance_monitor(run_id) is False
```

运行：`pytest tests/test_unified_orchestrator.py -v` → **红灯**（未实现或断言失败）。

**② Green：实现最小逻辑**

在 `shared/unified/orchestrator.py`（或合适模块）中：

```python
def run_has_performance_monitor(run_id: str) -> bool:
    r = get_run(run_id)
    if not r:
        return False
    children = r.get("children") or {}
    return "performance_monitor" in children and isinstance(children.get("performance_monitor"), dict)
```

再跑测试 → **绿灯**。

**③ Refactor**：如有重复或可读性更好的写法，在测试保持通过的前提下重构。

---

## 4. 已有代码如何接轨 TDD

- **新需求**：严格按「先写测试（Red）→ 实现（Green）→ 重构」。
- **改旧逻辑**：  
  1. 先为「当前行为」补测试（避免改坏）。  
  2. 再改实现或加新行为，并补充/修改测试。  
  3. 用测试做回归。
- **难以单测的部分**（如强依赖 ADB、Flask 请求）：  
  - 用 **mock**（如 `unittest.mock`）把依赖挡在外面，只测本模块逻辑；  
  - 或抽一层「纯函数/纯类」到单独模块，先对这部分做 TDD。

---

## 5. 参考示例

- **纯逻辑、无外部依赖**：`tests/test_unified_orchestrator.py`（编排器 create_run / get_run / set_child / remove_run 等）
- **API 行为**：`tests/test_api_unified.py`（Flask test_client）
- **带 mock**：`tests/test_performance_service_mock.py`、`tests/test_monitor_mock.py`

建议新功能优先在「纯逻辑」上做 TDD，再通过 API 测试或集成测试覆盖接口行为。

---

## 6. 常用命令

```bash
# 运行全部测试
pytest

# 运行指定文件
pytest tests/test_unified_orchestrator.py -v

# 运行匹配名称的测试
pytest -k "orchestrator" -v

# 显示 print
pytest -s
```

---

## 7. TDD Agent（自动化红-绿-重构）

项目内置 **TDD Agent**（`tools/tdd_agent.py`），可在每次功能构建时调用，自动完成：**生成测试（Red）→ 运行测试 → 根据失败输出建议实现（Green）**。

### 7.1 使用方式

**CLI（推荐在开发新功能时执行）：**

```bash
# 项目根目录下
python -m tools.tdd_agent "需求描述" --module 目标模块路径

# 示例：为编排器新增「判断是否包含性能监控」的辅助函数
python -m tools.tdd_agent "新增 run_has_performance_monitor(run_id) 函数，根据 children 判断" --module shared.unified.orchestrator

# 仅生成测试不写入、不运行
python -m tools.tdd_agent "需求" --module shared.unified.orchestrator --no-write --no-run

# 仅运行已有测试（不生成）
python -m tools.tdd_agent --run-only tests/test_unified_orchestrator.py
```

**在代码中调用：**

```python
from tools.tdd_agent import TDDAgent, TDDCycleResult

agent = TDDAgent()
result = agent.run_cycle(
    "新增 xxx 函数，行为为 ...",
    "shared.unified.orchestrator",
    write_test_file=True,
    run_after_generate=True,
    suggest_on_failure=True,
)
# result.test_file, result.test_code, result.tests_passed, result.implementation_suggestion
```

### 7.2 应用到「每次构建/功能」

- **本地**：开发新功能前执行一次  
  `python -m tools.tdd_agent "本迭代需求简述" --module 本次改动的模块`
- **CI**：在流水线中增加一步，对本次改动的模块跑 Agent（或仅 `--run-only` 跑对应测试），失败则构建不通过。
- **无 LLM**：未配置 `llm_config` 时，Agent 仍可运行测试、生成占位测试模板；实现建议需自行根据失败输出修改。

### 7.3 参数说明

| 参数 | 说明 |
|------|------|
| `feature` | 需求描述（自然语言），用于生成测试与实现建议 |
| `--module` | 目标模块路径，如 `shared.unified.orchestrator` |
| `--no-write` | 只生成测试代码，不写入 `tests/` |
| `--no-run` | 生成后不执行 pytest |
| `--no-suggest` | 测试失败时不调用 LLM 生成实现建议 |
| `--run-only PATH` | 只运行指定测试，不生成 |

---

**总结**：用「测试先于实现」驱动设计；用「红-绿-重构」小步迭代；优先为纯逻辑和核心行为写测试；可用 **TDD Agent** 在每次功能构建时自动生成测试、跑测试并建议实现。
