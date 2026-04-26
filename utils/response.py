"""
统一 API 响应格式
"""
from flask import jsonify
from typing import Any, Dict, Optional


def success_response(data: Any = None, message: str = "操作成功") -> Dict:
    """
    成功响应
    
    :param data: 响应数据
    :param message: 提示消息
    :return: JSON 响应字典
    """
    response = {
        'ok': True,
        'success': True,
        'message': message
    }
    if data is not None:
        response['data'] = data
    return jsonify(response)


def error_response(message: str = "操作失败", error: Optional[str] = None, status_code: int = 400) -> tuple:
    """
    错误响应
    
    :param message: 用户友好的错误消息
    :param error: 技术错误详情（用于调试）
    :param status_code: HTTP 状态码
    :return: (JSON 响应字典, 状态码)
    """
    response = {
        'ok': False,
        'success': False,
        'message': message,
        'error': error or message
    }
    return jsonify(response), status_code


def validate_required(data: Dict, *fields) -> Optional[tuple]:
    """
    验证必填字段
    
    :param data: 请求数据字典
    :param fields: 必填字段名
    :return: 如果验证失败返回错误响应，否则返回 None
    """
    def is_missing(field: str) -> bool:
        if field not in data:
            return True
        value = data.get(field)
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        return False

    missing = [field for field in fields if is_missing(field)]
    if missing:
        return error_response(
            message=f"缺少必填字段: {', '.join(missing)}",
            error=f"Missing required fields: {', '.join(missing)}"
        )
    return None

