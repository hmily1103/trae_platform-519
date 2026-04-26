# -*- coding: utf-8 -*-
"""
架构透视分析器：从 PRD 中提取功能架构视图

输出：
- 功能模块清单及层级关系
- 接口依赖表（模块间调用关系）
- 核心状态机（状态流转图）
- 数据实体关系
- 风险热力图（复杂度/依赖度可视化）
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class ModuleNode:
    """功能模块节点"""
    name: str
    level: int = 1  # 层级：1=系统级，2=子系统，3=功能模块
    parent: Optional[str] = None
    description: str = ""
    interfaces: List[str] = field(default_factory=list)  # 对外接口
    dependencies: List[str] = field(default_factory=list)  # 依赖的其他模块
    complexity_score: float = 0.0  # 复杂度评分
    risk_level: str = "P2"  # P0/P1/P2


@dataclass
class StateTransition:
    """状态转换"""
    from_state: str
    to_state: str
    event: str
    action: str = ""
    module: str = ""  # 所属模块


@dataclass
class DataEntity:
    """数据实体"""
    name: str
    fields: List[str] = field(default_factory=list)
    relations: List[Tuple[str, str]] = field(default_factory=list)  # (关联实体, 关系类型)
    module: str = ""


@dataclass
class ApiInterface:
    """API接口定义"""
    name: str
    method: str = "GET"  # GET/POST/PUT/DELETE等
    path: str = ""  # 接口路径
    description: str = ""
    module: str = ""  # 所属模块
    params: List[Dict[str, str]] = field(default_factory=list)  # 参数列表
    response: str = ""  # 返回类型/结构
    caller: str = ""  # 调用方
    callee: str = ""  # 被调用方


@dataclass
class ArchitectureView:
    """架构全景视图"""
    modules: List[ModuleNode] = field(default_factory=list)
    state_machine: List[StateTransition] = field(default_factory=list)
    entities: List[DataEntity] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)  # 系统入口
    risk_hotspots: List[Dict[str, Any]] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_modules_from_stage1(stage1_output: Dict[str, Any]) -> List[ModuleNode]:
    """从 Stage1 输出中提取模块信息"""
    modules: List[ModuleNode] = []
    seen_names: Set[str] = set()
    
    # 从 modules 字段提取（字符串列表）
    raw_modules = stage1_output.get("modules", [])
    for i, m in enumerate(raw_modules):
        if isinstance(m, str):
            name = _norm(m)
        elif isinstance(m, dict):
            name = _norm(m.get("name", ""))
        else:
            continue
            
        if not name or name == "【PRD未说明】" or name in seen_names:
            continue
        seen_names.add(name)
        
        # 根据名称判断层级
        level = 2
        if any(k in name for k in ["系统", "平台", "中心", "引擎"]):
            level = 1
        elif any(k in name for k in ["功能", "模块", "管理", "服务"]):
            level = 2
        elif any(k in name for k in ["按钮", "列表", "详情", "弹窗", "页面"]):
            level = 3
            
        modules.append(ModuleNode(
            name=name,
            level=level,
            description=f"PRD中识别的模块"
        ))
    
    # 从 functional_modules 字段提取（字典列表，含priority/states）
    func_modules = stage1_output.get("functional_modules", [])
    for m in func_modules:
        if isinstance(m, dict):
            name = _norm(m.get("name", ""))
            priority = m.get("priority", "P2")
            states = m.get("states", [])
        else:
            name = _norm(m)
            priority = "P2"
            states = []
            
        if not name or name == "【PRD未说明】" or name in seen_names:
            continue
        seen_names.add(name)
        
        # 根据优先级判断层级
        level = 2
        if priority == "P0":
            level = 1  # 核心系统
        elif priority == "P1":
            level = 2  # 子系统
        else:
            level = 3  # 功能模块
            
        modules.append(ModuleNode(
            name=name,
            level=level,
            description=f"优先级:{priority}, 状态:{len(states)}个" if states else f"优先级:{priority}",
            risk_level=priority
        ))
    
    # 从 flows 中提取更多模块
    flows = stage1_output.get("flows", [])
    for flow in flows:
        flow_text = _norm(flow)
        if flow_text == "【PRD未说明】":
            continue
        # 提取"xxx流程"中的xxx作为模块
        matches = re.findall(r"([\u4e00-\u9fa5]{2,8})(?:流程|操作|步骤)", flow_text)
        for m in matches:
            if m not in seen_names and len(m) >= 2:
                seen_names.add(m)
                modules.append(ModuleNode(
                    name=m,
                    level=2,
                    description=f"从流程识别"
                ))
    
    return modules


def _extract_states_from_stage1(stage1_output: Dict[str, Any]) -> List[str]:
    """提取状态列表"""
    states = []
    
    # 从 states 字段提取
    raw_states = stage1_output.get("states", [])
    states.extend([s for s in raw_states if s and s != "【PRD未说明】"])
    
    # 从 functional_modules 中提取状态
    func_modules = stage1_output.get("functional_modules", [])
    for m in func_modules:
        if isinstance(m, dict):
            module_states = m.get("states", [])
            states.extend([s for s in module_states if s and s not in states])
    
    return states


def _build_state_machine(
    stage1_output: Dict[str, Any],
    modules: List[ModuleNode]
) -> List[StateTransition]:
    """构建状态机 - 增强版：从业务规则中提取状态转换"""
    transitions: List[StateTransition] = []
    states = _extract_states_from_stage1(stage1_output)
    flows = stage1_output.get("flows", [])
    rules = stage1_output.get("business_rules", [])
    func_modules = stage1_output.get("functional_modules", [])
    
    # 创建状态到模块的映射
    state_to_module: Dict[str, str] = {}
    for m in func_modules:
        if isinstance(m, dict):
            module_name = m.get("name", "")
            for s in m.get("states", []):
                state_to_module[s] = module_name
    
    # 如果没有提取到状态，尝试从规则中识别
    if not states:
        states = _extract_states_from_rules(rules)
    
    # 从流程文本中解析状态转换
    state_pattern = r"([\u4e00-\u9fa5]{2,12})(?:状态|模式)?"
    event_pattern = r"(?:当|如果|用户|系统)([\u4e00-\u9fa5]{2,8})(?:时|后|触发)"
    
    for flow in flows:
        flow_text = _norm(flow)
        if flow_text == "【PRD未说明】":
            continue
            
        found_states = re.findall(state_pattern, flow_text)
        found_events = re.findall(event_pattern, flow_text)
        
        for i in range(len(found_states) - 1):
            event = found_events[i] if i < len(found_events) else "触发"
            from_state = found_states[i]
            to_state = found_states[i + 1]
            module = state_to_module.get(from_state, modules[0].name if modules else "")
            
            transitions.append(StateTransition(
                from_state=from_state,
                to_state=to_state,
                event=event,
                action="",
                module=module
            ))
    
    # 从业务规则中解析状态转换（增强版）
    transitions.extend(_parse_transitions_from_rules(rules, states, state_to_module, modules))
    
    return transitions


def _extract_states_from_rules(rules: List[str]) -> List[str]:
    """从业务规则中提取状态名称"""
    states = []
    
    for rule in rules:
        rule_text = _norm(rule)
        if rule_text == "【PRD未说明】":
            continue
        
        # 匹配 "xx状态"、"xx模式"、"xx中" 等标准状态描述
        patterns = [
            r"([\u4e00-\u9fa5]{2,8}状态)",  # 投屏中状态
            r"([\u4e00-\u9fa5]{2,8}模式)",  # 横屏模式
            r"([\u4e00-\u9fa5]{2,8}中)",    # 播放中
            r"([\u4e00-\u9fa5]{2,8}展示)",  # 广告展示
            r"([\u4e00-\u9fa5]{2,8}播放)",  # 广告播放
            r"(?:进入|切换到|转至)([\u4e00-\u9fa5]{2,8})(?:状态|模式)?",  # 进入投屏
            r"(?:退出|离开)([\u4e00-\u9fa5]{2,8})(?:状态|模式)?",  # 退出投屏
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, rule_text)
            for m in matches:
                # 清理状态名
                state_name = _clean_state_name(m)
                if state_name and state_name not in states and len(state_name) >= 2:
                    states.append(state_name)
    
    return states


def _clean_state_name(name: str) -> str:
    """清理状态名称，去除前缀后缀"""
    name = name.strip()
    # 去除常见前缀
    prefixes = ["进入到", "进入", "切换到", "转至", "退出", "离开", "如有", "如此时正在"]
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
    # 去除常见后缀
    suffixes = ["默认展", "默认展示页面"]
    for suffix in suffixes:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[:-len(suffix)]
    return name.strip()


def _parse_transitions_from_rules(
    rules: List[str], 
    states: List[str],
    state_to_module: Dict[str, str],
    modules: List[ModuleNode]
) -> List[StateTransition]:
    """从业务规则中解析状态转换关系"""
    transitions: List[StateTransition] = []
    
    # 定义转换模式（按优先级排序）
    transition_patterns = [
        # 1. 优先级抢占: "投屏>游戏>广告>..."
        (r"([\u4e00-\u9fa5]{2,8})\s*>\s*([\u4e00-\u9fa5]{2,8})", "优先级抢占"),
        # 2. 进入打断: "进入投屏：如此时正在播放广告，则直接切断..."
        (r"进入([\u4e00-\u9fa5]{2,8})[：:].*?(?:正在|当前)([\u4e00-\u9fa5]{2,8})[，,].*?(?:切断|中断|停止)", "进入打断"),
        # 3. 退出切换: "退出投屏：检测广告，如有广告展示，则播放广告"
        (r"退出([\u4e00-\u9fa5]{2,8})[：:].*?(?:检测|检查).*?([\u4e00-\u9fa5]{2,8})", "退出切换"),
        # 4. 完成后恢复: "展示完毕后，则继续播放广告"
        (r"([\u4e00-\u9fa5]{2,8})完毕后?[，,].*?(?:继续|恢复|开始)([\u4e00-\u9fa5]{2,8})", "完成恢复"),
        # 5. 打断后恢复: "广告被打断。退出后继续广告展示"
        (r"([\u4e00-\u9fa5]{2,8})被打断.*?(?:退出|结束后?).*?(?:继续|恢复)([\u4e00-\u9fa5]{2,8})", "打断恢复"),
        # 6. 条件触发: "指定模式场景...展示广告，则被打断"
        (r"指定模式场景.*?(?:此时|则)([\u4e00-\u9fa5]{2,8})[，,].*?被打断", "场景打断"),
        # 7. 移动展示: "AI数字人移动至星耀屏展示"
        (r"([\u4e00-\u9fa5]{2,8})移动至([\u4e00-\u9fa5]{2,8})", "移动展示"),
        # 8. 使用时切换: "星耀屏被使用时，在tv端展示"
        (r"([\u4e00-\u9fa5]{2,8})被使用时?[，,].*?(?:在|于)([\u4e00-\u9fa5]{2,8})", "使用切换"),
    ]
    
    for rule in rules:
        rule_text = _norm(rule)
        if rule_text == "【PRD未说明】":
            continue
        
        for pattern, event_name in transition_patterns:
            matches = re.findall(pattern, rule_text)
            for match in matches:
                # 处理单个匹配结果（可能是字符串或元组）
                if isinstance(match, tuple):
                    if len(match) >= 2:
                        from_s, to_s = match[0], match[1]
                    else:
                        continue
                else:
                    # 如果是单个字符串，跳过（不符合预期格式）
                    continue
                # 清理状态名
                from_state = _clean_state_name(from_s)
                to_state = _clean_state_name(to_s)
                
                # 尝试匹配到已知状态
                from_state = _match_state(from_state, states)
                to_state = _match_state(to_state, states)
                
                if from_state and to_state and from_state != to_state:
                    # 去重检查
                    exists = any(
                        t.from_state == from_state and t.to_state == to_state 
                        for t in transitions
                    )
                    if not exists:
                        module = state_to_module.get(from_state, "")
                        if not module and modules:
                            module = modules[0].name
                        
                        transitions.append(StateTransition(
                            from_state=from_state,
                            to_state=to_state,
                            event=event_name,
                            action=rule_text[:60],
                            module=module
                        ))
    
    return transitions


def _match_state(candidate: str, states: List[str]) -> str:
    """将候选字符串匹配到已知状态列表"""
    # 直接匹配
    if candidate in states:
        return candidate
    
    # 尝试添加/移除 "状态" 后缀
    if candidate + "状态" in states:
        return candidate + "状态"
    if candidate.endswith("状态") and candidate[:-2] in states:
        return candidate[:-2]
    
    # 模糊匹配：包含关系
    for s in states:
        if candidate in s or s in candidate:
            return s
    
    # 如果没匹配到，返回原字符串（可能是新状态）
    return candidate


def _extract_api_interfaces(stage1_output: Dict[str, Any], modules: List[ModuleNode]) -> List[ApiInterface]:
    """从PRD中提取API接口定义 - 增强版"""
    interfaces: List[ApiInterface] = []
    seen_interfaces: Set[str] = set()
    
    # 1. 从 interfaces 字段提取（标准格式）
    raw_interfaces = stage1_output.get("interfaces", [])
    for iface in raw_interfaces:
        if isinstance(iface, dict):
            name = _norm(iface.get("name", ""))
            if not name:
                continue
                
            api = ApiInterface(
                name=name,
                method=iface.get("method", "GET"),
                path=iface.get("path", ""),
                description=_norm(iface.get("description", "")),
                module=_norm(iface.get("module", "")),
                caller=_norm(iface.get("caller", "")),
                callee=_norm(iface.get("callee", ""))
            )
            
            # 提取参数
            params = iface.get("params", [])
            if isinstance(params, list):
                api.params = [{"name": p, "type": "string"} if isinstance(p, str) else p for p in params]
            
            key = f"{api.method}:{api.path or api.name}"
            if key not in seen_interfaces:
                seen_interfaces.add(key)
                interfaces.append(api)
    
    # 2. 从 api_definitions 字段提取（如果有）
    api_defs = stage1_output.get("api_definitions", [])
    for api_def in api_defs:
        if isinstance(api_def, dict):
            name = _norm(api_def.get("name", ""))
            if not name:
                continue
                
            key = f"{api_def.get('method', 'GET')}:{api_def.get('path', name)}"
            if key not in seen_interfaces:
                seen_interfaces.add(key)
                interfaces.append(ApiInterface(
                    name=name,
                    method=api_def.get("method", "GET"),
                    path=api_def.get("path", ""),
                    description=_norm(api_def.get("description", "")),
                    module=_norm(api_def.get("module", "")),
                    params=api_def.get("params", []),
                    response=api_def.get("response", "")
                ))
    
    # 3. 从业务规则中识别接口调用模式
    rules = stage1_output.get("business_rules", [])
    flows = stage1_output.get("flows", [])
    all_text = " ".join(rules + flows)
    
    # 识别接口调用模式
    interface_patterns = [
        # "调用xx接口"
        (r"调用([\u4e00-\u9fa5]{2,12})接口", "调用"),
        # "请求xx数据"
        (r"请求([\u4e00-\u9fa5]{2,12})(?:数据|接口)", "请求"),
        # "获取xx信息"
        (r"获取([\u4e00-\u9fa5]{2,12})(?:信息|数据|列表)", "获取"),
        # "提交xx"
        (r"提交([\u4e00-\u9fa5]{2,12})", "提交"),
        # "发送xx"
        (r"发送([\u4e00-\u9fa5]{2,12})", "发送"),
        # "同步xx"
        (r"同步([\u4e00-\u9fa5]{2,12})", "同步"),
        # "查询xx"
        (r"查询([\u4e00-\u9fa5]{2,12})", "查询"),
        # "推送xx"
        (r"推送([\u4e00-\u9fa5]{2,12})", "推送"),
    ]
    
    for pattern, action in interface_patterns:
        matches = re.findall(pattern, all_text)
        for match in matches:
            iface_name = f"{action}{match}"
            if iface_name not in seen_interfaces:
                seen_interfaces.add(iface_name)
                
                # 推断HTTP方法
                method = "GET"
                if action in ["提交", "发送", "同步"]:
                    method = "POST"
                elif action in ["更新", "修改"]:
                    method = "PUT"
                elif action in ["删除", "移除"]:
                    method = "DELETE"
                
                # 推断所属模块
                module = ""
                for m in modules:
                    if m.name in iface_name or iface_name in m.name:
                        module = m.name
                        break
                
                interfaces.append(ApiInterface(
                    name=iface_name,
                    method=method,
                    description=f"从业务规则识别的接口：{action}{match}",
                    module=module
                ))
    
    # 4. 从数据流中识别
    data_flows = stage1_output.get("data_flows", [])
    for flow in data_flows:
        if isinstance(flow, dict):
            source = _norm(flow.get("source", ""))
            target = _norm(flow.get("target", ""))
            data = _norm(flow.get("data", ""))
            
            if source and target and data:
                iface_name = f"{source}→{target}:{data[:10]}"
                if iface_name not in seen_interfaces:
                    seen_interfaces.add(iface_name)
                    interfaces.append(ApiInterface(
                        name=f"传输{data[:8]}",
                        description=f"数据流：{source} → {target}",
                        caller=source,
                        callee=target
                    ))
    
    # 5. 关联接口到模块
    for api in interfaces:
        if not api.module:
            # 根据名称匹配模块
            for m in modules:
                if m.name in api.name or api.name in m.name:
                    api.module = m.name
                    break
        
        # 更新模块的接口列表
        for m in modules:
            if m.name == api.module or api.name in m.name or m.name in api.name:
                if api.name not in m.interfaces:
                    m.interfaces.append(api.name)
                if api.callee and api.callee not in m.dependencies:
                    m.dependencies.append(api.callee)
    
    return interfaces


def _extract_interface_dependencies(stage1_output: Dict[str, Any], modules: List[ModuleNode]) -> None:
    """从 stage1_output 中提取接口依赖关系，更新模块的 dependencies"""
    interfaces = stage1_output.get("interfaces", [])
    
    # 创建模块名到模块对象的映射
    module_map = {m.name: m for m in modules}
    
    for iface in interfaces:
        if isinstance(iface, dict):
            caller = _norm(iface.get("caller", ""))
            callee = _norm(iface.get("callee", ""))
            iface_name = _norm(iface.get("name", ""))
            
            # 找到调用方模块
            for m in modules:
                if caller in m.name or m.name in caller:
                    if callee and callee not in m.dependencies:
                        m.dependencies.append(callee)
                    if iface_name and iface_name not in m.interfaces:
                        m.interfaces.append(iface_name)
                    break
    
    # 从业务规则中推断依赖关系
    rules = stage1_output.get("business_rules", [])
    module_names = [m.name for m in modules]
    
    for rule in rules:
        rule_text = _norm(rule)
        # 找"A调用B"、"A依赖B"等模式
        for i, name_a in enumerate(module_names):
            if name_a in rule_text:
                for name_b in module_names:
                    if name_b != name_a and name_b in rule_text:
                        # 检查是否有依赖关键词在它们之间
                        pattern = f"{name_a}.*?({'|'.join(['调用', '依赖', '使用', '请求', '触发'])}).*?{name_b}"
                        if re.search(pattern, rule_text):
                            if name_b not in modules[i].dependencies:
                                modules[i].dependencies.append(name_b)


def _extract_data_entities(stage1_output: Dict[str, Any]) -> List[DataEntity]:
    """提取数据实体"""
    entities: List[DataEntity] = []
    seen_names: Set[str] = set()
    
    # 从 data_structures 中提取（支持字典格式）
    data_structures = stage1_output.get("data_structures", [])
    for ds in data_structures:
        if isinstance(ds, dict):
            name = ds.get("name", "")
            fields = ds.get("fields", [])
            if name and name not in seen_names:
                seen_names.add(name)
                entities.append(DataEntity(name=name, fields=fields, module=""))
        else:
            text = _norm(ds)
            if text == "【PRD未说明】":
                continue
            # 提取实体名（通常是开头部分）
            name_match = re.match(r"([\u4e00-\u9fa5]{2,8})(?:数据|对象|实体|表)?", text)
            if name_match:
                entity_name = name_match.group(1)
                if entity_name not in seen_names:
                    seen_names.add(entity_name)
                    # 提取字段
                    fields = re.findall(r"([\u4e00-\u9fa5a-zA-Z_]{2,12})[：:]", text)
                    entities.append(DataEntity(
                        name=entity_name,
                        fields=fields[:10],
                        module=""
                    ))
    
    # 从 entities 字段提取（如果存在）
    raw_entities = stage1_output.get("entities", [])
    for e in raw_entities:
        if isinstance(e, dict):
            name = e.get("name", "")
            fields = e.get("fields", [])
            if name and name not in seen_names:
                seen_names.add(name)
                entities.append(DataEntity(name=name, fields=fields, module=""))
    
    # 如果没有显式定义，从业务规则中猜测
    if not entities:
        common_entities = ["用户", "订单", "商品", "歌曲", "播放记录", "配置", "投屏任务", "广告任务", "游戏任务"]
        rules_text = " ".join(stage1_output.get("business_rules", []))
        for e in common_entities:
            if e in rules_text and e not in seen_names:
                seen_names.add(e)
                entities.append(DataEntity(name=e, module=""))
    
    return entities


def _calculate_complexity(
    module: ModuleNode,
    all_modules: List[ModuleNode],
    transitions: List[StateTransition],
    entities: List[DataEntity],
    stage1_output: Dict[str, Any] = None
) -> float:
    """计算模块复杂度 - 增强版：多维度精细计算"""
    score = 0.0
    details = []
    
    # 1. 依赖复杂度（加权计算）
    dep_count = len(module.dependencies)
    # 被其他模块依赖的数量（入度）
    depended_count = sum(1 for m in all_modules if module.name in m.dependencies)
    dep_score = dep_count * 0.8 + depended_count * 1.2
    score += min(dep_score, 5.0)
    if dep_count > 0:
        details.append(f"依赖{dep_count}个模块")
    if depended_count > 0:
        details.append(f"被{depended_count}个模块依赖")
    
    # 2. 接口复杂度
    iface_count = len(module.interfaces)
    iface_score = iface_count * 0.5
    score += min(iface_score, 3.0)
    if iface_count > 0:
        details.append(f"对外提供{iface_count}个接口")
    
    # 3. 状态机复杂度（考虑状态数量和转换路径）
    module_transitions = [t for t in transitions if t.module == module.name or module.name in t.from_state or module.name in t.to_state]
    related_states = set()
    for t in module_transitions:
        related_states.add(t.from_state)
        related_states.add(t.to_state)
    state_count = len(related_states)
    transition_count = len(module_transitions)
    # 状态复杂度 = 状态数 * 0.5 + 转换数 * 0.3
    state_score = state_count * 0.6 + transition_count * 0.4
    score += min(state_score, 4.0)
    if state_count > 0:
        details.append(f"涉及{state_count}个状态,{transition_count}条转换")
    
    # 4. 数据实体复杂度
    module_entities = [e for e in entities if e.module == module.name or module.name in e.name]
    entity_count = len(module_entities)
    field_count = sum(len(e.fields) for e in module_entities)
    entity_score = entity_count * 0.5 + field_count * 0.1
    score += min(entity_score, 2.5)
    if entity_count > 0:
        details.append(f"管理{entity_count}个实体,{field_count}个字段")
    
    # 5. 层级权重（系统级模块天然更复杂）
    level_weights = {1: 2.0, 2: 1.0, 3: 0.5}
    score += level_weights.get(module.level, 0.5)
    
    # 6. 业务规则复杂度（从PRD中提取）
    if stage1_output:
        rules = stage1_output.get("business_rules", [])
        module_rules = [r for r in rules if module.name in str(r)]
        rule_count = len(module_rules)
        # 规则越多越复杂，但超过10条后边际递减
        rule_score = min(rule_count * 0.3, 3.0)
        score += rule_score
        if rule_count > 0:
            details.append(f"包含{rule_count}条业务规则")
    
    # 7. 优先级风险加成
    priority_bonus = {"P0": 2.0, "P1": 1.0, "P2": 0.0, "P3": -0.5}
    score += priority_bonus.get(module.risk_level, 0)
    
    # 8. 名称关键词复杂度（某些关键词暗示复杂功能）
    complexity_keywords = {
        "引擎": 1.5, "调度": 1.5, "管理": 1.0, "控制": 1.0,
        "同步": 1.0, "异步": 1.0, "并发": 1.5, "缓存": 0.8,
        "队列": 0.8, "事务": 1.0, "安全": 1.0, "权限": 0.8
    }
    for keyword, bonus in complexity_keywords.items():
        if keyword in module.name:
            score += bonus
            details.append(f"含复杂关键词'{keyword}'")
            break  # 只加一次
    
    final_score = round(min(score, 10.0), 1)
    
    # 更新模块描述，加入复杂度详情
    if details:
        module.description = module.description.split(" | ")[0] + " | " + "; ".join(details[:3])
    
    return final_score


def _identify_risk_hotspots(
    modules: List[ModuleNode],
    transitions: List[StateTransition],
    entities: List[DataEntity],
    stage1_output: Dict[str, Any] = None
) -> List[Dict[str, Any]]:
    """识别风险热点 - 增强版：更多风险类型和精细评估"""
    hotspots: List[Dict[str, Any]] = []
    
    # 1. 高度依赖的模块（中心节点）- 考虑入度和出度
    in_degree: Dict[str, int] = {}  # 被依赖次数
    out_degree: Dict[str, int] = {}  # 依赖其他次数
    
    for m in modules:
        out_degree[m.name] = len(m.dependencies)
        for dep in m.dependencies:
            in_degree[dep] = in_degree.get(dep, 0) + 1
    
    # 高入度节点（被多个模块依赖）
    for mod_name, count in in_degree.items():
        if count >= 2:
            risk_level = "P0" if count >= 5 else ("P1" if count >= 3 else "P2")
            hotspots.append({
                "type": "中心节点",
                "target": mod_name,
                "risk": f"被{count}个模块依赖，变更影响面广，需重点回归测试",
                "level": risk_level,
                "score": min(count * 1.8, 10.0),
                "metrics": {"in_degree": count}
            })
    
    # 高度出度节点（依赖过多）
    for mod_name, count in out_degree.items():
        if count >= 4:
            hotspots.append({
                "type": "依赖过多",
                "target": mod_name,
                "risk": f"依赖{count}个模块，初始化/故障传播风险高",
                "level": "P1" if count >= 6 else "P2",
                "score": min(count * 1.2, 8.0),
                "metrics": {"out_degree": count}
            })
    
    # 2. 复杂状态机 - 按模块分析
    module_states: Dict[str, Set[str]] = {}
    for t in transitions:
        mod = t.module or "全局"
        if mod not in module_states:
            module_states[mod] = set()
        module_states[mod].add(t.from_state)
        module_states[mod].add(t.to_state)
    
    # 全局状态复杂度
    all_states = set()
    for states in module_states.values():
        all_states.update(states)
    state_count = len(all_states)
    
    if state_count >= 6:
        transition_count = len(transitions)
        # 状态复杂度 = 状态数 + 转换数/2
        complexity = state_count + transition_count / 2
        risk_level = "P0" if complexity >= 15 else ("P1" if complexity >= 10 else "P2")
        hotspots.append({
            "type": "复杂状态机",
            "target": "全局状态管理",
            "risk": f"涉及{state_count}个状态、{transition_count}条转换，状态流转复杂，需状态转换测试",
            "level": risk_level,
            "score": min(complexity * 0.7, 10.0),
            "metrics": {"states": state_count, "transitions": transition_count}
        })
    
    # 3. 循环依赖检测 - 支持多节点循环
    dep_graph: Dict[str, Set[str]] = {}
    for m in modules:
        dep_graph[m.name] = set(m.dependencies)
    
    # 检测两两循环
    detected_cycles = set()
    for m in modules:
        for dep in m.dependencies:
            dep_module = next((x for x in modules if x.name == dep), None)
            if dep_module and m.name in dep_module.dependencies:
                cycle_key = tuple(sorted([m.name, dep]))
                if cycle_key not in detected_cycles:
                    detected_cycles.add(cycle_key)
                    hotspots.append({
                        "type": "循环依赖",
                        "target": f"{m.name} ↔ {dep}",
                        "risk": "模块间循环依赖，可能导致初始化死锁、资源释放问题",
                        "level": "P0",
                        "score": 9.0,
                        "metrics": {"cycle_length": 2}
                    })
    
    # 4. 复杂度高的模块 - 分级更细
    complexity_levels = [
        (8.5, "P0", "极高复杂度，强烈建议拆分或重构"),
        (7.0, "P1", "高复杂度，建议重点测试并考虑优化"),
        (5.5, "P2", "中等复杂度，需关注核心逻辑"),
    ]
    
    detected_high_complexity = set()
    for threshold, level, desc in complexity_levels:
        for m in modules:
            if m.complexity_score >= threshold and m.name not in detected_high_complexity:
                detected_high_complexity.add(m.name)
                # 分析复杂度构成
                reasons = []
                if len(m.dependencies) >= 3:
                    reasons.append(f"依赖多({len(m.dependencies)})")
                if len(m.interfaces) >= 3:
                    reasons.append(f"接口多({len(m.interfaces)})")
                
                hotspots.append({
                    "type": "高复杂度模块",
                    "target": m.name,
                    "risk": f"复杂度{m.complexity_score}，{desc}",
                    "level": level,
                    "score": m.complexity_score,
                    "metrics": {
                        "complexity": m.complexity_score,
                        "dependencies": len(m.dependencies),
                        "interfaces": len(m.interfaces),
                        "reasons": reasons
                    }
                })
    
    # 5. 数据实体热点 - 被多个模块操作的实体
    if entities:
        entity_modules: Dict[str, Set[str]] = {}
        for e in entities:
            entity_modules[e.name] = set()
            for m in modules:
                if e.module == m.name or e.name in m.name or m.name in e.name:
                    entity_modules[e.name].add(m.name)
        
        for entity_name, mod_set in entity_modules.items():
            if len(mod_set) >= 3:
                hotspots.append({
                    "type": "共享数据实体",
                    "target": entity_name,
                    "risk": f"被{len(mod_set)}个模块操作，数据一致性风险",
                    "level": "P1" if len(mod_set) >= 5 else "P2",
                    "score": min(len(mod_set) * 1.5, 8.0),
                    "metrics": {"shared_by": len(mod_set)}
                })
    
    # 6. 业务规则热点 - 规则密集的模块
    if stage1_output:
        rules = stage1_output.get("business_rules", [])
        for m in modules:
            module_rules = [r for r in rules if m.name in str(r)]
            if len(module_rules) >= 5:
                hotspots.append({
                    "type": "业务规则密集",
                    "target": m.name,
                    "risk": f"包含{len(module_rules)}条业务规则，逻辑复杂易出错",
                    "level": "P1" if len(module_rules) >= 8 else "P2",
                    "score": min(len(module_rules) * 0.8, 7.0),
                    "metrics": {"rule_count": len(module_rules)}
                })
    
    # 按分数排序，去重（同一目标只保留最高风险）
    hotspots.sort(key=lambda x: x["score"], reverse=True)
    seen_targets = set()
    unique_hotspots = []
    for h in hotspots:
        if h["target"] not in seen_targets:
            seen_targets.add(h["target"])
            unique_hotspots.append(h)
    
    return unique_hotspots[:12]


def _generate_test_strategy(
    view: ArchitectureView,
    stage1_output: Dict[str, Any]
) -> Dict[str, Any]:
    """生成测试策略建议"""
    strategy = {
        "priority_modules": [],  # 优先测试的模块
        "automation_candidates": [],  # 适合自动化的模块
        "manual_focus": [],  # 需要人工重点测试的
        "risk_areas": [],  # 高风险区域
    }
    
    # 优先测试：入口模块 + 高风险模块
    entry_modules = view.entry_points if view.entry_points else []
    high_risk = [h["target"] for h in view.risk_hotspots if h["level"] == "P0"]
    
    for m in view.modules:
        if m.name in entry_modules or m.name in high_risk:
            strategy["priority_modules"].append({
                "module": m.name,
                "reason": "系统入口" if m.name in entry_modules else "高风险模块",
                "test_focus": ["核心流程", "异常场景", "边界条件"]
            })
    
    # 自动化候选：规则明确、状态清晰的模块
    for m in view.modules:
        if m.complexity_score <= 5.0 and len(m.dependencies) <= 2:
            strategy["automation_candidates"].append({
                "module": m.name,
                "reason": "复杂度低、依赖少，适合自动化",
                "coverage_target": "80%"
            })
    
    # 人工重点：复杂状态机、高风险模块
    for h in view.risk_hotspots:
        if h["level"] in ["P0", "P1"]:
            strategy["manual_focus"].append({
                "area": h["target"],
                "risk_type": h["type"],
                "test_approach": _get_test_approach(h["type"])
            })
    
    # 风险区域
    strategy["risk_areas"] = view.risk_hotspots[:5]
    
    return strategy


def _get_test_approach(risk_type: str) -> str:
    """根据风险类型给出测试方法"""
    approaches = {
        "中心节点": "集成测试 + 契约测试 + 回归测试",
        "复杂状态机": "状态转换测试 + 边界值测试 + 异常注入",
        "循环依赖": "启动顺序测试 + 资源释放测试 + 压力测试",
        "高复杂度": "探索式测试 + 代码审查 + 性能测试",
    }
    return approaches.get(risk_type, "功能测试 + 回归测试")


def run_architecture_scan(
    stage1_output: Dict[str, Any],
    stage2_output: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    主入口：运行架构透视扫描
    
    返回：
    {
        "architecture_view": ArchitectureView 对象,
        "module_hierarchy": 模块层级结构,
        "interface_matrix": 接口依赖矩阵,
        "state_diagram": 状态机描述,
        "entity_relations": 实体关系图,
        "risk_heatmap": 风险热力图数据,
        "test_strategy": 测试策略建议
    }
    """
    # 1. 提取模块
    modules = _extract_modules_from_stage1(stage1_output)
    
    # 2. 提取API接口定义
    api_interfaces = _extract_api_interfaces(stage1_output, modules)
    
    # 3. 提取接口依赖关系（补充）
    _extract_interface_dependencies(stage1_output, modules)
    
    # 4. 构建状态机
    transitions = _build_state_machine(stage1_output, modules)
    
    # 5. 提取数据实体
    entities = _extract_data_entities(stage1_output)
    
    # 6. 计算复杂度（传入stage1_output以获取业务规则）
    for m in modules:
        m.complexity_score = _calculate_complexity(m, modules, transitions, entities, stage1_output)
    
    # 7. 识别风险热点（传入stage1_output）
    risk_hotspots = _identify_risk_hotspots(modules, transitions, entities, stage1_output)
    
    # 8. 构建架构视图
    view = ArchitectureView(
        modules=modules,
        state_machine=transitions,
        entities=entities,
        entry_points=[m.name for m in modules if m.level == 1][:3],
        risk_hotspots=risk_hotspots
    )
    
    # 9. 生成测试策略
    test_strategy = _generate_test_strategy(view, stage1_output)
    
    # 10. 构建层级结构
    module_hierarchy = _build_module_hierarchy(modules)
    
    # 11. 构建接口矩阵
    interface_matrix = _build_interface_matrix(modules)
    
    # 12. 构建API接口清单
    api_list = _build_api_list(api_interfaces)
    
    # 13. 构建状态图描述
    state_diagram = _build_state_diagram_desc(transitions)
    
    # 14. 构建实体关系
    entity_relations = _build_entity_relations(entities)
    
    # 计算状态数（从转换中提取 + 从stage1直接提取的状态）
    all_states = set(t.from_state for t in transitions) | set(t.to_state for t in transitions)
    raw_states = _extract_states_from_stage1(stage1_output)
    all_states.update(raw_states)
    
    return {
        "architecture_view": {
            "module_count": len(modules),
            "state_count": len(all_states),
            "transition_count": len(transitions),
            "entity_count": len(entities),
            "entry_points": view.entry_points,
        },
        "modules": [
            {
                "name": m.name,
                "level": m.level,
                "parent": m.parent,
                "description": m.description,
                "complexity": m.complexity_score,
                "risk": m.risk_level,
                "interfaces": m.interfaces,
                "dependencies": m.dependencies,
            }
            for m in modules
        ],
        "module_hierarchy": module_hierarchy,
        "state_machine": [
            {
                "from": t.from_state,
                "to": t.to_state,
                "event": t.event,
                "action": t.action,
                "module": t.module,
            }
            for t in transitions
        ],
        "state_diagram": state_diagram,
        "entities": [
            {
                "name": e.name,
                "fields": e.fields,
                "module": e.module,
            }
            for e in entities
        ],
        "entity_relations": entity_relations,
        "interface_matrix": interface_matrix,
        "api_interfaces": api_list,
        "risk_hotspots": risk_hotspots,
        "risk_heatmap": _build_risk_heatmap(modules, risk_hotspots),
        "test_strategy": test_strategy,
    }


