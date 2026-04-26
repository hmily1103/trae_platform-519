"""
UI树解析器
解析XML格式的UI树，提取控件信息
"""
# Switch to lxml for better XPath support and getparent()
try:
    import lxml.etree as ET
except ImportError:
    import xml.etree.ElementTree as ET
    
from typing import List, Dict, Optional, Tuple
import re
from utils.logger import setup_logger

logger = setup_logger('ui_tree_parser')


class UIElement:
    """UI元素"""
    
    def __init__(self, element):
        """
        初始化UI元素
        
        :param element: XML元素 (lxml or etree)
        """
        self.element = element
        self.resource_id = self._normalize_attr(element.get('resource-id', ''))
        self.text = self._normalize_attr(element.get('text', ''))
        self.content_desc = self._normalize_attr(element.get('content-desc', ''))
        self.class_name = element.get('class', '')
        self.package = element.get('package', '')
        self.bounds = self._parse_bounds(element.get('bounds', ''))
        self.clickable = element.get('clickable', 'false').lower() == 'true'
        self.focusable = element.get('focusable', 'false').lower() == 'true'
        self.scrollable = element.get('scrollable', 'false').lower() == 'true'

    def _normalize_attr(self, value: str) -> str:
        if value is None:
            return ''
        value = str(value).strip()
        if value.lower() in ('null', 'none'):
            return ''
        return value
    
    def _parse_bounds(self, bounds_str: str) -> Optional[Dict]:
        """
        解析bounds字符串
        
        格式: [left,top][right,bottom]
        """
        if not bounds_str:
            return None
        
        try:
            m = re.match(r'^\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]$', bounds_str.strip())
            if not m:
                return None
            left, top, right, bottom = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
            
            return {
                'left': left,
                'top': top,
                'right': right,
                'bottom': bottom,
                'center_x': (left + right) // 2,
                'center_y': (top + bottom) // 2
            }
        except Exception:
            return None
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'resource_id': self.resource_id,
            'text': self.text,
            'content_desc': self.content_desc,
            'class_name': self.class_name,
            'package': self.package,
            'bounds': self.bounds,
            'clickable': self.clickable,
            'focusable': self.focusable,
            'scrollable': self.scrollable
        }


class UITreeParser:
    """UI树解析器"""
    
    def __init__(self, xml_content: str):
        """
        初始化解析器
        
        :param xml_content: UI树XML内容
        """
        try:
            # Handle encoding if bytes provided
            if isinstance(xml_content, str):
                # Remove encoding declaration if present as it might conflict
                xml_content = re.sub(r'<\?xml.*?\?>', '', xml_content)
                self.root = ET.fromstring(xml_content.encode('utf-8'))
            else:
                self.root = ET.fromstring(xml_content)
                
            self.elements: List[UIElement] = []
            self._parse_tree()
        except Exception as e:
            logger.error(f"解析UI树失败: {e}", exc_info=True)
            self.root = None
            self.elements = []
    
    def _parse_tree(self):
        """解析UI树"""
        if self.root is None:
            return
        
        # Use iter() for depth-first traversal which is compatible with both
        for element in self.root.iter():
             # Skip root if it doesn't have attributes we care about (optional)
             ui_element = UIElement(element)
             self.elements.append(ui_element)
    
    def find_element_by_coordinates(self, x: int, y: int) -> Optional[UIElement]:
        """
        通过坐标查找元素
        
        :param x: X坐标
        :param y: Y坐标
        :return: UI元素
        """
        # 从后往前查找（后面的元素在上层）
        for element in reversed(self.elements):
            bounds = element.bounds
            if not bounds:
                continue
            
            if (bounds['left'] <= x <= bounds['right'] and
                bounds['top'] <= y <= bounds['bottom']):
                return element
        
        return None
    
    def find_elements_by_resource_id(self, resource_id: str) -> List[UIElement]:
        """
        通过resource-id查找元素
        
        :param resource_id: resource-id
        :return: 元素列表
        """
        return [e for e in self.elements if e.resource_id == resource_id]
    
    def find_elements_by_text(self, text: str) -> List[UIElement]:
        """
        通过text查找元素
        
        :param text: 文本
        :return: 元素列表
        """
        return [e for e in self.elements if e.text == text]
    
    def find_elements_by_content_desc(self, content_desc: str) -> List[UIElement]:
        """
        通过content-desc查找元素
        
        :param content_desc: content-desc
        :return: 元素列表
        """
        return [e for e in self.elements if e.content_desc == content_desc]
    
    def get_all_clickable_elements(self) -> List[UIElement]:
        """获取所有可点击元素"""
        return [e for e in self.elements if e.clickable]
    
    def get_all_focusable_elements(self) -> List[UIElement]:
        """获取所有可聚焦元素"""
        return [e for e in self.elements if e.focusable]
