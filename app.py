from flask import Flask, render_template, jsonify, request
import os

app = Flask(__name__)

# 开发/内网部署常见问题：浏览器/代理缓存导致样式不更新
# 关闭静态文件缓存 + 开启模板自动重载，保证页面样式改动立即生效
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# 统一认证：ENABLE_API_AUTH=1 且 API_KEY 已设置时，/api/* 及模块 API 需携带 X-API-Key
ENABLE_API_AUTH = os.environ.get('ENABLE_API_AUTH', '').lower() in ('1', 'true', 'yes')
API_KEY = os.environ.get('API_KEY', '').strip()

# API 限流：Flask-Limiter
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=["300 per minute", "5000 per hour"],
        storage_uri="memory://",
    )
    RATELIMIT_ENABLED = True
except Exception:
    limiter = None
    RATELIMIT_ENABLED = False

# 确保日志目录存在
log_dir = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(log_dir, exist_ok=True)

# 模块加载失败记录（供仪表盘展示）
MODULE_LOAD_FAILURES = {}

# 延迟导入 logger，避免循环依赖
try:
    from utils.logger import platform_logger
except Exception:
    import logging
    platform_logger = logging.getLogger('platform')
    platform_logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    platform_logger.addHandler(handler)

try:
    from modules.clean_ad.views import clean_ad_bp
    app.register_blueprint(clean_ad_bp, url_prefix='/clean_ad')
except Exception as e:
    MODULE_LOAD_FAILURES['clean_ad'] = str(e)
    platform_logger.error(f'Error loading CleanAd module: {e}', exc_info=True)

try:
    from modules.sanfang.views import sanfang_bp
    app.register_blueprint(sanfang_bp, url_prefix='/sanfang')
except ImportError as e:
    MODULE_LOAD_FAILURES['sanfang'] = str(e)
    platform_logger.warning(f"Sanfang module not loaded yet: {e}")

try:
    from modules.reboot.views import reboot_bp
    app.register_blueprint(reboot_bp, url_prefix='/reboot')
except Exception as e:
    MODULE_LOAD_FAILURES['reboot'] = str(e)
    platform_logger.error(f'Error loading Reboot module: {e}', exc_info=True)

try:
    from modules.monkey.views import monkey_bp
    app.register_blueprint(monkey_bp, url_prefix='/monkey')
except Exception as e:
    MODULE_LOAD_FAILURES['monkey'] = str(e)
    platform_logger.error(f'Error loading Monkey module: {e}', exc_info=True)

try:
    from modules.player_stress.views import stress_bp
    app.register_blueprint(stress_bp, url_prefix='/player_stress')
except Exception as e:
    MODULE_LOAD_FAILURES['player_stress'] = str(e)
    platform_logger.error(f'Error loading PlayerStress module: {e}', exc_info=True)

try:
    from modules.log_monitor.views import log_monitor_bp
    app.register_blueprint(log_monitor_bp, url_prefix='/log_monitor')
except Exception as e:
    MODULE_LOAD_FAILURES['log_monitor'] = str(e)
    platform_logger.error(f'Error loading LogMonitor module: {e}', exc_info=True)

try:
    from modules.test_case.views import test_case_bp
    app.register_blueprint(test_case_bp)
except Exception as e:
    MODULE_LOAD_FAILURES['test_case'] = str(e)
    platform_logger.error(f'Error loading TestCase module: {e}', exc_info=True)

