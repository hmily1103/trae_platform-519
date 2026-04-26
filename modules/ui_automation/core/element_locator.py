"""
元素定位器
负责坐标到控件的映射，生成多策略选择器
"""
from typing import Optional, Dict, List, Tuple
from .ui_tree_parser import UITreeParser, UIElement
from ..models import UISelector
from utils.logger import setup_logger

logger = setup_logger('element_locator')


class ElementLocator:
    """元素定位器"""
    
    def __init__(self, ui_tree_parser: UITreeParser):
        """
        初始化定位器
        
        :param ui_tree_parser: UI树解析器
        """
        self.parser = ui_tree_parser
    
    def locate_by_coordinates(self, x: int, y: int) -> Optional[UISelector]:
        """
        通过坐标定位元素，生成多策略选择器
        
        :param x: X坐标
        :param y: Y坐标
        :return: 选择器
        """
        element = self._find_best_element_by_coordinates(x, y)
        if not element:
            # 如果找不到元素，返回坐标选择器
            return UISelector(
                strategy='coordinates',
                value=f"{x},{y}",
                bounds=None
            )
        
        # 生成多策略选择器
        return self._build_selector(element)

    def _find_best_element_by_coordinates(self, x: int, y: int) -> Optional[UIElement]:
        """
        查找包含坐标的最具体元素（面积最小）
        
        :param x: X坐标
        :param y: Y坐标
        :return: 最佳匹配元素
        """
        candidates: List[Tuple[int, int, UIElement]] = []

        # 遍历所有元素，寻找包含坐标的候选项
        # 使用 reversed 是为了在面积相同时优先选择层级靠后的（通常是覆盖在上面的）
        for idx, element in enumerate(reversed(self.parser.elements)):
            bounds = element.bounds
            if not bounds:
                continue
            
            # 判断点是否在 bounds 内
            if not (bounds['left'] <= x <= bounds['right'] and bounds['top'] <= y <= bounds['bottom']):
                continue

            # 计算面积
            area = max(0, (bounds['right'] - bounds['left']) * (bounds['bottom'] - bounds['top']))
            
            # (area, idx, element)
            # idx 用于在面积相同时保持相对顺序
            candidates.append((area, idx, element))

        if not candidates:
            return None

        # 按面积从小到大排序 -> 面积最小 = 最具体的控件
        candidates.sort(key=lambda item: (item[0], item[1]))
        
        return candidates[0][2]

    def _is_valid_resource_id(self, resource_id: str) -> bool:
        if not resource_id:
            return False
        return (':id/' in resource_id) or ('/id/' in resource_id)
    
    def _build_selector(self, element: UIElement) -> UISelector:
        """
        构建多策略选择器 (Upgrade 1: Robust Selector)
        
        优先级：resource-id > text > content-desc > xpath > coordinates
        """
        fallbacks = []

        # 1. Resource ID
        if element.resource_id and self._is_valid_resource_id(element.resource_id):
            fallbacks.append({
                "strategy": "resource_id",
                "value": element.resource_id
            })

        # 2. Text + Class (More specific than just text)
        if element.text:
            fallbacks.append({
                "strategy": "text",
                "value": element.text,
                "class_name": element.class_name
            })

        # 3. Content Desc
        if element.content_desc:
            fallbacks.append({
                "strategy": "content_desc",
                "value": element.content_desc
            })

        # 4. XPath (Relative)
        xpath = self._build_relative_xpath(element)
        if xpath:
            fallbacks.append({
                "strategy": "xpath",
                "value": xpath
            })

        # 5. Coordinates (Last resort)
        if element.bounds:
            fallbacks.append({
                "strategy": "coordinates",
                "bounds": element.bounds,
                "value": f"{element.bounds['center_x']},{element.bounds['center_y']}"
            })

        # Determine Primary Strategy
        # We pick the first one available in the priority list
        if not fallbacks:
            # Should not happen if coordinates fallback is added
            return UISelector(strategy="coordinates", value="0,0") # Fallback
            
        primary = fallbacks[0]
        
        return UISelector(
            strategy=primary["strategy"],
            value=primary.get("value", ""),
            fallbacks=fallbacks,
            bounds=element.bounds,
            class_name=element.class_name
        )

    def _is_stable_anchor(self, node) -> bool:
        """
        判断节点是否适合作为锚点
        """
        # Resource ID needs to be valid (not auto-generated or empty)
        res_id = node.get("resource-id")
        if res_id and self._is_valid_resource_id(res_id):
            return True
            
        # Content Desc is usually good
        if node.get("content-desc"):
            return True
            
        # Text is good if present
        if node.get("text"):
            return True
            
        return False

    def _find_anchor_node(self, node):
        """
        向上寻找最近的稳定锚点
        """
        current = node
        while current is not None:
            if self._is_stable_anchor(current):
                return current
            try:
                # 仅 lxml 支持 getparent()
                if hasattr(current, 'getparent'):
                    current = current.getparent()
                else:
                    return None
            except Exception:
                return None
        return None

    def _get_node_index(self, node) -> int:
        """
        获取节点在同类兄弟节点中的索引 (1-based)
        """
        try:
            parent = None
            if hasattr(node, 'getparent'):
                parent = node.getparent()
            
            if parent is None:
                return 1

            same_class = [
                n for n in parent
                if n.get("class") == node.get("class")
            ]

            return same_class.index(node) + 1
        except Exception:
            return 1

    def _build_node_xpath(self, node) -> str:
        """
        构建单节点的XPath片段
        """
        class_name = node.get("class", "*")
        conditions = []

        # 1. Resource ID
        res_id = node.get("resource-id")
        if res_id and self._is_valid_resource_id(res_id):
            conditions.append(f"@resource-id='{res_id}'")

        # 2. Text
        text = node.get("text")
        if text:
            # Handle quotes in text
            if "'" in text:
                 conditions.append(f'@text="{text}"')
            else:
                 conditions.append(f"@text='{text}'")

        # 3. Content Desc
        desc = node.get("content-desc")
        if desc:
             if "'" in desc:
                 conditions.append(f'@content-desc="{desc}"')
             else:
                 conditions.append(f"@content-desc='{desc}'")

        if conditions:
            return f"{class_name}[{' and '.join(conditions)}]"

        # Fallback to index
        index = self._get_node_index(node)
        return f"{class_name}[{index}]"

    def _build_relative_xpath(self, element: UIElement) -> Optional[str]:
        """
        构建相对XPath (Upgrade: Anchor-based Relative XPath)
        """
        node = element.element
        if node is None:
            return None
            
        anchor = self._find_anchor_node(node)

        # If no anchor found or the node itself is an anchor
        if anchor is None or anchor == node:
            return f"//{self._build_node_xpath(node)}"

        path_parts = []
        current = node

        while current is not None and current != anchor:
            path_parts.append(self._build_node_xpath(current))
            try:
                if hasattr(current, 'getparent'):
                    current = current.getparent()
                else:
                    break
            except Exception:
                break

        path_parts.reverse()

        anchor_xpath = self._build_node_xpath(anchor)
        
        # Combine: //Anchor/Path/To/Target
        return f"//{anchor_xpath}/" + "/".join(path_parts)
