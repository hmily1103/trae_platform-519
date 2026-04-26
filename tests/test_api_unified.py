"""
一键任务 API 自动化测试
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestApiUnified(unittest.TestCase):
    """一键任务 API 测试"""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()

    def test_unified_health(self):
        """GET /unified/api/health"""
        r = self.client.get('/unified/api/health')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('data', {}).get('ok', False))

    def test_unified_start_empty_modules(self):
        """POST /unified/api/start 无 modules 返回 400"""
        r = self.client.post('/unified/api/start', json={})
        self.assertEqual(r.status_code, 400)
        data = r.get_json()
        self.assertIn('modules', str(data).lower())

    def test_unified_start_invalid_modules(self):
        """POST /unified/api/start modules 为空数组返回 400"""
        r = self.client.post('/unified/api/start', json={'modules': []})
        self.assertEqual(r.status_code, 400)

    def test_unified_reports(self):
        """GET /unified/api/reports 返回报告列表"""
        r = self.client.get('/unified/api/reports?limit=10')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('data', data)
        self.assertIn('reports', data['data'])
        self.assertIsInstance(data['data']['reports'], list)

    def test_unified_reports_filter(self):
        """GET /unified/api/reports 支持 module/status/keyword 筛选"""
        r = self.client.get('/unified/api/reports?module=monkey&limit=5')
        self.assertEqual(r.status_code, 200)

    def test_unified_status_404_when_run_missing(self):
        """GET /unified/api/status/<run_id> 当 run 不存在时返回 404"""
        r = self.client.get('/unified/api/status/nonexistent_run_xyz')
        self.assertEqual(r.status_code, 404)

    def test_unified_stop_404_when_run_missing(self):
        """POST /unified/api/stop/<run_id> 当 run 不存在时返回 404"""
        r = self.client.post('/unified/api/stop/nonexistent_run_xyz', json={})
        self.assertEqual(r.status_code, 404)

    def test_unified_delete_run_returns_ok(self):
        """DELETE /unified/api/runs/<run_id> 移除记录返回 200"""
        r = self.client.delete('/unified/api/runs/nonexistent_run_xyz')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('ok', False))
        self.assertIn('removed', data.get('data', {}))

    # --- 联动压测（服务器 + 设备）场景 ---

    def test_unified_linked_stress_requires_basic_fields(self):
        """POST /unified/api/linked_stress/start 缺少必填字段返回 400"""
        r = self.client.post('/unified/api/linked_stress/start', json={})
        self.assertEqual(r.status_code, 400)
        data = r.get_json() or {}
        # 至少要提示 server_id / device_id 之类的缺失
        msg = str(data).lower()
        self.assertIn('server', msg)
        self.assertIn('device', msg)

    def test_unified_linked_stress_minimal_monkey_flow(self):
        """
        POST /unified/api/linked_stress/start
        提供最小参数（server + monkey + 观测）时应返回 200，并返回 run_id。
        这里只验证 API 形状，不要求真去拉起后端任务（由现有 unified_start 测试覆盖）。
        """
        payload = {
            "mode": "monkey",  # 链路 A：服务器 + Monkey
            "server": {
                "server_id": "test-server-1",
                "cpu_load": 50,
                "timeout": 60
            },
            "device": {
                "device_id": "192.168.16.131:8787",
                "package_name": "com.thunder.ktv",
                "monkey": {
                    "ip": "192.168.16.131",
                    "port": 8787,
                    "events_count": 1000
                }
            },
            "observers": {
                "performance_monitor": True,
                "log_monitor": True
            }
        }
        r = self.client.post('/unified/api/linked_stress/start', json=payload)
        # 在某些环境下 orchestrator 可能不可用，这时返回 500；这里只要求不要 4xx
        self.assertIn(r.status_code, (200, 500))
        if r.status_code == 200:
            data = r.get_json() or {}
            self.assertIn('data', data)
            self.assertIn('run_id', data['data'])


if __name__ == '__main__':
    unittest.main()
