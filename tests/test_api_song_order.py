"""
点歌模块 API 自动化测试
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestApiSongOrder(unittest.TestCase):
    """点歌 API 测试"""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()

    def test_config(self):
        """GET /song_order/api/config 返回配置"""
        r = self.client.get('/song_order/api/config')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('ok', False))
        d = data.get('data', {})
        self.assertIn('host', d)
        self.assertIn('search_port', d)
        self.assertIn('vod_port', d)

    def test_history(self):
        """GET /song_order/api/history 返回历史列表"""
        r = self.client.get('/song_order/api/history?limit=10')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('ok', False))
        self.assertIn('data', data)
        self.assertIn('entries', data['data'])
        self.assertIsInstance(data['data']['entries'], list)

    def test_order_missing_musicno(self):
        """POST /song_order/api/order 缺少 musicno 返回 400"""
        r = self.client.post('/song_order/api/order', json={})
        self.assertEqual(r.status_code, 400)
        data = r.get_json()
        self.assertFalse(data.get('success', True))
        self.assertIn('musicno', str(data).lower())


if __name__ == '__main__':
    unittest.main()