def _build_module_hierarchy(modules: List[ModuleNode]) -> Dict[str, Any]:
    """构建模块层级结构"""
    # 按层级分组
    by_level: Dict[int, List[str]] = {}
    for m in modules:
        by_level.setdefault(m.level, []).append(m.name)
    
    # 构建树形结构（简化版）
    tree: Dict[str, Any] = {"name": "系统", "children": []}
    
    # L1 作为系统直接子节点
    l1_modules = by_level.get(1, [])
    if not l1_modules and by_level.get(2):
        l1_modules = by_level.get(2, [])[:3]  # 如果没有L1，取前3个L2
    
    for l1 in l1_modules:
        node = {"name": l1, "children": []}
        # 找L2子节点
        for m in modules:
            if m.level == 2 and (m.parent == l1 or not m.parent):
                node["children"].append({"name": m.name})
        tree["children"].append(node)
    
    return tree


def _build_interface_matrix(modules: List[ModuleNode]) -> List[Dict[str, Any]]:
    """构建模块间接口依赖矩阵"""
    matrix = []
    for m in modules:
        if m.dependencies:
            matrix.append({
                "source": m.name,
                "targets": m.dependencies,
                "interface_count": len(m.interfaces),
            })
    return matrix


def _build_api_list(interfaces: List[ApiInterface]) -> List[Dict[str, Any]]:
    """构建API接口清单"""
    return [
        {
            "name": api.name,
            "method": api.method,
            "path": api.path,
            "description": api.description,
            "module": api.module,
            "caller": api.caller,
            "callee": api.callee,
            "params": api.params,
            "response": api.response,
        }
        for api in interfaces
    ]


