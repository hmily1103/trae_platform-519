"""
批次 Monkey（4 板同时 / 多轮循环 / 统一报告 / N 分钟采样）TDD 风格测试。

先写测试描述「期望行为」，再跑通实现（Red → Green）。
运行：pytest tests/test_monkey_batch.py -v
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestMonkeyBatchAPI(unittest.TestCase):
    """批次 Monkey API 行为"""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()

    def test_batch_start_empty_device_ids_returns_400(self):
        """POST /monkey/api/batch/start 无 device_ids 返回 400"""
        r = self.client.post("/monkey/api/batch/start", json={})
        self.assertEqual(r.status_code, 400)
        data = r.get_json()
        self.assertIn("device_ids", (data.get("message") or "").lower())

    def test_batch_start_valid_device_ids_returns_200_and_batch_id(self):
        """POST /monkey/api/batch/start 传入 device_ids 返回 200 且带 batch_id"""
        r = self.client.post(
            "/monkey/api/batch/start",
            json={
                "device_ids": ["192.168.1.101:8787", "192.168.1.102:8787"],
                "rounds": 1,
                "sample_interval_minutes": 2,
            },
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data.get("ok") or data.get("success"))
        self.assertIn("batch_id", data.get("data") or {})
        batch_id = (data.get("data") or {}).get("batch_id")
        self.assertTrue(batch_id and batch_id.startswith("batch_"))

    def test_batch_status_unknown_returns_404(self):
        """GET /monkey/api/batch/<batch_id>/status 未知 batch_id 返回 404"""
        r = self.client.get("/monkey/api/batch/nonexistent_batch_xyz/status")
        self.assertEqual(r.status_code, 404)

    def test_batch_status_known_returns_200_with_structure(self):
        """GET /monkey/api/batch/<batch_id>/status 已知 batch 返回 200 且含 device_ids、rounds、status"""
        start_r = self.client.post(
            "/monkey/api/batch/start",
            json={
                "device_ids": ["192.168.1.201:8787"],
                "rounds": 1,
                "sample_interval_minutes": 2,
            },
        )
        self.assertEqual(start_r.status_code, 200)
        batch_id = (start_r.get_json().get("data") or {}).get("batch_id")
        self.assertIsNotNone(batch_id)

        r = self.client.get(f"/monkey/api/batch/{batch_id}/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json().get("data") or r.get_json()
        self.assertIn("batch_id", data)
        self.assertIn("device_ids", data)
        self.assertIn("rounds", data)
        self.assertIn("status", data)
        self.assertEqual(data["batch_id"], batch_id)
        self.assertEqual(data["rounds"], 1)
        self.assertIn("192.168.1.201:8787", data["device_ids"])

    def test_batch_report_unknown_returns_404(self):
        """GET /monkey/api/batch/<batch_id>/report 未知 batch_id 返回 404"""
        r = self.client.get("/monkey/api/batch/nonexistent_batch_xyz/report")
        self.assertEqual(r.status_code, 404)

    def test_batch_report_known_returns_200_html(self):
        """GET /monkey/api/batch/<batch_id>/report 已知 batch 返回 200 且为 HTML"""
        start_r = self.client.post(
            "/monkey/api/batch/start",
            json={
                "device_ids": ["192.168.1.301:8787"],
                "rounds": 1,
                "sample_interval_minutes": 2,
            },
        )
        self.assertEqual(start_r.status_code, 200)
        batch_id = (start_r.get_json().get("data") or {}).get("batch_id")
        self.assertIsNotNone(batch_id)

        r = self.client.get(f"/monkey/api/batch/{batch_id}/report")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("text/html", r.headers.get("Content-Type", ""))
        html = r.get_data(as_text=True)
        self.assertIn(batch_id, html)


class TestDownsampleLogic(unittest.TestCase):
    """N 分钟采样（_downsample_to_minutes）行为"""

    def test_downsample_empty_returns_empty(self):
        """空 history 返回空列表"""
        from modules.monkey.views import _downsample_to_minutes
        self.assertEqual(_downsample_to_minutes([], 2), [])
        self.assertEqual(_downsample_to_minutes(None, 2), None)

    def test_downsample_interval_zero_returns_original(self):
        """interval_minutes <= 0 返回原列表"""
        from modules.monkey.views import _downsample_to_minutes
        hist = [{"ts": 1000, "fps": 30, "cpu": 10, "mem": 200}]
        self.assertEqual(_downsample_to_minutes(hist, 0), hist)

    def test_downsample_aggregates_by_interval(self):
        """按 N 分钟间隔聚合，输出点数少于等于输入且包含 fps/cpu/mem"""
        from modules.monkey.views import _downsample_to_minutes
        # 3 个点，间隔 1 秒；按 2 分钟(120s) 聚合应得到更少的点
        base_ts = 1000
        hist = [
            {"ts": base_ts + i * 1, "fps": 30 + i, "cpu": 10 + i, "mem": 200 + i}
            for i in range(5)
        ]
        out = _downsample_to_minutes(hist, 2)
        self.assertIsInstance(out, list)
        self.assertLessEqual(len(out), len(hist))
        for p in out:
            self.assertIn("fps", p)
            self.assertIn("cpu", p)
            self.assertIn("mem", p)
            self.assertIn("ts", p)


class TestParseTs(unittest.TestCase):
    """_parse_ts 时间戳解析行为"""

    def test_parse_ts_none_returns_zero(self):
        from modules.monkey.views import _parse_ts
        self.assertEqual(_parse_ts(None), 0)

    def test_parse_ts_number_returns_float(self):
        from modules.monkey.views import _parse_ts
        self.assertEqual(_parse_ts(1609459200.0), 1609459200.0)
        self.assertEqual(_parse_ts(100), 100.0)
