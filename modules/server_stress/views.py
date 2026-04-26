from flask import render_template, request, jsonify, current_app
from . import server_stress_bp
from .core.ssh_manager import SSHManager
from .core.stress_manager import StressManager
import uuid

def get_manager():
    return SSHManager.get_instance(current_app.root_path)

def get_stress_manager():
    sm = StressManager.get_instance()
    if not sm.ssh_manager:
        sm.set_ssh_manager(get_manager())
    return sm

@server_stress_bp.route('/')
def index():
    return render_template('server_stress_index.html')

@server_stress_bp.route('/api/servers', methods=['GET'])
def get_servers():
    manager = get_manager()
    return jsonify({'servers': manager.get_servers()})

@server_stress_bp.route('/api/servers', methods=['POST'])
def add_server():
    data = request.json or {}
    manager = get_manager()
    simulated = data.get('simulated') is True or data.get('ip') == 'sim://slave'

    if simulated:
        # 模拟从机：用于只有一台物理机时模拟一主一从、从机高负载场景
        new_server = {
            'id': str(uuid.uuid4()),
            'name': data.get('name', '模拟从机'),
            'ip': 'sim://slave',
            'port': 222,
            'username': 'sim',
            'password': '',
            'simulated': True,
            'role': 'slave',
        }
    else:
        if not data.get('ip') or not data.get('username'):
            return jsonify({'success': False, 'message': 'Missing required fields (IP, Username)'}), 400
        try:
            port = int(data.get('port', 222))
        except (ValueError, TypeError):
            port = 222
            
        new_server = {
            'id': str(uuid.uuid4()),
            'name': data.get('name', data.get('ip')),
            'ip': data.get('ip'),
            'port': port,
            'username': data.get('username'),
            'password': data.get('password'),  # Note: storing plaintext for MVP
        }
    manager.add_server(new_server)
    out = {**new_server, 'password': '***'}
    return jsonify({'success': True, 'server': out})

@server_stress_bp.route('/api/servers/<server_id>', methods=['DELETE'])
def delete_server(server_id):
    manager = get_manager()
    manager.remove_server(server_id)
    return jsonify({'success': True})

@server_stress_bp.route('/api/servers/<server_id>', methods=['PUT'])
def update_server(server_id):
    data = request.json or {}
    manager = get_manager()
    server = manager.get_server_for_use(server_id)
    if not server:
        return jsonify({'success': False, 'message': '服务器不存在'}), 404
    if manager.is_simulated_slave(server):
        return jsonify({'success': False, 'message': '模拟从机不可编辑'}), 400
    updates = {}
    if 'name' in data:
        updates['name'] = data.get('name')
    if 'ip' in data:
        updates['ip'] = data.get('ip')
    if 'port' in data:
        try:
            updates['port'] = int(data.get('port', 222))
        except (ValueError, TypeError):
            updates['port'] = 222
    if 'username' in data:
        updates['username'] = data.get('username')
    if 'password' in data:
        updates['password'] = data.get('password')
    if manager.update_server(server_id, updates):
        out = manager.get_server_for_use(server_id)
        return jsonify({'success': True, 'server': {**out, 'password': '***'}})
    return jsonify({'success': False, 'message': '更新失败'}), 500

@server_stress_bp.route('/api/test_connection', methods=['POST'])
def test_connection():
    data = request.json or {}
    server_id = data.get('server_id')
    manager = get_manager()
    if not server_id:
        return jsonify({'success': False, 'message': '缺少 server_id'}), 400
    server = manager.get_server_for_use(server_id)
    if not server:
        return jsonify({'success': False, 'message': '服务器不存在'}), 404
    success, message, details = manager.test_connection(server)
    return jsonify({'success': success, 'message': message, 'details': details})

# --- Stress Management APIs ---

@server_stress_bp.route('/api/stress/check_tool', methods=['POST'])
def check_tool():
    data = request.json
    server_id = data.get('server_id')
    sm = get_stress_manager()
    installed, msg = sm.check_tool_installed(server_id)
    return jsonify({'installed': installed, 'message': msg})

@server_stress_bp.route('/api/stress/install_tool', methods=['POST'])
def install_tool():
    data = request.json
    server_id = data.get('server_id')
    sm = get_stress_manager()
    success, msg = sm.install_tool(server_id)
    return jsonify({'success': success, 'message': msg})

@server_stress_bp.route('/api/stress/start', methods=['POST'])
def start_stress():
    data = request.json
    server_id = data.get('server_id')
    # Default to 0 (all cores), 80% load, 60s
    cpu_cores = int(data.get('cpu_cores', 0))
    cpu_load = int(data.get('cpu_load', 80))
    timeout = int(data.get('timeout', 60))
    
    # New params
    vm_workers = int(data.get('vm_workers', 0))
    vm_bytes = data.get('vm_bytes', '256M')
    io_workers = int(data.get('io_workers', 0))
    
    sm = get_stress_manager()
    success, msg = sm.start_stress(server_id, cpu_cores, cpu_load, timeout, vm_workers, vm_bytes, io_workers)
    return jsonify({'success': success, 'message': msg})

@server_stress_bp.route('/api/stress/stop', methods=['POST'])
def stop_stress():
    data = request.json
    server_id = data.get('server_id')
    sm = get_stress_manager()
    success, msg = sm.stop_stress(server_id)
    return jsonify({'success': success, 'message': msg})

@server_stress_bp.route('/api/stress/active_jobs', methods=['GET'])
def get_active_stress_jobs():
    """获取当前运行中的压测任务，用于页面切换后恢复"""
    sm = get_stress_manager()
    jobs = sm.get_active_jobs()
    return jsonify({'active_jobs': jobs})


@server_stress_bp.route('/api/monitor/stats', methods=['GET'])
def get_monitor_stats():
    server_id = request.args.get('server_id')
    if not server_id:
        return jsonify({'success': False, 'message': 'Missing server_id'}), 400
        
    sm = get_stress_manager()
    stats = sm.get_system_stats(server_id)
    
    # Check for alerts (OOM)
    alerts = []
    oom_events = sm.check_oom_events(server_id)
    if oom_events:
        for event in oom_events:
            alerts.append({
                'type': 'OOM',
                'severity': 'P0',
                'message': f"检测到 OOM 内存溢出事件: {event}",
                'timestamp': '刚刚' 
            })
    
    # Simulate alert for demo if param present
    if request.args.get('simulate_alert'):
        import datetime
        alerts.append({
            'type': 'OOM',
            'severity': 'P0',
            'message': f"内存不足进程被杀死 (当前值: 进程 stress-ng (PID 12345) 因系统内存不足被内核 OOM 杀死...)",
            'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    if stats:
        return jsonify({'success': True, 'stats': stats, 'alerts': alerts})
    else:
        # Even if stats fail, we might want to return alerts if any? 
        # But usually stats fail means connection fail.
        return jsonify({'success': False, 'message': 'Failed to fetch stats'}), 500
