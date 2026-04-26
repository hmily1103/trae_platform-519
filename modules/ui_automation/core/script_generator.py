"""
脚本生成器
将录制数据（JSON）转换为可执行的Python脚本（uiautomator2）
"""
from typing import List
from ..models import RecordingSession, UIAction
from utils.logger import setup_logger

logger = setup_logger('script_generator')


class ScriptGenerator:
    """脚本生成器"""
    
    def __init__(self):
        """初始化生成器"""
        pass
    
    def generate(self, session: RecordingSession, device_id: str = None) -> str:
        """
        生成Python脚本
        
        :param session: 录制会话
        :param device_id: 设备ID（如果为None，使用session中的）
        :return: Python脚本内容
        """
        target_device = device_id or session.device_id
        
        lines = [
            '"""',
            f'自动生成的UI自动化脚本',
            f'录制ID: {session.id}',
            f'设备ID: {session.device_id}',
            f'应用包名: {session.package_name}',
            f'创建时间: {session.created_at.isoformat()}',
            f'描述: {session.description}',
            '"""',
            '',
            '# Fix for "Invalid version: \'\'" error',
            'try:',
            '    import packaging.version',
            '    original_parse = packaging.version.parse',
            '    def safe_parse(version):',
            '        if not version or version == "":',
            '            return original_parse("0.0.0")',
            '        return original_parse(version)',
            '    packaging.version.parse = safe_parse',
            'except ImportError:',
            '    pass',
            '',
            'import uiautomator2 as u2',
            'import time',
            'import re',
            'import os',
            'import sys',
            '',
            '# 确保能导入项目模块',
            'sys.path.append(os.getcwd())',
            'try:',
            '    from modules.ui_automation.executor.trace_listener import TraceListener, StepContext',
            'except ImportError:',
            '    print("Warning: Failed to import TraceListener, tracing will be disabled")',
            '    TraceListener = None',
            '    StepContext = None',
            '',
            f'd = u2.connect("{target_device}")', 
            '',
            '# 初始化Trace监听器',
            'execution_id = sys.argv[1] if len(sys.argv) > 1 else ""',
            'listener = None',
            'if TraceListener and execution_id:',
            '    try:',
            f'        listener = TraceListener(execution_id, "{target_device}")',
            '        print(f"Trace listener initialized for execution: {execution_id}")',
            '    except Exception as e:',
            '        print(f"Failed to initialize trace listener: {e}")',
            '',
            '# 截图保存路径',
            'screenshot_dir = os.path.join(os.getcwd(), "trae_platform", "static", "ui_automation", "screenshots")',
            'if not os.path.exists(screenshot_dir):',
            '    os.makedirs(screenshot_dir)',
            '',
            '# 设置全局隐式等待 (10秒)',
            'd.implicitly_wait(10.0)',
            '',
            '# 开始执行',
            'try:',
        ]
        
        # 生成每个步骤的代码
        for action in session.actions:
            # 步骤上下文包装
            lines.append(f'    # Step {action.step_num} Wrapper')
            lines.append(f'    step_ctx_{action.step_num} = StepContext(listener, {action.step_num}, "{action.action_type}") if StepContext else None')
            lines.append(f'    if step_ctx_{action.step_num}:')
            lines.append(f'        step_ctx_{action.step_num}.__enter__()')
            lines.append(f'    try:')
            
            step_code = self._generate_step_code(action)
            # 增加缩进 (8个空格)
            indented_step_code = []
            for line in step_code:
                # 替换原来的4空格缩进为8空格
                if line.startswith('    '):
                    indented_step_code.append('    ' + line)
                else:
                    indented_step_code.append('        ' + line)
            
            lines.extend(indented_step_code)
            
            # 结束上下文
            lines.append(f'    except Exception as e:')
            lines.append(f'        if step_ctx_{action.step_num}:')
            lines.append(f'            step_ctx_{action.step_num}.__exit__(type(e), e, None)')
            lines.append(f'        raise')
            lines.append(f'    else:')
            lines.append(f'        if step_ctx_{action.step_num}:')
            lines.append(f'            step_ctx_{action.step_num}.__exit__(None, None, None)')
            
            lines.append('')
        
        lines.extend([
            '    print("脚本执行完成")',
            'except Exception as e:',
            '    print(f"脚本执行失败: {e}")',
            '    # 尝试截图',
            '    try:',
            '        timestamp = int(time.time())',
            f'        filename = f"error_{{timestamp}}_{target_device.replace(":", "_")}.jpg"',
            '        filepath = os.path.join(screenshot_dir, filename)',
            '        d.screenshot(filepath)',
            '        print(f"ERROR_SCREENSHOT: {{filename}}")',
            '    except Exception as s_e:',
            '        print(f"截图失败: {s_e}")',
            '    raise',
        ])
        
        return '\n'.join(lines)
    
    def _generate_step_code(self, action: UIAction) -> List[str]:
        """
        生成单个步骤的代码
        
        :param action: 操作记录
        :return: 代码行列表
        """
        lines = []
        indent = '    '  # 4个空格
        
        # 添加注释
        if action.description:
            lines.append(f'{indent}# Step {action.step_num}: {action.description}')
        else:
            lines.append(f'{indent}# Step {action.step_num}')
        
        # 根据操作类型生成代码
        if action.action_type == 'click':
            lines.extend(self._generate_click_code(action, indent))
        elif action.action_type == 'swipe':
            lines.extend(self._generate_swipe_code(action, indent))
        elif action.action_type == 'input':
            lines.extend(self._generate_input_code(action, indent))
        elif action.action_type == 'wait':
            lines.extend(self._generate_wait_code(action, indent))
        elif action.action_type == 'assertion':
            lines.extend(self._generate_assertion_code(action, indent))
        else:
            lines.append(f'{indent}# 未支持的操作类型: {action.action_type}')
        
        # 添加等待时间
        if action.wait_after > 0 and action.action_type != 'wait':
            wait_seconds = action.wait_after / 1000.0
            lines.append(f'{indent}time.sleep({wait_seconds})')
        
        return lines
    
    def _generate_click_code(self, action: UIAction, indent: str) -> List[str]:
        """生成点击代码"""
        lines = []
        ctx_var = f"step_ctx_{action.step_num}"
        
        if action.selector:
            selector_code = self._generate_selector_code(action.selector)
            if selector_code:
                if action.selector.fallbacks:
                    selector_codes = [selector_code] + [
                        self._generate_fallback_selector_code(fallback)
                        for fallback in action.selector.fallbacks
                    ]
                    # 获取策略和值，用于Trace
                    strategies = [(action.selector.strategy, -1)] + [
                        (fb.get('strategy', 'text'), i) for i, fb in enumerate(action.selector.fallbacks)
                    ]
                    
                    selector_codes = [c for c in selector_codes if c]
                    # 确保strategies长度匹配
                    strategies = strategies[:len(selector_codes)]
                    
                    base_indent = indent
                    if selector_codes:
                        lines.append(f'{base_indent}try:')
                        # Trace: 设置策略和获取bounds
                        lines.append(f'{base_indent}    if {ctx_var}: {ctx_var}.set_strategy("{strategies[0][0]}", {strategies[0][1]})')
                        lines.append(f'{base_indent}    ele = {selector_codes[0]}')
                        lines.append(f'{base_indent}    if {ctx_var} and {ctx_var}.listener:')
                        lines.append(f'{base_indent}        try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                        lines.append(f'{base_indent}        except: pass')
                        lines.append(f'{base_indent}    ele.click()')
                    
                    current_indent = base_indent
                    if selector_codes:
                        for i in range(1, len(selector_codes)):
                            lines.append(f'{current_indent}except Exception:')
                            current_indent = current_indent + '    '
                            lines.append(f'{current_indent}try:')
                            # Trace: 设置策略和获取bounds
                            lines.append(f'{current_indent}    if {ctx_var}: {ctx_var}.set_strategy("{strategies[i][0]}", {strategies[i][1]})')
                            lines.append(f'{current_indent}    ele = {selector_codes[i]}')
                            lines.append(f'{current_indent}    if {ctx_var} and {ctx_var}.listener:')
                            lines.append(f'{current_indent}        try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                            lines.append(f'{current_indent}        except: pass')
                            lines.append(f'{current_indent}    ele.click()')
                        
                        lines.append(f'{current_indent}except Exception:')
                        final_indent = current_indent + '    '
                        if action.coordinates:
                            x = action.coordinates.get('x', 0)
                            y = action.coordinates.get('y', 0)
                            # Trace: 坐标策略
                            lines.append(f'{final_indent}if {ctx_var}: {ctx_var}.set_strategy("coordinates", -2)')
                            lines.append(f'{final_indent}d.click({x}, {y})')
                        else:
                            lines.append(f'{final_indent}raise')
                    elif action.coordinates:
                        x = action.coordinates.get('x', 0)
                        y = action.coordinates.get('y', 0)
                        lines.append(f'{indent}d.click({x}, {y})')
                else:
                    # 单一选择器
                    lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("{action.selector.strategy}", -1)')
                    lines.append(f'{indent}ele = {selector_code}')
                    lines.append(f'{indent}if {ctx_var} and {ctx_var}.listener:')
                    lines.append(f'{indent}    try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                    lines.append(f'{indent}    except: pass')
                    lines.append(f'{indent}ele.click()')
            elif action.coordinates:
                x = action.coordinates.get('x', 0)
                y = action.coordinates.get('y', 0)
                lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("coordinates", -2)')
                lines.append(f'{indent}d.click({x}, {y})')
        elif action.coordinates:
            # 使用坐标
            x = action.coordinates.get('x', 0)
            y = action.coordinates.get('y', 0)
            lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("coordinates", -2)')
            lines.append(f'{indent}d.click({x}, {y})')
        
        return lines
    
    def _generate_swipe_code(self, action: UIAction, indent: str) -> List[str]:
        """生成滑动代码"""
        lines = []
        ctx_var = f"step_ctx_{action.step_num}"
        
        if action.coordinates:
            coords = action.coordinates
            from_coords = coords.get('from', {})
            to_coords = coords.get('to', {})
            duration = coords.get('duration', 300) / 1000.0  # 转换为秒
            
            x1 = from_coords.get('x', 0)
            y1 = from_coords.get('y', 0)
            x2 = to_coords.get('x', 0)
            y2 = to_coords.get('y', 0)
            
            lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("coordinates", -2)')
            lines.append(f'{indent}d.swipe({x1}, {y1}, {x2}, {y2}, duration={duration})')
        
        return lines
    
    def _generate_input_code(self, action: UIAction, indent: str) -> List[str]:
        """生成输入代码"""
        lines = []
        ctx_var = f"step_ctx_{action.step_num}"
        
        if action.value:
            selector_code = self._generate_selector_code(action.selector) if action.selector else None
            
            # 转义文本中的特殊字符
            escaped_text = action.value.replace('"', '\\"')
            
            if selector_code:
                if action.selector and action.selector.fallbacks:
                    selector_codes = [selector_code] + [
                        self._generate_fallback_selector_code(fallback)
                        for fallback in action.selector.fallbacks
                    ]
                    # 获取策略和值，用于Trace
                    strategies = [(action.selector.strategy, -1)] + [
                        (fb.get('strategy', 'text'), i) for i, fb in enumerate(action.selector.fallbacks)
                    ]
                    
                    selector_codes = [c for c in selector_codes if c]
                    strategies = strategies[:len(selector_codes)]
                    
                    base_indent = indent
                    if selector_codes:
                        lines.append(f'{base_indent}try:')
                        lines.append(f'{base_indent}    if {ctx_var}: {ctx_var}.set_strategy("{strategies[0][0]}", {strategies[0][1]})')
                        lines.append(f'{base_indent}    ele = {selector_codes[0]}')
                        lines.append(f'{base_indent}    if {ctx_var} and {ctx_var}.listener:')
                        lines.append(f'{base_indent}        try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                        lines.append(f'{base_indent}        except: pass')
                        lines.append(f'{base_indent}    ele.set_text("{escaped_text}")')
                    
                    current_indent = base_indent
                    if selector_codes:
                        for i in range(1, len(selector_codes)):
                            lines.append(f'{current_indent}except Exception:')
                            current_indent = current_indent + '    '
                            lines.append(f'{current_indent}try:')
                            lines.append(f'{current_indent}    if {ctx_var}: {ctx_var}.set_strategy("{strategies[i][0]}", {strategies[i][1]})')
                            lines.append(f'{current_indent}    ele = {selector_codes[i]}')
                            lines.append(f'{current_indent}    if {ctx_var} and {ctx_var}.listener:')
                            lines.append(f'{current_indent}        try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                            lines.append(f'{current_indent}        except: pass')
                            lines.append(f'{current_indent}    ele.set_text("{escaped_text}")')
                        
                        lines.append(f'{current_indent}except Exception:')
                        final_indent = current_indent + '    '
                        if action.coordinates:
                            x = action.coordinates.get('x', 0)
                            y = action.coordinates.get('y', 0)
                            lines.append(f'{final_indent}if {ctx_var}: {ctx_var}.set_strategy("coordinates", -2)')
                            lines.append(f'{final_indent}d.click({x}, {y})')
                            lines.append(f'{final_indent}d.send_keys("{escaped_text}")')
                        else:
                            lines.append(f'{final_indent}raise')
                    elif action.coordinates:
                        x = action.coordinates.get('x', 0)
                        y = action.coordinates.get('y', 0)
                        lines.append(f'{indent}d.click({x}, {y})')
                        lines.append(f'{indent}d.send_keys("{escaped_text}")')
                else:
                    lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("{action.selector.strategy}", -1)')
                    lines.append(f'{indent}ele = {selector_code}')
                    lines.append(f'{indent}if {ctx_var} and {ctx_var}.listener:')
                    lines.append(f'{indent}    try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                    lines.append(f'{indent}    except: pass')
                    lines.append(f'{indent}ele.set_text("{escaped_text}")')
            elif action.coordinates:
                x = action.coordinates.get('x', 0)
                y = action.coordinates.get('y', 0)
                lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("coordinates", -2)')
                lines.append(f'{indent}d.click({x}, {y})')
                lines.append(f'{indent}d.send_keys("{escaped_text}")')
        
        return lines
    
    def _generate_wait_code(self, action: UIAction, indent: str) -> List[str]:
        """生成等待代码"""
        lines = []
        ctx_var = f"step_ctx_{action.step_num}"
        
        if action.selector:
            lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("{action.selector.strategy}", -1)')
            selector_code = self._generate_selector_code(action.selector)
            if selector_code:
                timeout = action.wait_after / 1000.0 if action.wait_after > 0 else 5.0
                # Trace: Wait and capture bounds
                lines.append(f'{indent}ele = {selector_code}')
                lines.append(f'{indent}exists = ele.wait(timeout={timeout})')
                lines.append(f'{indent}if exists and {ctx_var} and {ctx_var}.listener:')
                lines.append(f'{indent}    try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
                lines.append(f'{indent}    except: pass')
        elif action.wait_after > 0:
            lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("time", -2)')
            wait_seconds = action.wait_after / 1000.0
            lines.append(f'{indent}time.sleep({wait_seconds})')
        
        return lines
    
    def _generate_assertion_code(self, action: UIAction, indent: str) -> List[str]:
        """生成断言代码"""
        lines = []
        ctx_var = f"step_ctx_{action.step_num}"
        
        # 解析 assertion_type 和 expected_value
        # action.value 格式可能是 "type" 或 "type:expected_value"
        full_value = action.value or ""
        if ':' in full_value:
            assertion_type, expected_value = full_value.split(':', 1)
        else:
            assertion_type = full_value
            expected_value = ""
            
        selector_code = self._generate_selector_code(action.selector) if action.selector else None
        
        if not selector_code:
            lines.append(f'{indent}# 警告: 断言缺少选择器')
            return lines
            
        # Trace: Log strategy
        if action.selector:
            lines.append(f'{indent}if {ctx_var}: {ctx_var}.set_strategy("{action.selector.strategy}", -1)')
            lines.append(f'{indent}ele = {selector_code}')
            
        if assertion_type == 'exists':
            lines.append(f'{indent}assert ele.exists(), "断言失败: 元素不存在"')
            # Trace: Capture bounds if exists
            lines.append(f'{indent}if {ctx_var} and {ctx_var}.listener:')
            lines.append(f'{indent}    try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
            lines.append(f'{indent}    except: pass')
        elif assertion_type == 'not_exists':
            lines.append(f'{indent}assert not ele.exists(), "断言失败: 元素存在"')
        elif assertion_type == 'text_contains':
            # Trace: Capture bounds before text check
            lines.append(f'{indent}if {ctx_var} and {ctx_var}.listener:')
            lines.append(f'{indent}    try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
            lines.append(f'{indent}    except: pass')
            
            if expected_value:
                expected = expected_value.replace('"', '\\"')
                lines.append(f'{indent}actual_text = ele.get_text()')
                lines.append(f'{indent}assert "{expected}" in actual_text, f"断言失败: 文本 {{actual_text}} 不包含 {expected}"')
            else:
                lines.append(f'{indent}# 警告: 断言缺少预期值')
        elif assertion_type == 'text_matches':
            # Trace: Capture bounds before text check
            lines.append(f'{indent}if {ctx_var} and {ctx_var}.listener:')
            lines.append(f'{indent}    try: {ctx_var}.set_bounds(ele.info.get("bounds"))')
            lines.append(f'{indent}    except: pass')
            
            if expected_value:
                # 使用 re.search 进行正则匹配
                expected = expected_value.replace('"', '\\"')
                lines.append(f'{indent}text = ele.get_text()')
                lines.append(f'{indent}assert re.search(r"{expected}", text), f"断言失败: 文本 {{text}} 不匹配正则 {expected}"')
            else:
                lines.append(f'{indent}# 警告: 断言缺少预期值')
        else:
            lines.append(f'{indent}# 未支持的断言类型: {assertion_type}')
            
        return lines
    
    def _generate_selector_code(self, selector) -> str:
        """生成选择器代码"""
        if not selector:
            return ''
        
        strategy = selector.strategy
        value = selector.value
        
        if strategy == 'resource_id':
            return f'd(resourceId="{value}")'
        elif strategy == 'text':
            return f'd(text="{value}")'
        elif strategy == 'content_desc':
            return f'd(description="{value}")'
        elif strategy == 'coordinates':
            return ''
        else:
            return f'd(text="{value}")  # 未知策略'
    
    def _generate_fallback_selector_code(self, fallback: dict) -> str:
        """生成备用选择器代码"""
        strategy = fallback.get('strategy', 'text')
        value = fallback.get('value', '')
        
        if strategy == 'resource_id':
            return f'd(resourceId="{value}")'
        elif strategy == 'text':
            return f'd(text="{value}")'
        elif strategy == 'content_desc':
            return f'd(description="{value}")'
        elif strategy == 'coordinates':
            return ''
        else:
            return f'd(text="{value}")'
