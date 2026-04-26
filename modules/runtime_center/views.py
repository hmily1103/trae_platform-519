from flask import render_template, jsonify, request
from core.runtime import get_runtime_manager, RuntimeStatus
from . import runtime_center_bp
import time

@runtime_center_bp.route('/')
def index():
    return render_template('runtime_center_index.html')

@runtime_center_bp.route('/api/devices')
def list_devices():
    """设备列表及占用状态"""
    try:
        from core.device import get_device_manager
        dm = get_device_manager()
        devices = dm.get_devices()
        locks = []
        for d in devices:
            owner = dm.get_device_owner(d)
            locks.append({"device_id": d, "locked": bool(owner), "runtime_id": owner})
        return jsonify({"devices": devices, "locks": locks})
    except Exception as e:
        return jsonify({"devices": [], "locks": [], "error": str(e)})

@runtime_center_bp.route('/api/list')
def list_runtimes():
    manager = get_runtime_manager()
    module_filter = request.args.get('module')
    status_filter = request.args.get('status')
    limit = request.args.get('limit', type=int, default=100)

    if status_filter:
        try:
            status_filter = RuntimeStatus(status_filter)
        except ValueError:
            status_filter = None

    runtimes = manager.list_runtimes(module=module_filter, status=status_filter, limit=limit)
    return jsonify([r.to_dict() for r in runtimes])

@runtime_center_bp.route('/api/create_test_runtime', methods=['POST'])
def create_test_runtime():
    """Helper to verify the system is working by creating a fake runtime."""
    manager = get_runtime_manager()
    data = request.json or {}
    name = data.get('name', 'Test Runtime')
    module = data.get('module', 'test_module')
    
    runtime = manager.create_runtime(name=name, module=module, context={'demo': True})
    
    # Simulate some lifecycle
    manager.update_status(runtime.runtime_id, RuntimeStatus.RUNNING)
    
    return jsonify(runtime.to_dict())
