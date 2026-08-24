# 点歌与搜索：从 JMeter 计划抽出的接口，在 Web 端提供搜歌 + 点歌
# 敏感配置（host/roominfo/userid/appid）从环境变量读取，不暴露给前端
from flask import render_template, request, jsonify
import requests

from . import song_order_bp
from .history_store import add_order, list_history


def _precision_context(data):
    data = data or {}
    return {
        "analysis_id": data.get("precision_analysis_id") or request.args.get("precision_analysis_id"),
        "test_point_id": data.get("precision_test_point_id") or request.args.get("precision_test_point_id"),
        "execution_id": data.get("precision_execution_id") or request.args.get("precision_execution_id"),
    }


def _get_config():
    """从环境变量读取点歌配置，脱敏"""
    from utils.config_loader import get_song_order_config
    return get_song_order_config()


@song_order_bp.route('/')
def index():
    return render_template('song_order_index.html')


@song_order_bp.route('/api/history', methods=['GET'])
def api_history():
    """获取点歌历史"""
    limit = request.args.get('limit', type=int, default=50)
    entries = list_history(limit=limit)
    return jsonify({"ok": True, "data": {"entries": entries}})


@song_order_bp.route('/api/config', methods=['GET'])
def api_config():
    """获取点歌默认配置（仅 host/port，不含 roominfo/userid/appid）"""
    cfg = _get_config()
    return jsonify({
        'ok': True,
        'data': {
            'host': cfg['host'],
            'search_port': cfg['search_port'],
            'vod_port': cfg['vod_port'],
        },
    })


@song_order_bp.route('/api/search', methods=['POST'])
def api_search():
    """代理：搜索接口 POST /media/newsearchinfo?page=1&size=1"""
    cfg = _get_config()
    data = request.get_json() or {}
    host = (data.get('host') or cfg['host']).strip()
    port = int(data.get('search_port') or cfg['search_port'])
    content = (data.get('content') or '').strip()
    searchtype = int(data.get('searchtype') if data.get('searchtype') is not None else 1)

    url = f"http://{host}:{port}/media/newsearchinfo?page=1&size=1"
    payload = {"content": content, "searchtype": searchtype}
    try:
        r = requests.post(url, json=payload, timeout=10, headers={"Content-Type": "application/json"})
        return jsonify({
            "success": r.status_code == 200,
            "status_code": r.status_code,
            "data": r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:2000]},
        })
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "message": str(e)}), 500


@song_order_bp.route('/api/order', methods=['POST'])
def api_order():
    """代理：点歌接口 POST /song/vod，敏感配置从后端读取"""
    cfg = _get_config()
    data = request.get_json() or {}
    host = (data.get('host') or cfg['host']).strip()
    port = int(data.get('vod_port') or cfg['vod_port'])
    roominfo = (data.get('roominfo') or cfg['roominfo']).strip()
    userid = (data.get('userid') or cfg['userid']).strip()
    appid = (data.get('appid') or cfg['appid']).strip()
    musicno = (data.get('musicno') or '').strip()
    musicname = (data.get('musicname') or '').strip()

    if not musicno:
        return jsonify({"success": False, "message": "缺少歌曲编号 musicno"}), 400

    url = f"http://{host}:{port}/song/vod"
    payload = {
        "roominfo": roominfo,
        "musicinfo": [{"musicno": musicno, "musicname": musicname}],
        "parm": "{\"vip\":0}",
        "userid": userid,
        "appid": appid,
    }
    try:
        r = requests.post(url, json=payload, timeout=10, headers={"Content-Type": "application/json"})
        ok = r.status_code == 200
        resp_data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:2000]}
        if ok and resp_data and (resp_data.get("msg") == "点歌请求成功" or resp_data.get("code") == 200):
            add_order(musicno, musicname, success=True, precision_context=_precision_context(data))
        else:
            add_order(musicno, musicname, success=False, precision_context=_precision_context(data))
        return jsonify({
            "success": ok,
            "status_code": r.status_code,
            "data": resp_data,
        })
    except requests.exceptions.RequestException as e:
        add_order(musicno, musicname, success=False, precision_context=_precision_context(data))
        return jsonify({"success": False, "message": str(e)}), 500