def _build_state_diagram_desc(transitions: List[StateTransition]) -> str:
    """构建状态图文本描述（可用于 Mermaid 等工具渲染）"""
    if not transitions:
        return "未识别到状态转换"
    
    lines = ["stateDiagram-v2"]
    
    # 收集所有状态
    all_states = set()
    for t in transitions:
        all_states.add(t.from_state)
        all_states.add(t.to_state)
    
    # 定义状态
    for state in sorted(all_states):
        lines.append(f"    {state}")
    
    lines.append("")
    
    # 定义转换
    for t in transitions:
        event_label = f" : {t.event}" if t.event else ""
        lines.append(f"    {t.from_state} --> {t.to_state}{event_label}")
    
    return "\n".join(lines)


def _build_entity_relations(entities: List[DataEntity]) -> List[Dict[str, Any]]:
    """构建实体关系"""
    relations = []
    for e in entities:
        if e.relations:
            for rel_entity, rel_type in e.relations:
                relations.append({
                    "from": e.name,
                    "to": rel_entity,
                    "type": rel_type,
                })
    return relations


def _build_risk_heatmap(
    modules: List[ModuleNode],
    hotspots: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """构建风险热力图数据 - 增强版：多维度风险矩阵"""
    # 为每个模块计算综合风险分数
    module_risks: Dict[str, Dict[str, Any]] = {}
    
    for m in modules:
        # 基础风险 = 复杂度
        base_risk = m.complexity_score
        
        # 查找该模块的热点
        module_hotspots = [h for h in hotspots if h["target"] == m.name or m.name in h["target"]]
        hotspot_score = max([h["score"] for h in module_hotspots], default=0)
        
        # 综合风险分（取最高值）
        total_risk = max(base_risk, hotspot_score)
        
        # 风险维度分析
        dimensions = {
            "complexity": min(m.complexity_score, 10),
            "dependency": min(len(m.dependencies) * 1.5, 10),
            "interface": min(len(m.interfaces) * 1.2, 10),
        }
        
        module_risks[m.name] = {
            "total": round(total_risk, 1),
            "base": round(base_risk, 1),
            "hotspot": round(hotspot_score, 1),
            "dimensions": dimensions,
            "hotspot_types": [h["type"] for h in module_hotspots],
            "level": "P0" if total_risk >= 8.5 else ("P1" if total_risk >= 6.5 else ("P2" if total_risk >= 4 else "P3"))
        }
    
    # 分类统计
    high_risk = [m for m, r in module_risks.items() if r["total"] >= 6.5]
    medium_risk = [m for m, r in module_risks.items() if 4.0 <= r["total"] < 6.5]
    low_risk = [m for m, r in module_risks.items() if r["total"] < 4.0]
    
    # 统计信息
    stats = {
        "total_modules": len(modules),
        "high_risk_count": len(high_risk),
        "medium_risk_count": len(medium_risk),
        "low_risk_count": len(low_risk),
        "avg_complexity": round(sum(m.complexity_score for m in modules) / len(modules), 1) if modules else 0,
        "max_complexity": max((m.complexity_score for m in modules), default=0),
        "hotspot_count": len(hotspots)
    }
    
    return {
        "high": high_risk,
        "medium": medium_risk,
        "low": low_risk,
        "modules": module_risks,
        "stats": stats,
        "hotspots": hotspots
    }


def generate_html_report(scan_result: Dict[str, Any], prd_title: str = "PRD架构分析报告") -> str:
    """生成HTML可视化报告"""
    import datetime
    
    # 提取数据
    arch_view = scan_result.get("architecture_view", {})
    modules = scan_result.get("modules", [])
    state_machine = scan_result.get("state_machine", [])
    entities = scan_result.get("entities", [])
    api_interfaces = scan_result.get("api_interfaces", [])
    risk_heatmap = scan_result.get("risk_heatmap", {})
    risk_hotspots = scan_result.get("risk_hotspots", [])
    test_strategy = scan_result.get("test_strategy", {})
    state_diagram = scan_result.get("state_diagram", "")
    
    # 风险颜色映射
    def risk_color(level: str) -> str:
        colors = {"P0": "#ff4d4f", "P1": "#faad14", "P2": "#52c41a", "P3": "#1890ff"}
        return colors.get(level, "#999")
    
    def risk_bg(level: str) -> str:
        colors = {"P0": "#fff1f0", "P1": "#fff7e6", "P2": "#f6ffed", "P3": "#e6f7ff"}
        return colors.get(level, "#f5f5f5")
    
    # 构建模块表格行
    module_rows = ""
    for m in modules:
        risk_c = risk_color(m.get("risk", "P2"))
        risk_b = risk_bg(m.get("risk", "P2"))
        deps = ", ".join(m.get("dependencies", [])[:3]) or "-"
        module_rows += f'<tr><td><strong>{m.get("name", "")}</strong></td><td>{"系统" if m.get("level") == 1 else ("子系统" if m.get("level") == 2 else "功能")}</td><td><span class="badge" style="background:{risk_b};color:{risk_c};border:1px solid {risk_c}">{m.get("risk", "P2")}</span></td><td><div class="score-bar"><div class="score-fill" style="width:{m.get("complexity", 0)*10}%;background:{risk_c}"></div></div> {m.get("complexity", 0)}</td><td>{deps}</td><td class="desc">{m.get("description", "")}</td></tr>'
    
    # 构建风险热点表格
    hotspot_rows = ""
    for h in risk_hotspots[:8]:
        risk_c = risk_color(h.get("level", "P2"))
        risk_b = risk_bg(h.get("level", "P2"))
        hotspot_rows += f'<tr><td><span class="badge" style="background:{risk_b};color:{risk_c};border:1px solid {risk_c}">{h.get("level", "P2")}</span></td><td><strong>{h.get("type", "")}</strong></td><td>{h.get("target", "")}</td><td>{h.get("risk", "")}</td><td><div class="score-bar"><div class="score-fill" style="width:{h.get("score", 0)*10}%;background:{risk_c}"></div></div> {h.get("score", 0)}</td></tr>'
    
    # 构建状态机Mermaid图
    mermaid_diagram = state_diagram.replace(chr(10), "\\n") if state_diagram else "stateDiagram-v2\\n    暂无状态数据"
    
    # 构建API接口列表
    api_rows = ""
    for api in api_interfaces[:10]:
        method_colors = {"GET": "#52c41a", "POST": "#1890ff", "PUT": "#faad14", "DELETE": "#ff4d4f"}
        method_c = method_colors.get(api.get("method", "GET"), "#999")
        api_rows += f'<tr><td><span class="method-badge" style="background:{method_c}">{api.get("method", "GET")}</span></td><td><strong>{api.get("name", "")}</strong></td><td>{api.get("path", "-")}</td><td>{api.get("module", "-")}</td><td class="desc">{api.get("description", "")}</td></tr>'
    
    # 统计数据
    stats = risk_heatmap.get("stats", {})
    high_risk_count = stats.get("high_risk_count", 0)
    
    # 生成HTML
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{prd_title}</title>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <style>
        :root {{ --primary: #1890ff; --success: #52c41a; --warning: #faad14; --danger: #ff4d4f; --bg: #f0f2f5; --card-bg: #fff; --text: #333; --text-secondary: #666; --border: #e8e8e8; }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); }}
        .header h1 {{ font-size: 32px; margin-bottom: 12px; }}
        .header p {{ opacity: 0.9; font-size: 16px; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .stat-card {{ background: var(--card-bg); padding: 24px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; transition: transform 0.2s; }}
        .stat-card:hover {{ transform: translateY(-2px); }}
        .stat-value {{ font-size: 36px; font-weight: bold; color: var(--primary); margin-bottom: 8px; }}
        .stat-label {{ color: var(--text-secondary); font-size: 14px; }}
        .section {{ background: var(--card-bg); border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .section-title {{ font-size: 20px; font-weight: 600; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 2px solid var(--border); display: flex; align-items: center; gap: 8px; }}
        .data-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        .data-table th {{ background: #fafafa; padding: 12px 16px; text-align: left; font-weight: 600; color: var(--text-secondary); border-bottom: 1px solid var(--border); }}
        .data-table td {{ padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }}
        .data-table tr:hover {{ background: #fafafa; }}
        .data-table .desc {{ max-width: 300px; color: var(--text-secondary); font-size: 13px; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }}
        .method-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; color: white; }}
        .score-bar {{ display: inline-block; width: 60px; height: 8px; background: #e8e8e8; border-radius: 4px; overflow: hidden; margin-right: 8px; vertical-align: middle; }}
        .score-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
        .risk-legend {{ display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }}
        .risk-item {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
        .risk-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
        .mermaid-container {{ background: #fafafa; border-radius: 8px; padding: 20px; overflow-x: auto; }}
        .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
        @media (max-width: 900px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
        .strategy-list {{ list-style: none; }}
        .strategy-list li {{ padding: 12px 16px; background: #fafafa; border-radius: 8px; margin-bottom: 8px; border-left: 4px solid var(--primary); }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏗️ {prd_title}</h1>
            <p>基于PRD文档自动生成的架构透视分析报告 | 生成时间：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{arch_view.get("module_count", 0)}</div><div class="stat-label">功能模块</div></div>
            <div class="stat-card"><div class="stat-value">{arch_view.get("state_count", 0)}</div><div class="stat-label">状态节点</div></div>
            <div class="stat-card"><div class="stat-value">{arch_view.get("transition_count", 0)}</div><div class="stat-label">状态转换</div></div>
            <div class="stat-card"><div class="stat-value">{arch_view.get("entity_count", 0)}</div><div class="stat-label">数据实体</div></div>
            <div class="stat-card"><div class="stat-value">{len(api_interfaces)}</div><div class="stat-label">API接口</div></div>
            <div class="stat-card"><div class="stat-value" style="color:{risk_color("P0") if high_risk_count > 0 else risk_color("P2")}">{high_risk_count}</div><div class="stat-label">高风险模块</div></div>
        </div>
        
        <div class="section">
            <h2 class="section-title">🔥 风险热力图</h2>
            <div class="risk-legend">
                <div class="risk-item"><div class="risk-dot" style="background:{risk_color("P0")}"></div> P0 极高风险 (≥8.5)</div>
                <div class="risk-item"><div class="risk-dot" style="background:{risk_color("P1")}"></div> P1 高风险 (6.5-8.5)</div>
                <div class="risk-item"><div class="risk-dot" style="background:{risk_color("P2")}"></div> P2 中风险 (4.0-6.5)</div>
                <div class="risk-item"><div class="risk-dot" style="background:{risk_color("P3")}"></div> P3 低风险 (&lt;4.0)</div>
            </div>
            <table class="data-table">
                <thead><tr><th width="80">等级</th><th width="120">风险类型</th><th width="150">目标</th><th>风险描述</th><th width="120">风险分</th></tr></thead>
                <tbody>{hotspot_rows if hotspot_rows else '<tr><td colspan="5" style="text-align:center;color:#999">暂无风险热点</td></tr>'}</tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">📦 功能模块清单</h2>
            <table class="data-table">
                <thead><tr><th>模块名称</th><th width="80">层级</th><th width="80">风险</th><th width="120">复杂度</th><th width="150">依赖模块</th><th>描述</th></tr></thead>
                <tbody>{module_rows if module_rows else '<tr><td colspan="6" style="text-align:center;color:#999">暂无模块数据</td></tr>'}</tbody>
            </table>
        </div>
        
        <div class="two-col">
            <div class="section">
                <h2 class="section-title">🔄 核心状态机</h2>
                <div class="mermaid-container">
                    <pre class="mermaid">{mermaid_diagram}</pre>
                </div>
            </div>
            <div class="section">
                <h2 class="section-title">🔌 API接口清单</h2>
                <table class="data-table">
                    <thead><tr><th width="60">方法</th><th>接口名称</th><th>路径</th><th>所属模块</th><th>描述</th></tr></thead>
                    <tbody>{api_rows if api_rows else '<tr><td colspan="5" style="text-align:center;color:#999">暂无API接口数据</td></tr>'}</tbody>
                </table>
            </div>
        </div>
        
        <div class="section">
            <h2 class="section-title">🗄️ 数据实体</h2>
            <div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(250px, 1fr));gap:16px">
                {''.join(f'<div style="background:#fafafa;padding:16px;border-radius:8px"><h4 style="margin-bottom:8px">{e.get("name", "")}</h4><p style="font-size:13px;color:#666">字段: {", ".join(e.get("fields", [])[:5]) or "-"}</p></div>' for e in entities) or '<p style="color:#999">暂无实体数据</p>'}
            </div>
        </div>
    </div>
    <script>mermaid.initialize({{startOnLoad: true, theme: 'default'}});</script>
</body>
</html>'''
    
    return html


# 便捷函数
def scan_prd_architecture(
    prd_text: str,
    stage1_output: Dict[str, Any],
    stage2_output: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    对外接口：扫描 PRD 架构
    """
    return run_architecture_scan(stage1_output, stage2_output)


def generate_architecture_report(
    stage1_output: Dict[str, Any],
    output_path: str,
    prd_title: str = "PRD架构分析报告"
) -> str:
    """
    生成完整的架构分析报告（HTML格式）
    
    Args:
        stage1_output: Stage1分析结果
        output_path: 输出文件路径
        prd_title: 报告标题
        
    Returns:
        生成的HTML文件路径
    """
    # 运行架构扫描
    scan_result = run_architecture_scan(stage1_output)
    
    # 生成HTML报告
    html_content = generate_html_report(scan_result, prd_title)
    
    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    return output_path


# ============================================
# 变更影响分析模块
# ============================================

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
    change_text = _norm(change_description)
    
    # 提取已有数据
    modules = scan_result.get("modules", [])
    state_machine = scan_result.get("state_machine", [])
    api_interfaces = scan_result.get("api_interfaces", [])
    entities = scan_result.get("entities", [])
    risk_hotspots = scan_result.get("risk_hotspots", [])
    
    # 1. 分析受影响的模块
    affected_modules = _analyze_module_impact(change_text, modules)
    
    # 2. 分析受影响的状态
    affected_states = _analyze_state_impact(change_text, state_machine, modules)
    
    # 3. 分析受影响的API
    affected_apis = _analyze_api_impact(change_text, api_interfaces, modules)
    
    # 4. 分析受影响的实体
    affected_entities = _analyze_entity_impact(change_text, entities, modules)
    
    # 5. 风险评估
    risk_assessment = _generate_change_risk_assessment(
        change_text,
        affected_modules,
        affected_states,
        affected_apis,
        affected_entities
    )
    
    # 6. 测试建议
    test_recommendations = _generate_change_test_recommendations(
        affected_modules,
        affected_states,
        affected_apis,
        affected_entities,
        risk_assessment
    )
    
    return {
        "change_description": change_description,
        "summary": _generate_change_summary(affected_modules, affected_states, affected_apis, affected_entities),
        "affected_modules": affected_modules,
        "affected_states": affected_states,
        "affected_apis": affected_apis,
        "affected_entities": affected_entities,
        "risk_assessment": risk_assessment,
        "test_recommendations": test_recommendations,
    }


def _analyze_module_impact(
    change_text: str,
    modules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """分析受影响的模块"""
    affected = []
    
    # 关键词匹配模式
    action_patterns = [
        # 新增
        (r"(?:新增|增加|添加|接入|引入|支持)([\u4e00-\u9fa5]{2,10})(?:功能|模块|服务|系统)", "新增"),
        (r"([\u4e00-\u9fa5]{2,10})(?:功能|模块|服务)新增", "新增"),
        # 修改
        (r"(?:修改|调整|优化|变更|改动)([\u4e00-\u9fa5]{2,10})(?:功能|模块|服务)", "修改"),
        (r"([\u4e00-\u9fa5]{2,10})(?:功能|模块|服务)(?:调整|优化|变更)", "修改"),
        # 删除
        (r"(?:删除|移除|下线|取消)([\u4e00-\u9fa5]{2,10})(?:功能|模块|服务)", "删除"),
        # 调用
        (r"(?:调用|依赖|使用)([\u4e00-\u9fa5]{2,10})(?:模块|服务|系统)", "关联"),
    ]
    
    for module in modules:
        module_name = module.get("name", "")
        match_type = None
        match_reason = ""
        
        # 精确匹配
        if module_name in change_text:
            match_type = "直接修改"
            match_reason = f"变更描述直接提及模块「{module_name}」"
        else:
            # 模式匹配
            for pattern, action in action_patterns:
                matches = re.findall(pattern, change_text)
                for match in matches:
                    if match in module_name or module_name in match:
                        match_type = action
                        match_reason = f"关键词「{match}」匹配到模块「{module_name}」"
                        break
                if match_type:
                    break
        
        if match_type:
            # 获取模块详情
            complexity = module.get("complexity", 0)
            risk = module.get("risk", "P2")
            dependencies = module.get("dependencies", [])
            
            # 评估影响范围
            impact_scope = "高"
            if match_type == "新增":
                impact_scope = "中"  # 新增通常影响较小
            elif match_type == "删除":
                impact_scope = "高"  # 删除影响较大
            elif complexity >= 7.0 or risk == "P0":
                impact_scope = "高"
            elif complexity >= 4.0 or risk == "P1":
                impact_scope = "中"
            
            affected.append({
                "module": module_name,
                "action": match_type,
                "reason": match_reason,
                "complexity": complexity,
                "risk": risk,
                "dependencies": dependencies,
                "impact_scope": impact_scope,
                "regression_risk": "高" if (complexity >= 5.0 or dependencies) else "中"
            })
    
    # 按影响范围排序
    impact_order = {"高": 0, "中": 1, "低": 2}
    affected.sort(key=lambda x: (impact_order.get(x["impact_scope"], 2), -x.get("complexity", 0)))
    
    return affected


def _analyze_state_impact(
    change_text: str,
    state_machine: List[Dict[str, Any]],
    modules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """分析受影响的状态"""
    affected = []
    
    # 从状态机中提取所有状态
    all_states = set()
    state_to_module = {}
    for t in state_machine:
        all_states.add(t.get("from", ""))
        all_states.add(t.get("to", ""))
        module = t.get("module", "")
        if module:
            state_to_module[t.get("from", "")] = module
            state_to_module[t.get("to", "")] = module
    
    # 状态相关关键词
    state_keywords = [
        "状态", "模式", "展示", "播放", "暂停", "停止", "开始", "结束",
        "进入", "退出", "切换", "跳转", "激活", "失效", "完成"
    ]
    
    for state in all_states:
        if not state:
            continue
            
        # 检查变更是否涉及此状态
        if state in change_text:
            # 找到相关的转换
            related_transitions = [
                t for t in state_machine
                if t.get("from") == state or t.get("to") == state
            ]
            
            affected.append({
                "state": state,
                "action": "直接修改",
                "reason": f"变更直接提及状态「{state}」",
                "transitions_count": len(related_transitions),
                "related_transitions": [
                    {
                        "from": t.get("from"),
                        "to": t.get("to"),
                        "event": t.get("event")
                    }
                    for t in related_transitions[:3]
                ],
                "module": state_to_module.get(state, ""),
                "impact_scope": "高" if len(related_transitions) >= 3 else "中"
            })
        else:
            # 检查关键词匹配
            for keyword in state_keywords:
                if keyword in state and keyword in change_text:
                    affected.append({
                        "state": state,
                        "action": "间接影响",
                        "reason": f"变更涉及「{keyword}」，可能影响状态「{state}」",
                        "transitions_count": 0,
                        "related_transitions": [],
                        "module": state_to_module.get(state, ""),
                        "impact_scope": "低"
                    })
                    break
    
    # 去重并按影响范围排序
    seen_states = set()
    unique_affected = []
    for a in affected:
        if a["state"] not in seen_states:
            seen_states.add(a["state"])
            unique_affected.append(a)
    
    impact_order = {"高": 0, "中": 1, "低": 2}
    unique_affected.sort(key=lambda x: impact_order.get(x["impact_scope"], 2))
    
    return unique_affected


def _analyze_api_impact(
    change_text: str,
    api_interfaces: List[Dict[str, Any]],
    modules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """分析受影响的API接口"""
    affected = []
    
    # API相关关键词
    api_keywords = ["接口", "API", "请求", "调用", "返回", "参数", "数据"]
    
    for api in api_interfaces:
        api_name = api.get("name", "")
        api_path = api.get("path", "")
        api_method = api.get("method", "GET")
        
        is_affected = False
        reason = ""
        action = "可能影响"
        
        # 直接匹配
        if api_name in change_text or (api_path and api_path in change_text):
            is_affected = True
            action = "直接修改"
            reason = f"变更直接提及接口「{api_name}」"
        else:
            # 关键词匹配
            matched_keywords = [k for k in api_keywords if k in change_text]
            if matched_keywords and (api_name in change_text or any(api_name for _ in matched_keywords)):
                is_affected = True
                action = "间接影响"
                reason = f"变更涉及{', '.join(matched_keywords)}，可能影响此接口"
        
        if is_affected:
            affected.append({
                "api": api_name,
                "method": api_method,
                "path": api_path,
                "action": action,
                "reason": reason,
                "module": api.get("module", ""),
                "caller": api.get("caller", ""),
                "callee": api.get("callee", ""),
                "impact_scope": "高" if action == "直接修改" else "中"
            })
    
    # 额外：检测是否可能新增接口
    create_patterns = [
        r"(?:新增|增加|添加)(?:一个|条)?[\u4e00-\u9fa5]{0,6}接口",
        r"需要提供[\u4e00-\u9fa5]{2,10}接口",
        r"对接[\u4e00-\u9fa5]{2,10}接口"
    ]
    
    for pattern in create_patterns:
        if re.search(pattern, change_text):
            affected.append({
                "api": "[待新增]",
                "method": "待定",
                "path": "待定",
                "action": "需要新增",
                "reason": "变更描述暗示需要新增接口",
                "module": "",
                "caller": "",
                "callee": "",
                "impact_scope": "中"
            })
            break
    
    return affected


def _analyze_entity_impact(
    change_text: str,
    entities: List[Dict[str, Any]],
    modules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """分析受影响的数据实体"""
    affected = []
    
    # 实体相关关键词
    entity_keywords = ["数据", "字段", "属性", "表", "存储", "持久化", "数据库"]
    
    for entity in entities:
        entity_name = entity.get("name", "")
        fields = entity.get("fields", [])
        
        is_affected = False
        reason = ""
        action = "可能影响"
        
        # 直接匹配
        if entity_name in change_text:
            is_affected = True
            action = "直接修改"
            reason = f"变更直接提及数据实体「{entity_name}」"
        else:
            # 字段匹配
            for field in fields:
                if field and field in change_text:
                    is_affected = True
                    action = "字段变更"
                    reason = f"变更涉及字段「{field}」"
                    break
            
            # 关键词匹配
            if not is_affected:
                for keyword in entity_keywords:
                    if keyword in change_text and keyword in entity_name:
                        is_affected = True
                        action = "间接影响"
                        reason = f"变更涉及「{keyword}」，可能影响实体「{entity_name}」"
                        break
        
        if is_affected:
            affected.append({
                "entity": entity_name,
                "action": action,
                "reason": reason,
                "fields": fields,
                "module": entity.get("module", ""),
                "impact_scope": "高" if action == "直接修改" else "中"
            })
    
    return affected


def _generate_change_risk_assessment(
    change_text: str,
    affected_modules: List[Dict[str, Any]],
    affected_states: List[Dict[str, Any]],
    affected_apis: List[Dict[str, Any]],
    affected_entities: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """生成变更风险评估"""
    
    # 计算影响分数
    module_impact = len([m for m in affected_modules if m.get("impact_scope") == "高"]) * 3 + \
                   len([m for m in affected_modules if m.get("impact_scope") == "中"]) * 2 + \
                   len([m for m in affected_modules if m.get("impact_scope") == "低"]) * 1
    
    state_impact = len([s for s in affected_states if s.get("impact_scope") == "高"]) * 3 + \
                   len([s for s in affected_states if s.get("impact_scope") == "中"]) * 2 + \
                   len([s for s in affected_states if s.get("impact_scope") == "低"]) * 1
    
    api_impact = len([a for a in affected_apis if a.get("impact_scope") == "高"]) * 3 + \
                len([a for a in affected_apis if a.get("impact_scope") == "中"]) * 2 + \
                len([a for a in affected_apis if a.get("impact_scope") == "低"]) * 1
    
    entity_impact = len([e for e in affected_entities if e.get("impact_scope") == "高"]) * 3 + \
                   len([e for e in affected_entities if e.get("impact_scope") == "中"]) * 2 + \
                   len([e for e in affected_entities if e.get("impact_scope") == "低"]) * 1
    
    total_score = module_impact + state_impact + api_impact + entity_impact
    
    # 确定风险等级
    if total_score >= 15:
        risk_level = "P0"
        risk_desc = "极高风险"
        recommendation = "建议进行完整回归测试，需架构评审"
    elif total_score >= 10:
        risk_level = "P1"
        risk_desc = "高风险"
        recommendation = "建议进行核心流程回归测试，重点模块详细测试"
    elif total_score >= 5:
        risk_level = "P2"
        risk_desc = "中等风险"
        recommendation = "建议进行变更相关模块测试"
    else:
        risk_level = "P3"
        risk_desc = "低风险"
        recommendation = "建议进行冒烟测试确认"
    
    # 高风险项详情
    high_risk_items = []
    high_risk_items.extend([f"模块「{m['module']}」(风险:{m['impact_scope']})" for m in affected_modules if m.get("impact_scope") == "高"])
    high_risk_items.extend([f"状态「{s['state']}」(影响:{s['impact_scope']})" for s in affected_states if s.get("impact_scope") == "高"])
    high_risk_items.extend([f"接口「{a['api']}」(影响:{a['impact_scope']})" for a in affected_apis if a.get("impact_scope") == "高"])
    high_risk_items.extend([f"实体「{e['entity']}」(影响:{e['impact_scope']})" for e in affected_entities if e.get("impact_scope") == "高"])
    
    return {
        "risk_level": risk_level,
        "risk_desc": risk_desc,
        "total_score": total_score,
        "breakdown": {
            "module_impact": module_impact,
            "state_impact": state_impact,
            "api_impact": api_impact,
            "entity_impact": entity_impact
        },
        "high_risk_items": high_risk_items[:10],
        "recommendation": recommendation
    }


def _generate_change_test_recommendations(
    affected_modules: List[Dict[str, Any]],
    affected_states: List[Dict[str, Any]],
    affected_apis: List[Dict[str, Any]],
    affected_entities: List[Dict[str, Any]],
    risk_assessment: Dict[str, Any]
) -> Dict[str, Any]:
    """生成变更测试建议"""
    
    recommendations = {
        "priority_test_areas": [],
        "regression_modules": [],
        "api_test_focus": [],
        "data_test_focus": [],
        "automation_suggestions": [],
        "manual_test_focus": []
    }
    
    # 1. 优先测试区域
    for m in affected_modules[:5]:
        if m.get("impact_scope") in ["高", "中"]:
            recommendations["priority_test_areas"].append({
                "module": m["module"],
                "reason": m["reason"],
                "focus": ["功能验证", "异常场景"] if m.get("regression_risk") == "高" else ["功能验证"]
            })
    
    # 2. 回归测试模块
    high_regression = [m["module"] for m in affected_modules if m.get("regression_risk") == "高"]
    recommendations["regression_modules"] = high_regression
    
    # 3. API测试重点
    for api in affected_apis:
        if api.get("action") == "直接修改":
            recommendations["api_test_focus"].append({
                "api": api["api"],
                "method": api["method"],
                "focus": ["参数校验", "返回值验证", "异常处理"]
            })
    
    # 4. 数据测试重点
    for entity in affected_entities:
        recommendations["data_test_focus"].append({
            "entity": entity["entity"],
            "action": entity["action"],
            "focus": ["数据完整性", "字段校验", "持久化验证"] if entity.get("fields") else ["数据流转"]
        })
    
    # 5. 自动化建议
    if len(affected_modules) >= 3:
        recommendations["automation_suggestions"].append({
            "type": "接口自动化",
            "reason": "涉及多个模块，建议建立接口自动化用例"
        })
    for api in affected_apis:
        if api.get("action") == "直接修改":
            recommendations["automation_suggestions"].append({
                "type": f"用例-{api['api']}",
                "reason": "核心接口变更，建议添加自动化监控"
            })
    
    # 6. 人工测试重点
    if affected_states:
        recommendations["manual_test_focus"].append({
            "area": "状态转换测试",
            "reason": f"涉及{len(affected_states)}个状态的变更，需重点验证状态流转"
        })
    for m in affected_modules:
        if m.get("complexity", 0) >= 6.0:
            recommendations["manual_test_focus"].append({
                "area": f"模块-{m['module']}",
                "reason": f"高复杂度模块({m.get('complexity')})，需探索式测试"
            })
    
    return recommendations


def _generate_change_summary(
    affected_modules: List[Dict[str, Any]],
    affected_states: List[Dict[str, Any]],
    affected_apis: List[Dict[str, Any]],
    affected_entities: List[Dict[str, Any]]
) -> str:
    """生成变更影响摘要"""
    
    high_impact_modules = len([m for m in affected_modules if m.get("impact_scope") == "高"])
    medium_impact_modules = len([m for m in affected_modules if m.get("impact_scope") == "中"])
    
    parts = []
    
    if affected_modules:
        parts.append(f"涉及{len(affected_modules)}个模块（{high_impact_modules}个高影响）")
    if affected_states:
        parts.append(f"{len(affected_states)}个状态")
    if affected_apis:
        parts.append(f"{len(affected_apis)}个接口")
    if affected_entities:
        parts.append(f"{len(affected_entities)}个数据实体")
    
    if not parts:
        return "变更可能对现有系统影响较小，建议进行基础冒烟测试"
    
    return "变更" + "，".join(parts) + "，建议根据影响范围制定测试策略"


def generate_impact_report_html(
    impact_result: Dict[str, Any],
    prd_title: str = "PRD需求变更影响分析"
) -> str:
    """生成变更影响分析HTML报告"""
    import datetime
    
    summary = impact_result.get("summary", "")
    risk = impact_result.get("risk_assessment", {})
    modules = impact_result.get("affected_modules", [])
    states = impact_result.get("affected_states", [])
    apis = impact_result.get("affected_apis", [])
    entities = impact_result.get("affected_entities", [])
    test_recs = impact_result.get("test_recommendations", {})
    
    def risk_color(level: str) -> str:
        colors = {"P0": "#ff4d4f", "P1": "#faad14", "P2": "#52c41a", "P3": "#1890ff"}
        return colors.get(level, "#999")
    
    def impact_color(scope: str) -> str:
        colors = {"高": "#ff4d4f", "中": "#faad14", "低": "#52c41a"}
        return colors.get(scope, "#999")
    
    # 构建模块表格
    module_rows = ""
    for m in modules:
        impact_c = impact_color(m.get("impact_scope", "低"))
        module_rows += f'''<tr>
            <td><strong>{m.get("module", "")}</strong></td>
            <td><span class="action-badge">{m.get("action", "")}</span></td>
            <td><span class="impact-badge" style="background:{impact_c}20;color:{impact_c}">{m.get("impact_scope", "")}影响</span></td>
            <td>{m.get("reason", "")}</td>
            <td>{m.get("complexity", 0)}</td>
            <td>{m.get("risk", "-")}</td>
        </tr>'''
    
    # 构建状态表格
    state_rows = ""
    for s in states:
        impact_c = impact_color(s.get("impact_scope", "低"))
        transitions = ", ".join([f"{t['from']}→{t['to']}" for t in s.get("related_transitions", [])[:2]])
        state_rows += f'''<tr>
            <td><strong>{s.get("state", "")}</strong></td>
            <td><span class="action-badge">{s.get("action", "")}</span></td>
            <td><span class="impact-badge" style="background:{impact_c}20;color:{impact_c}">{s.get("impact_scope", "")}影响</span></td>
            <td>{s.get("reason", "")}</td>
            <td>{transitions or "-"}</td>
        </tr>'''
    
    # 构建API表格
    api_rows = ""
    for a in apis:
        method_colors = {"GET": "#52c41a", "POST": "#1890ff", "PUT": "#faad14", "DELETE": "#ff4d4f"}
        method_c = method_colors.get(a.get("method", "GET"), "#999")
        api_rows += f'''<tr>
            <td><span class="method-badge" style="background:{method_c}">{a.get("method", "-")}</span></td>
            <td><strong>{a.get("api", "")}</strong></td>
            <td>{a.get("path", "-")}</td>
            <td><span class="action-badge">{a.get("action", "")}</span></td>
            <td>{a.get("reason", "")}</td>
        </tr>'''
    
    # 构建实体表格
    entity_rows = ""
    for e in entities:
        impact_c = impact_color(e.get("impact_scope", "低"))
        fields = ", ".join(e.get("fields", [])[:3]) or "-"
        entity_rows += f'''<tr>
            <td><strong>{e.get("entity", "")}</strong></td>
            <td><span class="action-badge">{e.get("action", "")}</span></td>
            <td><span class="impact-badge" style="background:{impact_c}20;color:{impact_c}">{e.get("impact_scope", "")}影响</span></td>
            <td>{fields}</td>
            <td>{e.get("reason", "")}</td>
        </tr>'''
    
    # 构建测试建议
    test_sections = ""
    
    if test_recs.get("priority_test_areas"):
        test_sections += f'''<div class="recommend-section">
            <h3>优先测试区域</h3>
            <ul>{"".join(f"<li><strong>{p['module']}</strong>: {p['reason']}</li>" for p in test_recs['priority_test_areas'])}</ul>
        </div>'''
    
    if test_recs.get("regression_modules"):
        test_sections += f'''<div class="recommend-section">
            <h3>回归测试模块</h3>
            <div class="tag-list">{"".join(f"<span class='tag'>{m}</span>" for m in test_recs['regression_modules'])}</div>
        </div>'''
    
    if test_recs.get("manual_test_focus"):
        test_sections += f'''<div class="recommend-section">
            <h3>人工测试重点</h3>
            <ul>{"".join(f"<li><strong>{p['area']}</strong>: {p['reason']}</li>" for p in test_recs['manual_test_focus'])}</ul>
        </div>'''
    
    if test_recs.get("automation_suggestions"):
        test_sections += f'''<div class="recommend-section">
            <h3>自动化建议</h3>
            <ul>{"".join(f"<li>{s['type']}: {s['reason']}</li>" for s in test_recs['automation_suggestions'])}</ul>
        </div>'''
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{prd_title}</title>
    <style>
        :root {{ --primary: #1890ff; --success: #52c41a; --warning: #faad14; --danger: #ff4d4f; --bg: #f0f2f5; --card-bg: #fff; --text: #333; --text-secondary: #666; --border: #e8e8e8; }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 40px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); }}
        .header h1 {{ font-size: 32px; margin-bottom: 12px; }}
        .header p {{ opacity: 0.9; font-size: 16px; }}
        .risk-banner {{ background: {risk_color(risk.get('risk_level', 'P3'))}; color: white; padding: 20px; border-radius: 12px; margin-bottom: 24px; text-align: center; }}
        .risk-banner .level {{ font-size: 28px; font-weight: bold; }}
        .risk-banner .score {{ font-size: 16px; opacity: 0.9; }}
        .summary-card {{ background: var(--card-bg); padding: 20px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border-left: 4px solid var(--primary); }}
        .section {{ background: var(--card-bg); border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .section-title {{ font-size: 20px; font-weight: 600; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 2px solid var(--border); }}
        .data-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        .data-table th {{ background: #fafafa; padding: 12px 16px; text-align: left; font-weight: 600; color: var(--text-secondary); border-bottom: 1px solid var(--border); }}
        .data-table td {{ padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }}
        .data-table tr:hover {{ background: #fafafa; }}
        .action-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; background: #e6f7ff; color: #1890ff; }}
        .impact-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }}
        .method-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; color: white; }}
        .recommend-section {{ margin-bottom: 20px; }}
        .recommend-section h3 {{ font-size: 16px; margin-bottom: 12px; color: var(--primary); }}
        .recommend-section ul {{ list-style: none; }}
        .recommend-section li {{ padding: 8px 12px; background: #fafafa; border-radius: 6px; margin-bottom: 6px; }}
        .tag-list {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .tag {{ display: inline-block; padding: 4px 12px; background: #fff0f0; color: #ff4d4f; border-radius: 16px; font-size: 13px; }}
        .change-desc {{ background: #f0f5ff; padding: 20px; border-radius: 8px; margin-bottom: 24px; border-left: 4px solid #1890ff; }}
        .change-desc h3 {{ margin-bottom: 12px; color: #1890ff; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{prd_title}</h1>
            <p>变更影响分析报告 | 生成时间：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
        </div>
        
        <div class="change-desc">
            <h3>变更描述</h3>
            <p>{impact_result.get("change_description", "")}</p>
        </div>
        
        <div class="risk-banner">
            <div class="level">{risk.get("risk_level", "-")} - {risk.get("risk_desc", "")}</div>
            <div class="score">影响分数: {risk.get("total_score", 0)} | {risk.get("recommendation", "")}</div>
        </div>
        
        <div class="summary-card">
            <h3>影响摘要</h3>
            <p style="font-size: 16px; margin-top: 12px;">{summary}</p>
            {f'<p style="margin-top:12px;color:#666">高风险项: {", ".join(risk.get("high_risk_items", [])[:5])}</p>' if risk.get("high_risk_items") else ''}
        </div>
        
        <div class="section">
            <h2 class="section-title">📦 受影响的功能模块 ({len(modules)})</h2>
            <table class="data-table">
                <thead><tr><th>模块名称</th><th>变更类型</th><th>影响范围</th><th>影响原因</th><th>复杂度</th><th>风险等级</th></tr></thead>
                <tbody>{module_rows or '<tr><td colspan="6" style="text-align:center;color:#999">未识别到受影响的模块</td></tr>'}</tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🔄 受影响的状态 ({len(states)})</h2>
            <table class="data-table">
                <thead><tr><th>状态名称</th><th>变更类型</th><th>影响范围</th><th>影响原因</th><th>相关转换</th></tr></thead>
                <tbody>{state_rows or '<tr><td colspan="5" style="text-align:center;color:#999">未识别到受影响的状态</td></tr>'}</tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🔌 受影响的API接口 ({len(apis)})</h2>
            <table class="data-table">
                <thead><tr><th width="60">方法</th><th>接口名称</th><th>路径</th><th>变更类型</th><th>影响原因</th></tr></thead>
                <tbody>{api_rows or '<tr><td colspan="5" style="text-align:center;color:#999">未识别到受影响的接口</td></tr>'}</tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🗄️ 受影响的数据实体 ({len(entities)})</h2>
            <table class="data-table">
                <thead><tr><th>实体名称</th><th>变更类型</th><th>影响范围</th><th>相关字段</th><th>影响原因</th></tr></thead>
                <tbody>{entity_rows or '<tr><td colspan="5" style="text-align:center;color:#999">未识别到受影响的实体</td></tr>'}</tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🧪 测试建议</h2>
            {test_sections or '<p style="color:#999">暂无详细建议</p>'}
        </div>
    </div>
</body>
</html>'''
    
    return html
