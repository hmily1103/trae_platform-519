# PRD审计系统 - 架构透视与变更影响分析优化说明

> 更新日期：2026-03-30

---

## 一、优化概述

本次优化新增了两大功能模块：

1. **架构透视增强** - 在技术审计(L3)报告中新增第九节
2. **变更影响分析** - 独立的影响评估功能（需UI集成）

---

## 二、架构透视增强

### 2.1 新增章节

在 L3 技术审计报告中新增 **第九节：架构透视（功能全景分析）**，包含：

| 章节 | 内容 |
|-----|------|
| 9.1 架构概览 | 功能模块数、状态数、状态转换数、数据实体、系统入口 |
| 9.2 功能模块清单 | 模块名、层级（系统/子系统/功能）、复杂度、风险等级、依赖模块 |
| 9.3 风险热力图 | 风险类型、目标、风险等级、风险描述、风险分 |
| 9.4 核心状态机 | Mermaid 格式状态图 |
| 9.5 API接口清单 | 接口名称、HTTP方法、路径、所属模块 |
| 9.6 数据实体 | 实体名称、字段列表 |
| 9.7 测试策略建议 | 优先测试模块、自动化候选、人工测试重点 |

### 2.2 修改文件

| 文件路径 | 修改内容 |
|---------|---------|
| `modules/prd_audit/pipeline.py` | 新增 `_render_architecture_markdown()` 函数，渲染第九节内容 |

### 2.3 关键代码

```python
# pipeline.py 第1098-1180行
def _render_architecture_markdown(architecture_scan: Dict[str, Any]) -> str:
    """渲染架构透视报告为 Markdown"""
    # 9.1 架构概览
    # 9.2 功能模块清单
    # 9.3 风险热力图
    # 9.4 核心状态机
    # 9.5 API接口清单
    # 9.6 数据实体
    # 9.7 测试策略建议
```

---

## 三、变更影响分析

### 3.1 功能说明

输入需求变更描述，自动分析对系统的影响，包括：

- **受影响模块** - 哪些功能模块会受到影响
- **受影响状态** - 状态机中哪些状态/转换受影响
- **受影响API** - 哪些接口需要修改
- **受影响实体** - 数据模型变更影响
- **风险评估** - P0/P1/P2/P3 风险等级
- **测试建议** - 优先测试区域、回归测试模块

### 3.2 修改文件

| 文件路径 | 修改内容 |
|---------|---------|
| `modules/prd_audit/architecture_scanner.py` | 新增两个核心函数 |

### 3.3 核心函数

#### 3.3.1 `analyze_impact()`

```python
def analyze_impact(
    change_description: str,
    scan_result: Dict[str, Any]
) -> Dict[str, Any]:
    """
    分析需求变更对系统的影响
    
    Args:
        change_description: 变更描述（功能点/需求描述）
        scan_result: 架构扫描结果（来自 run_architecture_scan）
        
    Returns:
        影响评估报告，包含：
        - affected_modules: 受影响的功能模块
        - affected_states: 受影响的状态
        - affected_apis: 受影响的API接口
        - affected_entities: 受影响的数据实体
        - risk_assessment: 风险评估
        - test_recommendations: 测试建议
    """
```

**内部子函数：**

| 函数名 | 功能 |
|-------|------|
| `_analyze_module_impact()` | 关键词匹配模块分析（新增/修改/删除/关联） |
| `_analyze_state_impact()` | 状态机影响分析 |
| `_analyze_api_impact()` | API接口影响分析 |
| `_analyze_entity_impact()` | 数据实体影响分析 |
| `_generate_change_risk_assessment()` | 风险评估（影响分数 + P0-P3等级） |
| `_generate_change_test_recommendations()` | 测试建议生成 |

#### 3.3.2 `generate_impact_report_html()`

```python
def generate_impact_report_html(
    impact_result: Dict[str, Any],
    prd_title: str = "PRD需求变更影响分析"
) -> str:
    """生成变更影响分析HTML报告"""
```

**HTML报告包含：**

- 风险概览卡片（风险等级、影响分数）
- 受影响模块列表
- 受影响状态列表
- 受影响API列表
- 受影响实体列表
- 测试建议（优先测试区域、回归测试、人工重点）

### 3.4 使用示例

```python
from modules.prd_audit.architecture_scanner import (
    run_architecture_scan,
    analyze_impact,
    generate_impact_report_html
)

# 1. 先运行架构扫描获取基础数据
stage1_output = {
    "modules": ["投屏功能", "游戏功能", "广告管理"],
    "functional_modules": [...],
    "states": [...],
    "business_rules": [...],
    "interfaces": [...],
    "data_structures": [...]
}

scan_result = run_architecture_scan(stage1_output)

# 2. 分析变更影响
change = "新增语音点歌功能，支持用户通过语音指令搜索和点播歌曲"
impact_result = analyze_impact(change, scan_result)

# 3. 生成HTML报告
html = generate_impact_report_html(impact_result, "星耀屏PRD变更影响分析")

# 保存报告
with open("change_impact_report.html", "w", encoding="utf-8") as f:
    f.write(html)
```

---

## 四、测试验证

### 4.1 架构扫描测试

测试数据：星耀屏 PRD

| 指标 | 结果 |
|-----|------|
| 功能模块 | 7个 |
| 状态节点 | 28个 |
| API接口 | 9个 |
| 数据实体 | 3个 |
| 风险热点 | 1个 |

### 4.2 变更影响分析测试

| 测试场景 | 风险等级 | 影响分数 |
|---------|---------|---------|
| 新增语音点歌功能 | P3 (低风险) | 0 |
| 修改广告播放逻辑 | P2 (中等风险) | 6 |
| 新增接口对接第三方平台 | P0 (极高风险) | 20 |
| 新增游戏功能+优化广告策略 | P2 (中等风险) | 9 |

---

## 五、待完成事项

### 5.1 UI集成（未完成）

变更影响分析功能目前只有后端逻辑，尚未集成到 Web 界面。

**需要的开发工作：**

1. **前端页面**
   - 在 PRD 审计首页添加"变更影响分析"标签页/按钮
   - 输入区域：变更描述文本框
   - 输出区域：影响分析结果展示

2. **后端API**
   - 新增 `/api/analyze_impact` 接口
   - 调用 `analyze_impact()` 和 `generate_impact_report_html()`

---

## 六、文件清单

| 文件 | 状态 | 说明 |
|-----|------|------|
| `modules/prd_audit/architecture_scanner.py` | ✅ 已修改 | 新增 `analyze_impact()` 和 `generate_impact_report_html()` |
| `modules/prd_audit/pipeline.py` | ✅ 已修改 | 新增第九节渲染逻辑 |
| `test_change_impact.py` | ✅ 已创建 | 测试脚本 |
| `change_impact_report.html` | ✅ 已生成 | 测试报告示例 |

---

## 七、注意事项

1. **代码部署**：修改后需重启服务才能生效
2. **大模型依赖**：架构扫描依赖 PRD 解析结果，解析质量影响扫描效果
3. **变更分析**：需要先有架构扫描数据（`architecture_scan`）才能分析

---

*文档生成时间：2026-03-30*
