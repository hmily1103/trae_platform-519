"""
统一 API 响应格式
"""
from flask import jsonify
from typing import Any, Dict, Optional


def success_response(data: Any = None, message: str = "操作成功") -> Dict:
    response = {
        'ok': True,
        'success': True,
        'message': message
    }
    if data is not None:
        response['data'] = data
    return jsonify(response)


def error_response(message: str = "操作失败", error: Optional[str] = None, status_code: int = 400) -> tuple:
    response = {
        'ok': False,
        'success': False,
        'message': message,
        'error': error or message
    }
    return jsonify(response), status_code
