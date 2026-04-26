from typing import List, Dict, Any
from collections import defaultdict
import statistics
from ..models import ExecutionTrace

class TraceAnalyzer:
    """
    Trace数据分析器
    负责计算稳定性指标、检测UI漂移等
    """
    
    def analyze_stability(self, traces: List[ExecutionTrace]) -> Dict[str, Any]:
        """
        分析自动化稳定性
        
        :param traces: 所有相关执行的Trace列表 (跨设备/跨Run)
        :return: 稳定性报告
        """
        # 按步骤分组
        steps_data = defaultdict(list)
        for trace in traces:
            steps_data[trace.step_num].append(trace)
            
        report = {
            'total_steps': len(steps_data),
            'step_metrics': {},
            'unstable_steps': []
        }
        
        for step_num, step_traces in steps_data.items():
            # 1. Step 稳定性 (成功率)
            total_execs = len(step_traces)
            success_execs = len([t for t in step_traces if t.success])
            success_rate = success_execs / total_execs if total_execs > 0 else 0
            
            # 2. Selector 稳定性 (主策略使用率)
            # 只有成功的执行才统计Selector
            successful_traces = [t for t in step_traces if t.success]
            if successful_traces:
                primary_strategy_usage = len([t for t in successful_traces if t.fallback_index == -1])
                selector_stability = primary_strategy_usage / len(successful_traces)
                
                # 3. 平均 Fallback 深度
                # -1 (primary) -> depth 0
                # 0 (1st fallback) -> depth 1
                fallback_depths = []
                for t in successful_traces:
                    if t.fallback_index == -1:
                        fallback_depths.append(0)
                    else:
                        fallback_depths.append(t.fallback_index + 1)
                
                avg_fallback_depth = statistics.mean(fallback_depths) if fallback_depths else 0
            else:
                selector_stability = 0.0
                avg_fallback_depth = 0.0
                
            metrics = {
                'step_num': step_num,
                'success_rate': success_rate,
                'selector_stability': selector_stability,
                'avg_fallback_depth': avg_fallback_depth,
                'total_execs': total_execs,
                'success_execs': success_execs,
                'device_failures': list(set([t.device_id for t in step_traces if not t.success]))
            }
            
            report['step_metrics'][step_num] = metrics
            
            # 判定为不稳定步骤
            # 成功率 < 100% 或 选择器稳定性 < 80% (频繁fallback)
            if success_rate < 1.0 or selector_stability < 0.8:
                report['unstable_steps'].append(metrics)
                
        # 排序不稳定步骤 (按成功率低 -> 高，然后按fallback深度高 -> 低)
        report['unstable_steps'].sort(key=lambda x: (x['success_rate'], -x['avg_fallback_depth']))
        
        return report

    def detect_ui_drift(self, traces: List[ExecutionTrace]) -> Dict[str, Any]:
        """
        检测UI漂移 (Bounds差异)
        """
        # 按步骤分组
        steps_data = defaultdict(list)
        for trace in traces:
            if trace.success and trace.bounds:
                steps_data[trace.step_num].append(trace)
        
        drift_report = {}
        
        for step_num, step_traces in steps_data.items():
            if len(step_traces) < 2:
                continue
                
            centers = []
            for t in step_traces:
                b = t.bounds
                # uiautomator2 bounds: {'left': x, 'top': y, 'right': x, 'bottom': y}
                try:
                    cx = (b.get('left', 0) + b.get('right', 0)) / 2
                    cy = (b.get('top', 0) + b.get('bottom', 0)) / 2
                    centers.append({
                        'device': t.device_id, 
                        'x': cx, 
                        'y': cy, 
                        'w': b.get('right', 0) - b.get('left', 0), 
                        'h': b.get('bottom', 0) - b.get('top', 0)
                    })
                except:
                    continue
            
            if not centers:
                continue

            # 简单比较：计算最大距离
            max_dist = 0
            for i in range(len(centers)):
                for j in range(i + 1, len(centers)):
                    c1 = centers[i]
                    c2 = centers[j]
                    dist = ((c1['x'] - c2['x'])**2 + (c1['y'] - c2['y'])**2)**0.5
                    if dist > max_dist:
                        max_dist = dist
            
            # 阈值：50像素 (假设同分辨率或差异显著)
            if max_dist > 50: 
                drift_report[step_num] = {
                    'max_drift': max_dist,
                    'details': centers
                }
                
        return drift_report

    def analyze_device_diff(self, traces: List[ExecutionTrace]) -> Dict[str, Any]:
        """
        分析设备差异
        """
        # 按设备分组
        device_data = defaultdict(list)
        for trace in traces:
            device_data[trace.device_id].append(trace)
            
        report = {
            'device_summary': {},
            'problematic_devices': [],
            'device_specific_failures': []
        }
        
        # 1. 设备总体概况
        avg_success_rate = 0
        if device_data:
            total_rates = 0
            for device_id, device_traces in device_data.items():
                total = len(device_traces)
                success = len([t for t in device_traces if t.success])
                rate = success / total if total > 0 else 0
                
                # 计算平均耗时 (只算成功的)
                durations = [t.duration_ms for t in device_traces if t.success and t.duration_ms > 0]
                avg_duration = statistics.mean(durations) if durations else 0
                
                report['device_summary'][device_id] = {
                    'total_steps': total,
                    'success_rate': rate,
                    'avg_duration': avg_duration
                }
                total_rates += rate
            
            avg_success_rate = total_rates / len(device_data)
            
            # 2. 识别问题设备 (成功率低于平均值 10% 以上)
            for device_id, summary in report['device_summary'].items():
                if summary['success_rate'] < (avg_success_rate - 0.1):
                    report['problematic_devices'].append(device_id)
                    
        # 3. 识别特定设备失败的步骤
        # 按步骤分组
        steps_data = defaultdict(lambda: defaultdict(list))
        for trace in traces:
            steps_data[trace.step_num][trace.device_id].append(trace)
            
        for step_num, devices_map in steps_data.items():
            failed_devices = []
            working_devices = []
            
            for device_id, step_traces in devices_map.items():
                # 该设备在该步骤是否全部失败
                if all(not t.success for t in step_traces):
                    failed_devices.append(device_id)
                else:
                    working_devices.append(device_id)
            
            # 如果有设备失败，且有设备成功，且失败设备不是全部设备
            if failed_devices and working_devices:
                report['device_specific_failures'].append({
                    'step_num': step_num,
                    'failed_devices': failed_devices,
                    'working_devices': working_devices
                })
                
        return report

    def generate_suggestions(self, traces: List[ExecutionTrace]) -> List[Dict[str, Any]]:
        """
        生成反向改进建议
        """
        suggestions = []
        
        # 按步骤分组
        steps_data = defaultdict(list)
        for trace in traces:
            steps_data[trace.step_num].append(trace)
            
        for step_num, step_traces in steps_data.items():
            successful_traces = [t for t in step_traces if t.success]
            total_execs = len(step_traces)
            
            if not successful_traces:
                # 建议1: 全军覆没，建议重新录制
                suggestions.append({
                    'step_num': step_num,
                    'type': 'critical_failure',
                    'reason': f'该步骤在所有 {len(step_traces)} 次执行中全部失败',
                    'suggestion': '建议重新录制该步骤，或检查页面是否发生重大变更'
                })
                continue
                
            # 建议2: 自动 Selector 重排序
            # 统计每个 fallback_index 的使用次数
            strategy_usage = defaultdict(int)
            for t in successful_traces:
                strategy_usage[t.fallback_index] += 1
                
            # 如果 primary (-1) 不是使用最多的
            most_used_index = max(strategy_usage.items(), key=lambda x: x[1])[0]
            if most_used_index != -1:
                usage_count = strategy_usage[most_used_index]
                usage_rate = usage_count / len(successful_traces)
                
                # 如果某个 fallback 使用率超过 50%
                if usage_rate > 0.5:
                    suggestions.append({
                        'step_num': step_num,
                        'type': 'reorder_selectors',
                        'reason': f'Fallback索引 {most_used_index} 被使用了 {usage_rate:.1%} 的次数 (Primary仅 {(strategy_usage[-1]/len(successful_traces)):.1%})',
                        'suggestion': f'建议将 Fallback {most_used_index} 提升为首选 Selector'
                    })
            
            # 建议3: 补充 Fallback
            success_rate = len(successful_traces) / total_execs
            if success_rate < 0.9:
                 failed_devices = list(set([t.device_id for t in step_traces if not t.success]))
                 suggestions.append({
                     'step_num': step_num,
                     'type': 'add_fallback',
                     'reason': f'该步骤成功率仅 {success_rate:.1%}，在以下设备上失败: {", ".join(failed_devices)}',
                     'suggestion': '建议针对失败设备补充录制新的 Selector (Add Fallback)'
                 })

        return suggestions