try:
    from modules.prd_audit.views import prd_audit_bp
    app.register_blueprint(prd_audit_bp)
    platform_logger.info('PRD Audit (standalone) module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['prd_audit'] = str(e)
    platform_logger.error(f'Error loading PRD Audit module: {e}', exc_info=True)

try:
    from modules.performance_monitor.views import performance_monitor_bp
    app.register_blueprint(performance_monitor_bp, url_prefix='/performance_monitor')
except Exception as e:
    MODULE_LOAD_FAILURES['performance_monitor'] = str(e)
    platform_logger.error(f'Error loading PerformanceMonitor module: {e}', exc_info=True)

try:
    from modules.ui_automation.views import ui_automation_bp
    app.register_blueprint(ui_automation_bp, url_prefix='/ui_automation')
    platform_logger.info('UIAutomation module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['ui_automation'] = str(e)
    platform_logger.error(f'Error loading UIAutomation module: {e}', exc_info=True)
    import traceback
    traceback.print_exc()

try:
    from modules.remote_control.views import remote_control_bp
    app.register_blueprint(remote_control_bp, url_prefix='/remote_control')
    platform_logger.info('RemoteControl module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['remote_control'] = str(e)
    platform_logger.error(f'Error loading RemoteControl module: {e}', exc_info=True)

try:
    from modules.unified.views import unified_bp
    app.register_blueprint(unified_bp, url_prefix='/unified')
    platform_logger.info('Unified module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['unified'] = str(e)
    platform_logger.warning(f"Unified module not loaded yet: {e}")

try:
    from modules.runtime_center.views import runtime_center_bp
    app.register_blueprint(runtime_center_bp, url_prefix='/runtime_center')
    platform_logger.info('RuntimeCenter module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['runtime_center'] = str(e)
    platform_logger.error(f"Error loading RuntimeCenter module: {e}", exc_info=True)

try:
    from modules.combined_test.views import combined_test_bp
    app.register_blueprint(combined_test_bp, url_prefix='/combined_test')
    platform_logger.info('CombinedTest module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['combined_test'] = str(e)
    platform_logger.error(f'Error loading CombinedTest module: {e}', exc_info=True)

try:
    from modules.server_stress.views import server_stress_bp
    app.register_blueprint(server_stress_bp, url_prefix='/server_stress')
    platform_logger.info('ServerStress module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['server_stress'] = str(e)
    platform_logger.error(f'Error loading ServerStress module: {e}', exc_info=True)

try:
    from modules.song_order.views import song_order_bp
    app.register_blueprint(song_order_bp, url_prefix='/song_order')
    platform_logger.info('SongOrder module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['song_order'] = str(e)
    platform_logger.error(f'Error loading SongOrder module: {e}', exc_info=True)

try:
    from modules.api_stress.views import api_stress_bp
    app.register_blueprint(api_stress_bp, url_prefix='/api_stress')
    platform_logger.info('ApiStress module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['api_stress'] = str(e)
    platform_logger.error(f'Error loading ApiStress module: {e}', exc_info=True)

try:
    from modules.stb_calculator.views import stb_calculator_bp
    app.register_blueprint(stb_calculator_bp, url_prefix='/stb_calculator')
    platform_logger.info('STBCalculator module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['stb_calculator'] = str(e)
    platform_logger.error(f'Error loading STBCalculator module: {e}', exc_info=True)

try:
    from modules.precision_test import precision_test_bp
    app.register_blueprint(precision_test_bp)
    platform_logger.info('PrecisionTest module loaded successfully')
except Exception as e:
    MODULE_LOAD_FAILURES['precision_test'] = str(e)
    platform_logger.error(f'Error loading PrecisionTest module: {e}', exc_info=True)

# ModuleLoader：注册各模块到统一插件加载器
try:
    from shared.core.module_registry import register_all_modules
    register_all_modules(app)
    platform_logger.info('ModuleLoader: modules registered')
except Exception as e:
    platform_logger.warning(f'ModuleLoader registration skipped: {e}')


# --- 统一认证 (可选) ---
@app.before_request
def check_api_auth():
    """当 ENABLE_API_AUTH=1 且 API_KEY 已设置时，校验 X-API-Key"""
    if not ENABLE_API_AUTH or not API_KEY:
        return None
    path = request.path
    # 豁免：健康检查、文档、静态资源、首页
    if path in ('/api/health', '/docs', '/api/openapi.json', '/') or path.startswith('/static/'):
        return None
    # 仅对 API 路径校验
    if path.startswith('/api/') or '/api/' in path:
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if key != API_KEY:
            return jsonify({'ok': False, 'message': 'API 认证失败', 'error': 'unauthorized'}), 401
    return None


# --- 仪表盘 KPI 与健康检查 ---

@app.route('/api/announcements', methods=['GET'])
def api_announcements():
    """系统公告：从 config/announcements.json 读取，可运营修改"""
    try:
        from utils.config_loader import get_announcements
        return jsonify({'ok': True, 'data': get_announcements()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/dashboard/stats', methods=['GET'])
def api_dashboard_stats():
    """仪表盘 KPI：在线设备、今日任务、异常警告、工具模块数、加载失败模块"""
    from datetime import datetime, date
    stats = {'devices': 0, 'tasks_today': 0, 'alerts': 0, 'modules': 0, 'failed_modules': MODULE_LOAD_FAILURES}
    try:
        from core.device import get_device_manager
        stats['devices'] = len(get_device_manager().get_devices())
    except Exception:
        pass
    try:
        from shared.unified.report_store import get_unified_report_store
        store = get_unified_report_store()
        reports = store.list_reports(limit=500)
        today = date.today()
        for r in reports:
            ft = r.get('finished_at')
            if ft is None:
                continue
            try:
                if isinstance(ft, (int, float)):
                    dt = datetime.fromtimestamp(ft).date()
                else:
                    s = str(ft)
                    dt = datetime.fromisoformat(s.replace('Z', '+00:00')).date() if 'Z' in s or '+' in s else datetime.fromisoformat(s).date()
                if dt == today:
                    stats['tasks_today'] += 1
            except Exception:
                pass
    except Exception:
        pass
    # 异常警告：暂不聚合（需 task_id），保留 0
    try:
        feature_bps = {'clean_ad', 'sanfang', 'reboot', 'monkey', 'player_stress',
                       'log_monitor', 'performance_monitor', 'ui_automation', 'unified',
                       'server_stress', 'api_stress', 'song_order', 'runtime_center', 'test_case', 'remote_control',
                       'combined_test', 'stb_calculator'}
        stats['modules'] = sum(1 for bp in app.blueprints if bp in feature_bps)
    except Exception:
        stats['modules'] = 14
    return jsonify({'ok': True, 'success': True, 'data': stats})


@app.route('/api/modules/status', methods=['GET'])
def api_modules_status():
    """各模块运行状态（供 Dashboard 模块状态卡片使用）"""
    try:
        from shared.core.module_loader import get_module_loader
        loader = get_module_loader()
        status = loader.get_all_status()
        return jsonify({'ok': True, 'modules': status})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stream/recent', methods=['GET'])
def api_stream_recent():
    """Stream Bus：获取最近事件"""
    try:
        from shared.core.stream_bus import get_stream_bus
        limit = int(request.args.get('limit', 50))
        types = request.args.get('types')
        stream_types = set(types.split(',')) if types else None
        bus = get_stream_bus()
        events = bus.get_recent(limit=limit, stream_types=stream_types)
        return jsonify({'ok': True, 'events': events})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/runs/active', methods=['GET'])
def api_runs_active():
    """运行中的一键任务列表，用于前端展示当前在跑的任务"""
    try:
        from shared.unified.orchestrator import list_runs
        limit = int(request.args.get('limit', 20))
        runs = list_runs(limit=limit, status='running')
        return jsonify({'ok': True, 'runs': runs})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/devices/usage', methods=['GET'])
def api_devices_usage():
    """当前被占用的设备列表（device_id, runtime_id, acquired_at），用于展示谁在占用"""
    try:
        from core.device import get_device_manager
        usage = get_device_manager().get_device_usage()
        return jsonify({'ok': True, 'usage': usage})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _exempt_if_limiter(f):
    return limiter.exempt(f) if limiter else f

@app.route('/api/health', methods=['GET'])
@_exempt_if_limiter
def api_health():
    """健康检查：平台状态、模块加载、各模块 heartbeat、ADB 可用性"""
    from datetime import datetime
    health = {
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'platform': 'Trae',
        'modules': {},
        'module_status': {},
        'adb_available': False,
    }
    for name in app.blueprints:
        if name != 'static':
            health['modules'][name] = 'loaded'
    health['failed_modules'] = MODULE_LOAD_FAILURES
    try:
        from shared.core.module_loader import get_module_loader
        loader = get_module_loader()
        health['module_status'] = loader.get_all_status()
    except Exception as e:
        health['module_status_error'] = str(e)
    try:
        from core.device import get_device_manager
        devs = get_device_manager().get_devices()
        health['adb_available'] = True
        health['adb_devices'] = len(devs)
    except Exception as e:
        health['adb_available'] = False
        health['adb_error'] = str(e)
    return jsonify(health)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/docs')
@_exempt_if_limiter
def api_docs():
    """API 文档 (Swagger UI)"""
    return render_template('docs.html')


@app.route('/api/openapi.json')
@_exempt_if_limiter
def api_openapi():
    """OpenAPI 3.0 规范 (JSON)"""
    import yaml
    spec_path = os.path.join(os.path.dirname(__file__), 'docs', 'openapi.yaml')
    if os.path.exists(spec_path):
        with open(spec_path, 'r', encoding='utf-8') as f:
            spec = yaml.safe_load(f)
        # 动态设置 servers[0].url 为当前请求的 base
        if spec.get('servers') and request.host_url:
            base = request.host_url.rstrip('/')
            spec['servers'] = [{'url': base, 'description': '当前服务'}]
        return jsonify(spec)
    return jsonify({'openapi': '3.0.3', 'info': {'title': 'Trae Platform API'}, 'paths': {}}), 200


# --- 统一错误处理 ---
@app.errorhandler(404)
def not_found(e):
    # API 请求返回 404 时给出更明确提示
    if request.path.startswith('/monkey/api/') or (request.accept_mimetypes.best and 'application/json' in str(request.accept_mimetypes)):
        return jsonify({'ok': False, 'message': '请求的接口或数据不存在（请检查地址或刷新页面）', 'error': 'not_found'}), 404
    return jsonify({'ok': False, 'message': '资源不存在', 'error': 'not_found'}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({'ok': False, 'message': '服务器内部错误', 'error': 'internal_error'}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    if hasattr(e, 'code') and e.code == 404:
        return jsonify({'ok': False, 'message': '资源不存在'}), 404
    platform_logger.error(f'Unhandled exception: {e}', exc_info=True)
    return jsonify({'ok': False, 'message': str(e), 'error': 'exception'}), 500


if __name__ == '__main__':
    port = 5000
    print("=" * 60)
    print("Trae Platform 正在启动...")
    print("=" * 60)
    print(f"访问地址: http://127.0.0.1:{port}")
    print(f"访问地址: http://localhost:{port}")
    print("=" * 60)
    print("按 Ctrl+C 停止服务器\n")
    try:
        print("Starting app.run()...", flush=True)
        # Force host to 127.0.0.1 to avoid binding issues with 0.0.0.0
        # Try port 5001 if 5000 fails? No, user insisted on 5000.
        # Check if socket is usable first?
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                print(f"Warning: Port {port} is already in use!", flush=True)
            else:
                print(f"Port {port} seems free.", flush=True)
        
        # from waitress import serve
        # print(f"Starting Waitress server on 127.0.0.1:{port}...", flush=True)
        # serve(app, host='127.0.0.1', port=port)
        # print("Waitress serve() returned.", flush=True)

        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
        print("app.run() returned.", flush=True)
    except KeyboardInterrupt:
        print("\n\n服务器已停止 (KeyboardInterrupt)", flush=True)
    except BaseException as e:
        print(f"\n\n启动失败 (BaseException): {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        print("End of __main__ block.", flush=True)
