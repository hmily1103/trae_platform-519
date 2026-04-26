import os
import base64
import io
import time
from typing import List, Dict
import matplotlib
matplotlib.use('Agg')  # Force non-interactive backend
import matplotlib.pyplot as plt
from datetime import datetime
import json

class ReportGenerator:
    """
    V2 HTML 报告生成器
    包含:
    1. 基础测试信息
    2. 决策结论 (Pass/Fail/Grade)
    3. 性能趋势图 (Matplotlib -> Base64)
    4. 错误汇总
    """
    
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        # 设置中文字体，防止乱码 (尝试常见字体)
        plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'sans-serif'] 
        plt.rcParams['axes.unicode_minus'] = False

    def generate_report(self, summary_data: Dict, history_data: List[Dict], filename_prefix: str):
        """生成 HTML 报告"""
        
        # 1. 生成图表
        pss_chart_b64 = self._generate_pss_chart(history_data)
        cpu_chart_b64 = self._generate_cpu_chart(history_data)
        fps_chart_b64 = self._generate_fps_chart(history_data)
        
        # 2. 准备数据
        decision = summary_data.get("score_result", {})
        error_stats = summary_data.get("error_stats", {})
        
        html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>Android 播放器压测报告 (V2)</title>
    <style>
        :root {{ --bg: #f8fafc; --card: #fff; --text: #1e293b; --text-muted: #64748b; --border: #e2e8f0; --primary: #0ea5e9; --success: #22c55e; --danger: #ef4444; --warning: #f59e0b; }}
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; margin: 0; padding: 24px; background: var(--bg); color: var(--text); line-height: 1.6; font-size: 15px; }}
        .container {{ max-width: 1100px; margin: 0 auto; background: var(--card); padding: 32px; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        h1 {{ font-size: 1.75rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text); }}
        h2 {{ font-size: 1.25rem; font-weight: 600; margin: 0; }}
        h3 {{ font-size: 1.1rem; font-weight: 600; margin: 0 0 12px 0; color: var(--text); }}
        h4 {{ font-size: 0.95rem; font-weight: 600; margin: 0 0 8px 0; color: var(--text-muted); }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; padding-bottom: 20px; margin-bottom: 24px; border-bottom: 2px solid var(--border); }}
        .header-meta {{ font-size: 0.9rem; color: var(--text-muted); }}
        .result-pass {{ color: var(--success); font-weight: 600; }}
        .result-fail {{ color: var(--danger); font-weight: 600; }}
        .score-card {{ display: grid; grid-template-columns: auto 1fr 1fr; gap: 24px; align-items: center; padding: 24px; background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%); border-radius: 10px; margin-bottom: 24px; border: 1px solid #bae6fd; }}
        .score-main {{ text-align: center; padding-right: 24px; border-right: 1px solid var(--border); }}
        .grade {{ font-size: 2.5rem; font-weight: 800; color: var(--primary); line-height: 1.2; }}
        .score {{ font-size: 0.95rem; color: var(--text-muted); margin-top: 4px; }}
        .score-block {{ display: flex; flex-direction: column; gap: 6px; }}
        .score-block p {{ margin: 0; font-size: 0.9rem; }}
        .score-block strong {{ color: var(--text); }}
        .section {{ margin-bottom: 28px; }}
        .section-title {{ margin-bottom: 12px; padding-bottom: 6px; }}
        .chart-container {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; }}
        .chart-box {{ background: #fafafa; padding: 16px; border-radius: 8px; border: 1px solid var(--border); overflow: hidden; }}
        .chart-box img {{ display: block; max-width: 100%; height: auto; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
        th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border); }}
        th {{ background: #f1f5f9; font-weight: 600; color: var(--text); }}
        tr:hover {{ background: #f8fafc; }}
        .error-tag {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; color: white; }}
        .bg-red {{ background: var(--danger); }}
        .bg-orange {{ background: var(--warning); }}
        .bg-grey {{ background: var(--text-muted); }}
        .no-errors {{ color: var(--success); padding: 12px; background: #f0fdf4; border-radius: 6px; border: 1px solid #bbf7d0; }}
        .degradation {{ padding: 14px; background: #f8fafc; border-radius: 6px; border: 1px solid var(--border); font-size: 0.9rem; line-height: 1.7; }}
        .degradation strong {{ color: var(--text); }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>📺 Android 播放器压测报告</h1>
                <p class="header-meta">生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
            <div>
                <h2 class="{ 'result-pass' if decision.get('ready_to_release') else 'result-fail' }">
                    { "✅ 建议上线" if decision.get('ready_to_release') else "❌ 不建议上线" }
                </h2>
            </div>
        </div>

        <div class="score-card">
            <div class="score-main">
                <div class="grade">{decision.get('grade', 'N/A')}</div>
                <div class="score">稳定性评分 {decision.get('score', 0)}</div>
            </div>
            <div class="score-block">
                <p><strong>测试时长</strong> {summary_data.get('duration_str', 'N/A')}</p>
                <p><strong>播放总数</strong> {summary_data.get('song_count', 0)} 首</p>
                <p><strong>播放成功率</strong> {summary_data.get('success_rate', 0):.1f}%</p>
            </div>
            <div class="score-block">
                <p><strong>崩溃 (Crash)</strong> <span class="{ 'result-fail' if error_stats.get('crash_count', 0) > 0 else '' }">{error_stats.get('crash_count', 0)}</span></p>
                <p><strong>无响应 (ANR)</strong> <span class="{ 'result-fail' if error_stats.get('anr_count', 0) > 0 else '' }">{error_stats.get('anr_count', 0)}</span></p>
                <p><strong>PID 重启</strong> <span class="{ 'result-fail' if summary_data.get('restart_count', 0) > 0 else '' }">{summary_data.get('restart_count', 0)}</span></p>
            </div>
        </div>

        { self._render_deductions(decision.get('deductions', [])) }

        <div class="section">
            <h3 class="section-title">📈 性能趋势 (最近 1 小时)</h3>
            <div class="chart-container">
                <div class="chart-box">
                    <h4>内存 PSS (MB)</h4>
                    <img src="data:image/png;base64,{pss_chart_b64}" alt="PSS Chart"/>
                </div>
                <div class="chart-box">
                    <h4>CPU 占用率 (%)</h4>
                    <img src="data:image/png;base64,{cpu_chart_b64}" alt="CPU Chart"/>
                </div>
                { f'<div class="chart-box"><h4>视频 FPS</h4><img src="data:image/png;base64,{fps_chart_b64}" alt="FPS Chart"/></div>' if fps_chart_b64 else '' }
            </div>
        </div>

        <div class="section">
            <h3 class="section-title">📋 详细错误记录</h3>
            {self._render_error_table(summary_data)}
        </div>
        
        <div class="section">
            <h3 class="section-title">📉 内存退化分析</h3>
            <div class="degradation">{self._render_degradation(summary_data)}</div>
        </div>
    </div>
</body>
</html>
        """
        
        filename = f"report_{filename_prefix}.html"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        return filepath
    
    def generate_comparison(self, json_path_a: str, json_path_b: str, filename_prefix: str):
        with open(json_path_a, 'r', encoding='utf-8') as fa:
            data_a = json.load(fa)
        with open(json_path_b, 'r', encoding='utf-8') as fb:
            data_b = json.load(fb)
        meta_a = data_a.get('meta', {})
        meta_b = data_b.get('meta', {})
        decision_a = data_a.get('decision', {})
        decision_b = data_b.get('decision', {})
        stats_a = data_a.get('stats', {})
        stats_b = data_b.get('stats', {})
        counts_a = decision_a.get('counts', {})
        counts_b = decision_b.get('counts', {})
        def val(d, k, default=0):
            return d.get(k, default)
        rows = [
            ("包名", meta_a.get('package_name',''), meta_b.get('package_name','')),
            ("开始时间", meta_a.get('start_time',''), meta_b.get('start_time','')),
            ("结束时间", meta_a.get('end_time',''), meta_b.get('end_time','')),
            ("时长(秒)", str(meta_a.get('duration_sec',0)), str(meta_b.get('duration_sec',0))),
            ("评分", f"{decision_a.get('score',0)} ({decision_a.get('grade','')})", f"{decision_b.get('score',0)} ({decision_b.get('grade','')})"),
            ("建议", "建议上线" if decision_a.get('ready_to_release') else "不建议上线", "建议上线" if decision_b.get('ready_to_release') else "不建议上线"),
            ("平均PSS(MB)", str(stats_a.get('avg_pss_mb',0)), str(stats_b.get('avg_pss_mb',0))),
            ("峰值PSS(MB)", str(stats_a.get('max_pss_mb',0)), str(stats_b.get('max_pss_mb',0))),
            ("总丢帧", str(stats_a.get('total_gfx_jank',0)), str(stats_b.get('total_gfx_jank',0))),
            ("总渲染帧数", str(stats_a.get('total_frames_delta',0)), str(stats_b.get('total_frames_delta',0))),
            ("平均视频帧率(FPS)", str(stats_a.get('avg_video_fps','N/A')), str(stats_b.get('avg_video_fps','N/A'))),
            ("卡顿日志次数", str(stats_a.get('final_log_stutter_count',0)), str(stats_b.get('final_log_stutter_count',0))),
            ("UI丢帧率(GFX Jank)", str(stats_a.get('avg_jank_percent',0)), str(stats_b.get('avg_jank_percent',0))),
            ("重启次数", str(stats_a.get('restart_count',0)), str(stats_b.get('restart_count',0))),
            ("崩溃数", str(stats_a.get('crash_count',0)), str(stats_b.get('crash_count',0))),
            ("ANR数", str(stats_a.get('anr_count',0)), str(stats_b.get('anr_count',0))),
            ("屏幕异常", str(counts_a.get('screen_anomaly',0)), str(counts_b.get('screen_anomaly',0)))
        ]
        table_rows = "".join([f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>" for r in rows])
        html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>对比报告</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background-color: #f8f9fa; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Android 播放器压测对比报告</h1>
        <p>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <table>
            <thead>
                <tr>
                    <th>指标</th>
                    <th>A</th>
                    <th>B</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>
    </div>
</body>
</html>
        """
        filename = f"compare_{filename_prefix}.html"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        return filepath

    def _render_deductions(self, deductions):
        if not deductions:
            return ""
        
        items = "".join([f"<li style='margin: 6px 0;'>{d}</li>" for d in deductions])
        return f"""
        <div class="section" style="background: #fffbeb; padding: 16px; border-radius: 8px; border-left: 4px solid #f59e0b;">
            <h3 class="section-title" style="color: #b45309; margin-top: 0;">⚠️ 扣分/风险项</h3>
            <ul style="margin: 0; padding-left: 20px;">{items}</ul>
        </div>
        """

    def _render_error_table(self, summary):
        rows = ""
        # 1. Failed Sessions
        for item in summary.get('failed_sessions', []):
            rows += f"""
            <tr>
                <td><span class="error-tag bg-orange">播放失败</span></td>
                <td>{item.get('time')}</td>
                <td>{item.get('song')}</td>
                <td>{item.get('reason')}</td>
            </tr>
            """
        
        # 2. System Errors
        for evt in summary.get('error_events', []):
            bg = "bg-red" if evt['type'] in ['CRASH', 'ANR'] else "bg-orange"
            rows += f"""
            <tr>
                <td><span class="error-tag {bg}">{evt['type']}</span></td>
                <td>{evt['time']}</td>
                <td>System</td>
                <td>{evt['message']} <br/><small>{evt.get('log_file','')}</small></td>
            </tr>
            """
            
        # 3. PID Events
        for evt in summary.get('pid_events', []):
            if evt['type'] in ["PID_RESTART", "PID_LOST"]:
                rows += f"""
                <tr>
                    <td><span class="error-tag bg-red">PID重启</span></td>
                    <td>{evt['timestamp']}</td>
                    <td>Process</td>
                    <td>{evt['description']} (T+{evt.get('elapsed_min', 0)}m)</td>
                </tr>
                """

        if not rows:
            return "<p class=\"no-errors\">✅ 太棒了！本次测试未发现明显错误。</p>"
            
        return f"""
        <table>
            <thead>
                <tr>
                    <th style="width: 100px;">类型</th>
                    <th style="width: 180px;">时间</th>
                    <th style="width: 200px;">对象</th>
                    <th>详情</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
        """

    def _render_degradation(self, summary):
        deg = summary.get('degradation_analysis', {})
        if deg.get("status") == "insufficient_data":
            return "数据不足，无法分析退化趋势。"
        
        return f"""
        内存增长速率: <strong>{deg.get('mem_growth_rate_mb_per_hour', 0)} MB/h</strong><br/>
        CPU 变化幅度: {deg.get('cpu_change_percent', 0)}%<br/>
        (首段均值: {deg.get('first_avg_pss', 0)} MB -> 末段均值: {deg.get('last_avg_pss', 0)} MB)
        """

    def _generate_pss_chart(self, history: List[Dict]) -> str:
        """生成 PSS 曲线 Base64"""
        if not history: return ""
        
        # 只取最近 1 小时的数据 (假设 3600 个点，如果采样间隔 1s)
        # 或者全部数据，视需求而定。用户说 "最近 1 小时"，这里简单处理取最后 3600 点
        data = history[-3600:] if len(history) > 3600 else history
        
        timestamps = [x.get('timestamp') for x in data]
        start_ts = data[0].get('timestamp')
        start_sec = self._to_epoch_seconds(start_ts)
        x_axis = []
        for i, t in enumerate(timestamps):
            sec = self._to_epoch_seconds(t)
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
        y_axis = [x.get('pss_mb', 0) for x in data]
        
        plt.figure(figsize=(10, 4))
        plt.plot(x_axis, y_axis, color='#2196F3', linewidth=1.5)
        plt.title("Memory Usage (PSS)")
        plt.xlabel("Time (minutes)")
        plt.ylabel("MB")
        plt.grid(True, linestyle='--', alpha=0.5)
        
        # 标记重启点
        for i, pt in enumerate(data):
            if pt.get('is_restarted'):
                plt.axvline(x=x_axis[i], color='red', linestyle='--', alpha=0.8)
                plt.text(x_axis[i], max(y_axis)*0.9, 'Restart', color='red', fontsize=8)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    def _generate_cpu_chart(self, history: List[Dict]) -> str:
        """生成 CPU 曲线 Base64"""
        if not history: return ""
        data = history[-3600:] if len(history) > 3600 else history
        
        timestamps = [x.get('timestamp') for x in data]
        start_ts = data[0].get('timestamp')
        start_sec = self._to_epoch_seconds(start_ts)
        x_axis = []
        for i, t in enumerate(timestamps):
            sec = self._to_epoch_seconds(t)
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
        y_axis = [x.get('cpu_percent', 0) for x in data]
        
        plt.figure(figsize=(10, 4))
        plt.plot(x_axis, y_axis, color='#4CAF50', linewidth=1.0)
        plt.title("CPU Usage")
        plt.xlabel("Time (minutes)")
        plt.ylabel("%")
        plt.ylim(0, 100)
        plt.grid(True, linestyle='--', alpha=0.5)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    def _generate_fps_chart(self, history: List[Dict]) -> str:
        """生成视频 FPS 曲线 Base64（仅当有有效 FPS 数据时）"""
        if not history:
            return ""
        data = history[-3600:] if len(history) > 3600 else history
        fps_values = [x.get('video_fps', 0) for x in data]
        if not any(v > 0 for v in fps_values):
            return ""
        timestamps = [x.get('timestamp') for x in data]
        start_ts = data[0].get('timestamp')
        start_sec = self._to_epoch_seconds(start_ts)
        x_axis = []
        for i, t in enumerate(timestamps):
            sec = self._to_epoch_seconds(t)
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
        plt.figure(figsize=(10, 4))
        plt.plot(x_axis, fps_values, color='#8b5cf6', linewidth=1.5)
        plt.title("Video FPS")
        plt.xlabel("Time (minutes)")
        plt.ylabel("FPS")
        plt.ylim(0, max(max(fps_values) * 1.1, 30))
        plt.grid(True, linestyle='--', alpha=0.5)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    def _to_epoch_seconds(self, ts):
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                return dt.timestamp()
            except Exception:
                return None
        return None
