"""
平台 API 自动化测试：健康检查、仪表盘、公告、OpenAPI
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestApiPlatform(unittest.TestCase):
    """平台核心 API 测试"""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()

    def test_health(self):
        """GET /api/health 返回 200 且包含 status"""
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('status', data)
        self.assertIn('modules', data)
        self.assertEqual(data['status'], 'ok')

    def test_announcements(self):
        """GET /api/announcements 返回 200"""
        r = self.client.get('/api/announcements')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('ok', False))
        self.assertIn('data', data)

    def test_dashboard_stats(self):
        """GET /api/dashboard/stats 返回 200"""
        r = self.client.get('/api/dashboard/stats')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('ok', False))
        d = data.get('data', {})
        self.assertIn('devices', d)
        self.assertIn('modules', d)
        self.assertIn('failed_modules', d)

    def test_modules_status(self):
        """GET /api/modules/status 返回 200"""
        r = self.client.get('/api/modules/status')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get('ok', False))
        self.assertIn('modules', data)

    def test_openapi_json(self):
        """GET /api/openapi.json 返回有效 OpenAPI 规范"""
        r = self.client.get('/api/openapi.json')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('openapi', data)
        self.assertIn('info', data)
        self.assertIn('paths', data)
        self.assertIn('Trae Platform', str(data.get('info', {}).get('title', '')))

    def test_docs_page(self):
        """GET /docs 返回 200"""
        r = self.client.get('/docs')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'swagger-ui', r.data)

    def test_index(self):
        """GET / 返回 200"""
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)

    def test_404_json(self):
        """不存在的 API 返回 JSON 格式 404"""
        r = self.client.get('/api/nonexistent')
        self.assertEqual(r.status_code, 404)
        data = r.get_json()
        self.assertIsNotNone(data)
        self.assertIn('ok', data)
        self.assertFalse(data.get('ok', True))


if __name__ == '__main__':
    unittest.main()
