"""
性能基线管理模块
支持保存性能基线、对比当前性能、生成对比报告
"""
import json
import os
from typing import Dict, Optional, List
from datetime import datetime
from .models import PerformanceSnapshot


class PerformanceBaseline:
    """性能基线管理器"""
    
    def __init__(self, baseline_file: str = "performance_baseline.json"):
        """
        初始化性能基线管理器
        
        :param baseline_file: 基线文件路径
        """
        self.baseline_file = baseline_file
        self.baseline_data: Dict = {}
        self.load()
    
    def save_baseline(self, name: str, snapshot: PerformanceSnapshot, description: str = ""):
        """
        保存性能基线
        
        :param name: 基线名称
        :param snapshot: 性能快照
        :param description: 基线描述
        """
        total_pss = sum(p.pss_kb for p in snapshot.processes) if snapshot.processes else snapshot.total_pss
        total_cpu = sum(p.cpu_usage for p in snapshot.processes) if snapshot.processes else snapshot.cpu_usage
        total_gc = sum(p.gc_count for p in snapshot.processes) if snapshot.processes else snapshot.gc_count
        
        self.baseline_data[name] = {
            'timestamp': snapshot.timestamp.isoformat(),
            'description': description,
            'total_pss_kb': total_pss,
            'total_cpu_percent': total_cpu,
            'total_gc_count': total_gc,
            'fps': snapshot.fps,
            'jank_count': snapshot.jank_count,
            'network_rx_kb': snapshot.network_rx_kb,
            'network_tx_kb': snapshot.network_tx_kb,
            'process_count': len(snapshot.processes) if snapshot.processes else 0,
            'processes': [
                {
                    'pid': p.pid,
                    'name': p.process_name,
                    'pss_kb': p.pss_kb,
                    'cpu_percent': p.cpu_usage,
                    'gc_count': p.gc_count
                }
                for p in (snapshot.processes or [])
            ]
        }
        self.save()
    
    def compare_with_baseline(self, name: str, current_snapshot: PerformanceSnapshot) -> Dict:
        """
        与基线对比
        
        :param name: 基线名称
        :param current_snapshot: 当前性能快照
        :return: 对比结果字典
        """
        if name not in self.baseline_data:
            return {'error': f'基线 "{name}" 不存在'}
        
        baseline = self.baseline_data[name]
        
        current_total_pss = sum(p.pss_kb for p in current_snapshot.processes) if current_snapshot.processes else current_snapshot.total_pss
        current_total_cpu = sum(p.cpu_usage for p in current_snapshot.processes) if current_snapshot.processes else current_snapshot.cpu_usage
        current_total_gc = sum(p.gc_count for p in current_snapshot.processes) if current_snapshot.processes else current_snapshot.gc_count
        
        return {
            'baseline_name': name,
            'baseline_timestamp': baseline['timestamp'],
            'comparison_timestamp': current_snapshot.timestamp.isoformat(),
            'pss': {
                'baseline': baseline['total_pss_kb'],
                'current': current_total_pss,
                'diff': current_total_pss - baseline['total_pss_kb'],
                'diff_percent': ((current_total_pss - baseline['total_pss_kb']) / baseline['total_pss_kb'] * 100) if baseline['total_pss_kb'] > 0 else 0
            },
            'cpu': {
                'baseline': baseline['total_cpu_percent'],
                'current': current_total_cpu,
                'diff': current_total_cpu - baseline['total_cpu_percent'],
                'diff_percent': ((current_total_cpu - baseline['total_cpu_percent']) / baseline['total_cpu_percent'] * 100) if baseline['total_cpu_percent'] > 0 else 0
            },
            'gc': {
                'baseline': baseline['total_gc_count'],
                'current': current_total_gc,
                'diff': current_total_gc - baseline['total_gc_count']
            },
            'fps': {
                'baseline': baseline['fps'],
                'current': current_snapshot.fps,
                'diff': current_snapshot.fps - baseline['fps']
            },
            'jank': {
                'baseline': baseline['jank_count'],
                'current': current_snapshot.jank_count,
                'diff': current_snapshot.jank_count - baseline['jank_count']
            }
        }
    
    def list_baselines(self) -> List[Dict]:
        """列出所有基线"""
        return [
            {
                'name': name,
                'timestamp': data['timestamp'],
                'description': data.get('description', '')
            }
            for name, data in self.baseline_data.items()
        ]
    
    def delete_baseline(self, name: str) -> bool:
        """删除基线"""
        if name in self.baseline_data:
            del self.baseline_data[name]
            self.save()
            return True
        return False
    
    def load(self):
        """加载基线数据"""
        if os.path.exists(self.baseline_file):
            try:
                with open(self.baseline_file, 'r', encoding='utf-8') as f:
                    self.baseline_data = json.load(f)
            except Exception as e:
                print(f"加载基线数据失败: {e}")
                self.baseline_data = {}
        else:
            self.baseline_data = {}
    
    def save(self):
        """保存基线数据"""
        try:
            with open(self.baseline_file, 'w', encoding='utf-8') as f:
                json.dump(self.baseline_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存基线数据失败: {e}")
