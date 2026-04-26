"""
组合测试报告生成
"""
import os
import json
import logging
from datetime import datetime

from utils.report_paths import get_module_report_dir

logger = logging.getLogger(__name__)


def save_report(pipeline_id: str, pipeline_type: str, result: dict, logs: list, config: dict) -> str:
    """
    保存组合测试报告
    :return: 报告文件路径
    """
    report_dir = get_module_report_dir('combined_test')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    json_path = os.path.join(report_dir, f"report_{pipeline_id}_{ts}.json")
    html_path = os.path.join(report_dir, f"report_{pipeline_id}_{ts}.html")

    report_data = {
        'pipeline_id': pipeline_id,
        'pipeline_type': pipeline_type,
        'timestamp': ts,
        'start_ts': config.get('start_ts', 0),
        'end_ts': int(datetime.now().timestamp()),
        'success': result.get('success', False),
        'steps_done': result.get('steps_done', []),
        'steps_failed': result.get('steps_failed', []),
        'message': result.get('message', ''),
        'config': {
            'devices_count': len(config.get('devices', [])),
            'reboot_duration_minutes': config.get('reboot_duration_minutes', 5),
            'player_stress_duration': config.get('player_stress_duration', 30),
        },
        'logs': logs[-100:],
    }

    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception('保存 JSON 报告失败: %s', e)
        return ''

    try:
        _write_html_report(html_path, report_data)
    except Exception as e:
        logger.warning('生成 HTML 报告失败: %s', e)

    return json_path


def _write_html_report(path: str, data: dict):
    """生成 HTML 报告"""
    success = data.get('success', False)
    status_class = 'success' if success else 'danger'
    status_text = '通过' if success else '未通过'

    logs_html = ''.join(
        f'<div class="log-line"><span class="text-muted">[{datetime.fromtimestamp(x.get("ts", 0)).strftime("%H:%M:%S")}]</span> {x.get("msg", "").replace("<", "&lt;").replace(">", "&gt;")}</div>'
        for x in data.get('logs', [])
    )

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>组合测试报告 - {data.get("pipeline_id", "")}</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container py-4">
    <h2 class="mb-4">组合测试报告</h2>
    <div class="card mb-4">
        <div class="card-body">
            <div class="row mb-3">
                <div class="col-md-6"><strong>流水线ID:</strong> {data.get("pipeline_id", "-")}</div>
                <div class="col-md-6"><strong>流水线类型:</strong> {data.get("pipeline_type", "-")}</div>
            </div>
            <div class="row mb-3">
                <div class="col-md-6"><strong>执行时间:</strong> {datetime.fromtimestamp(data.get("start_ts", 0)).strftime("%Y-%m-%d %H:%M:%S")}</div>
                <div class="col-md-6"><strong>耗时:</strong> {data.get("end_ts", 0) - data.get("start_ts", 0)} 秒</div>
            </div>
            <div class="row mb-3">
                <div class="col-12">
                    <span class="badge bg-{status_class} fs-6">{status_text}</span>
                    <span class="ms-2">{data.get("message", "")}</span>
                </div>
            </div>
            <div class="row">
                <div class="col-md-6"><strong>已完成步骤:</strong> {", ".join(data.get("steps_done", [])) or "-"}</div>
                <div class="col-md-6"><strong>失败步骤:</strong> {", ".join(data.get("steps_failed", [])) or "-"}</div>
            </div>
        </div>
    </div>
    <div class="card">
        <div class="card-header"><strong>运行日志</strong></div>
        <div class="card-body" style="max-height:400px;overflow:auto;font-family:monospace;font-size:13px;">
            {logs_html or "<div class='text-muted'>无日志</div>"}
        </div>
    </div>
    <div class="mt-4 text-muted small">
        <p>子模块报告：</p>
        <ul>
            <li>Reboot 报告：<a href="/reboot/reports">中控重启 → 查看历史报告</a></li>
            <li>播放压测报告：<a href="/player_stress/">播放器压测 → 历史报告</a></li>
        </ul>
    </div>
</div>
</body>
</html>'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
