from flask import Blueprint, render_template, request, jsonify, current_app
import json
import os
from utils.response import success_response, error_response
from utils.logger import setup_logger

sanfang_bp = Blueprint('sanfang', __name__, template_folder='templates')
logger = setup_logger('sanfang_module')

CONFIG_PATH = r"D:\trae-code\sanfang\config.json"

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"error": "Config file not found"}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}

def save_config(data):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True, "保存成功"
    except Exception as e:
        return False, str(e)

@sanfang_bp.route('/')
def index():
    return render_template('sanfang_index.html')

@sanfang_bp.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'GET':
        try:
            config = load_config()
            if 'error' in config:
                return error_response(
                    message=config['error'],
                    error=config['error'],
                    status_code=404
                )
            return success_response(data=config)
        except Exception as e:
            logger.error(f'读取配置失败: {e}', exc_info=True)
            return error_response(
                message='读取配置失败',
                error=str(e),
                status_code=500
            )
    elif request.method == 'POST':
        try:
            data = request.json
            if not data:
                return error_response(
                    message='配置数据不能为空',
                    error='empty config data',
                    status_code=400
                )
            success, msg = save_config(data)
            if success:
                logger.info('配置保存成功')
                return success_response(message=msg)
            else:
                return error_response(
                    message=msg,
                    error=msg,
                    status_code=500
                )
        except Exception as e:
            logger.error(f'保存配置失败: {e}', exc_info=True)
            return error_response(
                message='保存配置失败',
                error=str(e),
                status_code=500
            )
