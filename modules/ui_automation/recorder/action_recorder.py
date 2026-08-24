"""
操作录制器
负责录制用户操作并生成操作记录
"""
import threading
import time
import os
from typing import Optional, Dict, Union
from datetime import datetime
from ..models import RecordingSession, UIAction, UISelector
from ..core.device_controller import DeviceController
from ..core.ui_tree_parser import UITreeParser
from ..core.element_locator import ElementLocator
from ..storage import RecordingStorage
from utils.logger import setup_logger

logger = setup_logger('action_recorder')


class ActionRecorder:
    """操作录制器"""
    
    def __init__(self, device_id: str, recording_id: str, 
                 storage: RecordingStorage, controller: DeviceController, auto_ui_tree: bool = True):
        """
        初始化录制器
        
        :param device_id: 设备ID
        :param recording_id: 录制ID
        :param storage: 存储管理器
        :param controller: 设备控制器实例
        :param auto_ui_tree: 是否自动获取UI树
        """
        self.device_id = device_id
        self.recording_id = recording_id
        self.storage = storage
        self.auto_ui_tree = auto_ui_tree
        
        self.controller = controller
        self.session = RecordingSession(
            id=recording_id,
            device_id=device_id,
            package_name="",  # 稍后设置
            created_at=datetime.now(),
            platform="android",
            target=device_id,
        )
        self.is_recording = False
        self.current_step = 0
        self.last_error: str = ""
        
        # 线程锁，保证并发安全
        self._lock = threading.Lock()
    
    def start(self, package_name: str = "", description: str = "", project_id: str = "", name: str = ""):
        """
        开始录制
        
        :param package_name: 应用包名
        :param description: 描述
        :param project_id: 所属项目ID
        :param name: 用例名称
        """
        with self._lock:
            self.session.package_name = package_name
            self.session.description = description
            self.session.project_id = project_id
            self.session.name = name
            self.session.platform = "android"
            self.session.target = self.device_id
            self.session.entry_url = ""
            self.is_recording = True
            self.current_step = 0
            self.last_error = ""
        
        # 启动UI树监控，确保持续缓存
        if self.auto_ui_tree:
            self.controller.start_monitor()
            
        logger.info(f"开始录制: {self.recording_id} (Project: {project_id})")
    
    def stop(self):
        """停止录制"""
        with self._lock:
            self.is_recording = False
            count = len(self.session.actions)
        
        # 停止监控
        if self.auto_ui_tree:
            self.controller.stop_monitor()
            
        logger.info(f"停止录制: {self.recording_id}, 共录制 {count} 个操作")
    
    def record_click(self, x: int, y: int, description: str = "") -> bool:
        """
        录制点击操作（异步模式）
        
        :param x: X坐标
        :param y: Y坐标
        :param description: 描述
        :return: 是否成功触发
        """
        if not self.is_recording:
            return False
        
        # 1. 执行点击
        if not self.controller.click(x, y):
            self.last_error = self.controller.last_output
            return False
            
        # 2. 触发异步录制
        return self.record_step_async(x, y, description, action_type='click')

    def record_step_async(self, x: int, y: int, description: str = "", action_type: str = 'click') -> bool:
        """
        异步录制步骤 (仅记录，不执行设备操作)
        """
        if not self.is_recording:
            return False

        try:
            self.last_error = ""
            
            # 1. 确定步骤号 (线程安全)
            with self._lock:
                if not self.is_recording:
                    return False
                self.current_step = max(self.current_step, self.storage.get_artifact_step_count(self.recording_id)) + 1
                current_step_local = self.current_step
                
                # 2. 立即创建 Pending 状态的操作记录 (内存操作，极快)
                pending_action = UIAction(
                    step_num=current_step_local,
                    action_type=action_type,
                    coordinates={'x': x, 'y': y},
                    timestamp=time.time(),
                    description=description or f"点击 ({x}, {y})",
                    status="pending"
                )
                self.session.actions.append(pending_action)
            
            # 3. 启动异步分析线程
            threading.Thread(
                target=self._async_analyze_worker,
                args=(current_step_local, x, y, description, time.time())
            ).start()
            
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"异步录制触发失败: {e}", exc_info=True)
            return False

    def _async_analyze_worker(self, step_num: int, x: int, y: int, description: str, timestamp: float):
        """后台异步执行重度任务：截图、分析UI、保存"""
        try:
            # 0. 先保存一次 Pending 状态 (确保落盘)
            with self._lock:
                self.storage.save_recording(self.session)

            # 1. 截图 (耗时)
            screenshot_path = self.storage.get_screenshot_path(self.recording_id, step_num)
            if not self.controller.screenshot(screenshot_path):
                logger.warning(f"截图失败: {screenshot_path}")
            
            # 2. 获取UI树和选择器 (利用缓存，快)
            ui_tree_path = None
            selector = None
            
            if self.auto_ui_tree:
                ui_tree_path = self.storage.get_ui_tree_path(self.recording_id, step_num)
                # 使用缓存的UI树
                ui_tree_content = self.controller.get_cached_ui_tree()
                
                if ui_tree_content:
                    # 保存UI树文件
                    try:
                        os.makedirs(os.path.dirname(ui_tree_path), exist_ok=True)
                        with open(ui_tree_path, "w", encoding="utf-8") as f:
                            f.write(ui_tree_content)
                    except Exception as e:
                        logger.warning(f"保存UI树失败: {e}")

                    parser = UITreeParser(ui_tree_content)
                    locator = ElementLocator(parser)
                    selector = locator.locate_by_coordinates(x, y)
            
            # 如果UI树获取失败，使用坐标
            if not selector:
                selector = UISelector(
                    strategy='coordinates',
                    value=f"{x},{y}",
                    bounds=None
                )
            
            # 3. 更新操作记录
            display_text = ""
            if selector:
                text_value = None
                # 尝试查找文本值（无论是主策略还是备选策略）
                if selector.strategy == 'text':
                    text_value = selector.value
                else:
                    # 检查 fallbacks
                    for fb in (selector.fallbacks or []):
                        if fb.get('strategy') == 'text':
                            text_value = fb.get('value')
                            break
                
                if selector.strategy == 'resource_id':
                    # 提取简短ID
                    simple_id = selector.value.split('/')[-1] if '/' in selector.value else selector.value
                    if text_value:
                        display_text = f"点击 {simple_id} ('{text_value}')"
                    else:
                        display_text = f"点击 {simple_id}"
                elif selector.strategy == 'text':
                    display_text = f"点击 '{selector.value}'"
                elif selector.strategy == 'content_desc':
                    display_text = f"点击 '{selector.value}'"
                elif selector.strategy == 'xpath':
                    display_text = f"点击 XPath"
                elif selector.strategy == 'coordinates':
                    display_text = f"点击 ({x}, {y})"
                    
            if not display_text:
                display_text = f"点击 ({x}, {y})"
                
            final_description = description if description else display_text
                
            with self._lock:
                # 找到对应的 action 并更新
                target_action = None
                for action in self.session.actions:
                    if action.step_num == step_num:
                        target_action = action
                        break
                
                if target_action:
                    target_action.selector = selector
                    target_action.screenshot = screenshot_path if os.path.exists(screenshot_path) else None
                    target_action.ui_tree = ui_tree_path if ui_tree_path and os.path.exists(ui_tree_path) else None
                    target_action.description = final_description
                    target_action.display = display_text
                    target_action.status = "completed"
                else:
                    # 理论上不应该发生，因为 record_click 已经添加了
                    logger.warning(f"未找到 Step {step_num} 的 Pending Action，重新创建")
                    target_action = UIAction(
                        step_num=step_num,
                        action_type='click',
                        selector=selector,
                        coordinates={'x': x, 'y': y},
                        screenshot=screenshot_path if os.path.exists(screenshot_path) else None,
                        ui_tree=ui_tree_path if ui_tree_path and os.path.exists(ui_tree_path) else None,
                        timestamp=timestamp,
                        wait_after=1000,
                        description=final_description,
                        status="completed"
                    )
                    self.session.actions.append(target_action)

                self.storage.save_recording(self.session)
                
            logger.info(f"Step {step_num} 异步录制完成: {final_description}")
            
        except Exception as e:
            logger.error(f"后台录制分析失败: {e}", exc_info=True)
            # 发生错误，更新状态为 failed
            with self._lock:
                for action in self.session.actions:
                    if action.step_num == step_num:
                        action.status = "failed"
                        action.description += f" (Error: {str(e)})"
                        break
                self.storage.save_recording(self.session)
    
    def record_swipe(self, x1: int, y1: int, x2: int, y2: int, 
                    duration: int = 300, description: str = "") -> bool:
        """
        录制滑动操作
        
        :param x1: 起始X坐标
        :param y1: 起始Y坐标
        :param x2: 结束X坐标
        :param y2: 结束Y坐标
        :param duration: 滑动时长（毫秒）
        :param description: 描述
        :return: 是否成功
        """
        if not self.is_recording:
            return False
        
        try:
            self.last_error = ""
            
            with self._lock:
                # 再次检查是否还在录制
                if not self.is_recording:
                    return False
                self.current_step = max(self.current_step, self.storage.get_artifact_step_count(self.recording_id)) + 1
                current_step_local = self.current_step
            
            # 截图
            screenshot_path = self.storage.get_screenshot_path(self.recording_id, current_step_local)
            if not self.controller.screenshot(screenshot_path):
                logger.warning(f"截图失败: {screenshot_path}")
            
            # 创建操作记录
            action = UIAction(
                step_num=current_step_local,
                action_type='swipe',
                coordinates={
                    'from': {'x': x1, 'y': y1},
                    'to': {'x': x2, 'y': y2},
                    'duration': duration
                },
                screenshot=screenshot_path if os.path.exists(screenshot_path) else None,
                timestamp=time.time(),
                wait_after=1000,
                description=description or f"滑动 ({x1}, {y1}) -> ({x2}, {y2})",
                display=f"滑动 ({x1}, {y1}) -> ({x2}, {y2})"
            )
            
            with self._lock:
                self.session.actions.append(action)
                self.storage.save_recording(self.session)
            
            # 执行滑动
            self.controller.swipe(x1, y1, x2, y2, duration)
            
            logger.debug(f"录制滑动: step={current_step_local}")
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"录制滑动失败: {e}", exc_info=True)
            return False

    def record_key(self, key_code: Union[int, str], description: str = "") -> bool:
        """
        录制按键操作
        
        :param key_code: 键值
        :param description: 描述
        :return: 是否成功
        """
        if not self.is_recording:
            return False
        
        try:
            self.last_error = ""
            
            with self._lock:
                if not self.is_recording:
                    return False
                self.current_step = max(self.current_step, self.storage.get_artifact_step_count(self.recording_id)) + 1
                current_step_local = self.current_step
            
            # 执行按键
            if not self.controller.press_key(key_code):
                self.last_error = self.controller.last_output
                return False
            
            # 截图 (按键后截图，可能需要等待界面变化)
            time.sleep(0.5) 
            screenshot_path = self.storage.get_screenshot_path(self.recording_id, current_step_local)
            if not self.controller.screenshot(screenshot_path):
                logger.warning(f"截图失败: {screenshot_path}")
            
            # 创建操作记录
            key_name = str(key_code)
            if str(key_code) == '4' or str(key_code).endswith('BACK'):
                key_name = "返回"
            elif str(key_code) == '3' or str(key_code).endswith('HOME'):
                key_name = "Home"
                
            action = UIAction(
                step_num=current_step_local,
                action_type='key',
                coordinates={}, # 按键无坐标
                screenshot=screenshot_path if os.path.exists(screenshot_path) else None,
                timestamp=time.time(),
                wait_after=1000,
                description=description or f"按键: {key_name}",
                display=f"按键: {key_name}"
            )
            
            # 存储 key_code 到 details
            # action.details = {'key_code': key_code} # UIAction 需要支持 details 字段，这里暂时省略或存入 coordinates?
            # 存入 coordinates 暂时替代
            action.coordinates['key_code'] = str(key_code)

            with self._lock:
                self.session.actions.append(action)
                self.storage.save_recording(self.session)
                
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"录制按键失败: {e}", exc_info=True)
            return False
    
    def record_input(self, x: int, y: int, text: str, description: str = "") -> bool:
        """
        录制输入操作
        
        :param x: 输入框X坐标（用于定位）
        :param y: 输入框Y坐标（用于定位）
        :param text: 输入文本
        :param description: 描述
        :return: 是否成功
        """
        if not self.is_recording:
            return False
        
        try:
            self.last_error = ""
            
            with self._lock:
                if not self.is_recording:
                    return False
                self.current_step = max(self.current_step, self.storage.get_artifact_step_count(self.recording_id)) + 1
                current_step_local = self.current_step
            
            # 截图
            screenshot_path = self.storage.get_screenshot_path(self.recording_id, current_step_local)
            if not self.controller.screenshot(screenshot_path):
                logger.warning(f"截图失败: {screenshot_path}")
            
            # 获取UI树和选择器
            ui_tree_path = None
            selector = None
            
            if self.auto_ui_tree:
                ui_tree_path = self.storage.get_ui_tree_path(self.recording_id, current_step_local)
                ui_tree_content = self.controller.get_ui_tree(ui_tree_path)
                
                if ui_tree_content:
                    parser = UITreeParser(ui_tree_content)
                    locator = ElementLocator(parser)
                    selector = locator.locate_by_coordinates(x, y)
            
            # 如果UI树获取失败，使用坐标
            if not selector:
                selector = UISelector(
                    strategy='coordinates',
                    value=f"{x},{y}",
                    bounds=None
                )
            
            # 构造描述
            if not description:
                target_desc = ""
                if selector:
                    if selector.strategy == 'resource_id':
                        target_desc = selector.value.split('/')[-1] if '/' in selector.value else selector.value
                    elif selector.strategy == 'text' and selector.value:
                        target_desc = f"'{selector.value}'"
                    elif selector.strategy == 'content_desc':
                        target_desc = f"'{selector.value}'"
                
                if target_desc:
                    description = f"在 {target_desc} 输入: {text}"
                else:
                    description = f"输入: {text}"

            # 创建操作记录
            action = UIAction(
                step_num=current_step_local,
                action_type='input',
                selector=selector,
                value=text,
                coordinates={'x': x, 'y': y},
                screenshot=screenshot_path if os.path.exists(screenshot_path) else None,
                ui_tree=ui_tree_path if ui_tree_path and os.path.exists(ui_tree_path) else None,
                timestamp=time.time(),
                wait_after=1000,
                description=description
            )
            
            with self._lock:
                self.session.actions.append(action)
                self.storage.save_recording(self.session)
            
            # 执行输入
            self.controller.input_text(text)
            
            logger.debug(f"录制输入: step={current_step_local}, text={text}")
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"录制输入失败: {e}", exc_info=True)
            return False
    
    def record_assertion(self, x: int, y: int, assertion_type: str, expected_value: str = "", description: str = "") -> bool:
        """
        录制断言操作
        
        :param x: X坐标
        :param y: Y坐标
        :param assertion_type: 断言类型 (exists, not_exists, text_contains, text_matches)
        :param expected_value: 预期值
        :param description: 描述
        :return: 是否成功
        """
        if not self.is_recording:
            return False
        
        try:
            self.last_error = ""
            self.current_step = max(self.current_step, self.storage.get_artifact_step_count(self.recording_id)) + 1
            
            # 截图
            screenshot_path = self.storage.get_screenshot_path(self.recording_id, self.current_step)
            if not self.controller.screenshot(screenshot_path):
                logger.warning(f"截图失败: {screenshot_path}")
            
            # 获取UI树
            ui_tree_path = None
            selector = None
            
            if self.auto_ui_tree:
                ui_tree_path = self.storage.get_ui_tree_path(self.recording_id, self.current_step)
                ui_tree_content = self.controller.get_ui_tree(ui_tree_path)
                
                if ui_tree_content:
                    parser = UITreeParser(ui_tree_content)
                    locator = ElementLocator(parser)
                    selector = locator.locate_by_coordinates(x, y)
            
            # 如果UI树获取失败，使用坐标
            if not selector:
                selector = UISelector(
                    strategy='coordinates',
                    value=f"{x},{y}",
                    bounds=None
                )
            
            # 构造value字段 (type:expected)
            value = assertion_type
            if expected_value:
                value = f"{assertion_type}:{expected_value}"

            # 创建操作记录
            action = UIAction(
                step_num=self.current_step,
                action_type='assertion',
                selector=selector,
                value=value,
                coordinates={'x': x, 'y': y},
                screenshot=screenshot_path if os.path.exists(screenshot_path) else None,
                ui_tree=ui_tree_path if ui_tree_path and os.path.exists(ui_tree_path) else None,
                timestamp=time.time(),
                wait_after=0, # 断言后不需要等待
                description=description or f"断言: {assertion_type} {expected_value}"
            )
            
            self.session.actions.append(action)
            self.storage.save_recording(self.session)
            
            # 断言不执行设备操作
            
            logger.debug(f"录制断言: step={self.current_step}, type={assertion_type}")
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"录制断言失败: {e}", exc_info=True)
            return False

    def delete_action(self, index: int) -> bool:
        """
        删除指定索引的操作
        
        :param index: 操作索引 (从0开始)
        :return: 是否成功
        """
        if not self.is_recording:
            return False
            
        try:
            if 0 <= index < len(self.session.actions):
                # 获取要删除的操作
                action = self.session.actions[index]
                logger.info(f"删除操作: index={index}, type={action.action_type}, step={action.step_num}")
                
                # 从列表中移除
                self.session.actions.pop(index)
                
                # 保存更新后的会话
                self.storage.save_recording(self.session)
                return True
            else:
                self.last_error = f"索引越界: {index}, 总数: {len(self.session.actions)}"
                return False
                
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"删除操作失败: {e}", exc_info=True)
            return False

    def update_action(self, index: int, action_type: str = None, 
                     value: str = None, description: str = None,
                     wait_after: int = None) -> bool:
        """
        更新指定索引的操作
        
        :param index: 操作索引
        :param action_type: 新的操作类型 (可选)
        :param value: 新的值 (可选)
        :param description: 新的描述 (可选)
        :param wait_after: 操作后等待时间(毫秒) (可选)
        :return: 是否成功
        """
        if not self.is_recording:
            return False
            
        try:
            if 0 <= index < len(self.session.actions):
                action = self.session.actions[index]
                
                if action_type:
                    action.action_type = action_type
                if value is not None:
                    action.value = value
                if description is not None:
                    action.description = description
                if wait_after is not None:
                    action.wait_after = wait_after
                    
                self.storage.save_recording(self.session)
                logger.info(f"更新操作: index={index}, type={action.action_type}")
                return True
            else:
                self.last_error = f"索引越界: {index}"
                return False
                
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"更新操作失败: {e}", exc_info=True)
            return False

    def save(self) -> bool:
        """保存录制"""
        return self.storage.save_recording(self.session)
    
    def get_session(self) -> RecordingSession:
        """获取录制会话"""
        return self.session
