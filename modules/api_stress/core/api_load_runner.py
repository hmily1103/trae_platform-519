# -*- coding: utf-8 -*-
"""HTTP API 压测运行器：多线程并发请求，统计 QPS、延迟、错误率"""
import threading
import time
import json
import logging
import random
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

# 默认接口预设
DEFAULT_PRESETS = {
    "search": {
        "name": "搜索歌曲",
        "url": "http://192.168.16.210:9000/media/newsearchinfo",
        "method": "POST",
        "query_params": "page=1&size=10",
        "headers": {"Content-Type": "application/json"},
        "body": '{"searchtype":1,"content":"周杰伦"}',
        "keywords": ["周杰伦", "稻香", "七里香", "晴天", "告白气球", "Beyond", "海阔天空", "喜欢你", "张学友", "吻别", "邓紫棋", "泡沫", "光年之外", "陈奕迅", "十年", "王菲", "传奇", "五月天", "倔强", "突然好想你", "a", "love", "test", "歌", "%", "#", "&", "*", "?", "-", "_", "周&杰", "歌%曲", "a+b"],
    },
    "order": {
        "name": "点歌",
        "url": "http://192.168.16.210:8008/song/vod",
        "method": "POST",
        "query_params": "",
        "headers": {"Content-Type": "application/json"},
        "body": '{"roominfo":"86f02338_192.168.1.134","musicinfo":[{"musicno":"5000176","musicname":""}],"parm":"{\\"vip\\":0}","userid":"123","appid":"32432424"}',
    },
}


@dataclass
class LoadMetrics:
    """压测指标"""
    total_requests: int = 0
    success_count: int = 0
    error_count: int = 0
    timeouts: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    status_code_counts: Dict[str, int] = field(default_factory=dict)
    exception_counts: Dict[str, int] = field(default_factory=dict)
    error_samples: List[Dict[str, Any]] = field(default_factory=list)  # Store recent error details
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(
        self,
        success: bool,
        latency_ms: float,
        is_timeout: bool = False,
        status_code: Optional[int] = None,
        exception_name: Optional[str] = None,
        error_detail: Optional[str] = None,
    ):
        with self._lock:
            self.total_requests += 1
            if is_timeout:
                self.timeouts += 1
                self.error_count += 1
            elif success:
                self.success_count += 1
                self.latencies_ms.append(latency_ms)
            else:
                self.error_count += 1
            if status_code is not None:
                key = str(status_code)
                self.status_code_counts[key] = self.status_code_counts.get(key, 0) + 1
            if exception_name:
                self.exception_counts[exception_name] = self.exception_counts.get(exception_name, 0) + 1
            
            # Record error sample if failed and we have room (keep last 20)
            if not success and error_detail:
                sample = {
                    "time": time.strftime("%H:%M:%S", time.localtime()),
                    "type": "HTTP" if status_code else "Exception",
                    "code": status_code or exception_name or "Unknown",
                    "message": error_detail[:500]  # Truncate long messages
                }
                self.error_samples.append(sample)
                if len(self.error_samples) > 20:
                    self.error_samples.pop(0)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lat = list(self.latencies_ms)
            total = self.total_requests
            success = self.success_count
            errors = self.error_count
            timeouts = self.timeouts
            status_code_counts = dict(self.status_code_counts)
            exception_counts = dict(self.exception_counts)
            error_samples = list(self.error_samples)
        if not lat:
            return {
                "total_requests": total,
                "success_count": success,
                "error_count": errors,
                "timeouts": timeouts,
                "qps": 0,
                "avg_latency_ms": 0,
                "min_latency_ms": 0,
                "max_latency_ms": 0,
                "p50_ms": 0,
                "p90_ms": 0,
                "p95_ms": 0,
                "p99_ms": 0,
                "error_rate": (errors / total * 100) if total else 0,
                "status_code_counts": status_code_counts,
                "exception_counts": exception_counts,
                "error_samples": error_samples,
            }
        lat.sort()
        n = len(lat)
        p50_idx = max(0, int(n * 0.50) - 1)
        p90_idx = max(0, int(n * 0.90) - 1)
        p95_idx = max(0, int(n * 0.95) - 1)
        p99_idx = max(0, int(n * 0.99) - 1)
        return {
            "total_requests": total,
            "success_count": success,
            "error_count": errors,
            "timeouts": timeouts,
            "avg_latency_ms": round(sum(lat) / n, 2),
            "min_latency_ms": round(lat[0], 2),
            "max_latency_ms": round(lat[-1], 2),
            "p50_ms": round(lat[p50_idx], 2),
            "p90_ms": round(lat[p90_idx], 2),
            "p95_ms": round(lat[p95_idx], 2),
            "p99_ms": round(lat[p99_idx], 2),
            "error_rate": round((errors / total * 100), 2) if total else 0,
            "status_code_counts": status_code_counts,
            "exception_counts": exception_counts,
            "error_samples": error_samples,
        }


class ApiLoadRunner:
    """API 压测运行器"""

    def __init__(
        self,
        url: str,
        method: str = "POST",
        query_params: str = "",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        concurrency: int = 10,
        duration_sec: int = 60,
        timeout_sec: int = 10,
        keywords: Optional[List[str]] = None,
        on_metrics: Optional[Callable[[Dict], None]] = None,
        on_complete: Optional[Callable[[Dict], None]] = None,
    ):
        self.url = url.rstrip("/")
        self.on_complete = on_complete
        self.method = method.upper()
        self.query_params = (query_params or "").strip()
        self.headers = headers or {}
        self.body = body
        self.concurrency = max(1, concurrency)
        self.duration_sec = max(1, duration_sec)
        self.timeout_sec = max(1, timeout_sec)
        self.keywords = keywords or []
        self.on_metrics = on_metrics

        self._stop_event = threading.Event()
        self._metrics = LoadMetrics()
        self._threads: List[threading.Thread] = []
        self._start_time: Optional[float] = None
        self._start_monotonic: Optional[float] = None
        self._end_monotonic: Optional[float] = None
        self._deadline_monotonic: Optional[float] = None
        self._history: List[Dict[str, Any]] = []
        self._metrics_interval = 1

    def _build_full_url(self) -> str:
        base = self.url
        if self.query_params:
            sep = "&" if "?" in base else "?"
            return base + sep + self.query_params
        return base

    def _get_body(self) -> Optional[str]:
        """获取请求体，若有 keywords 则随机替换 content 字段"""
        if not self.body:
            return None
        if not self.keywords:
            return self.body
        try:
            data = json.loads(self.body)
            if isinstance(data, dict) and "content" in data:
                data["content"] = random.choice(self.keywords)
                return json.dumps(data, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
        return self.body

    def _worker(self):
        full_url = self._build_full_url()
        while not self._stop_event.is_set():
            deadline = self._deadline_monotonic
            if deadline is not None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                request_timeout = min(self.timeout_sec, max(0.05, remain))
            else:
                request_timeout = self.timeout_sec

            start = time.perf_counter()
            success = False
            is_timeout = False
            status_code: Optional[int] = None
            exception_name: Optional[str] = None
            error_detail: Optional[str] = None
            try:
                body_str = self._get_body()
                if self.method == "GET":
                    r = requests.get(
                        full_url,
                        headers=self.headers,
                        timeout=request_timeout,
                    )
                else:
                    r = requests.request(
                        self.method,
                        full_url,
                        headers=self.headers,
                        data=body_str,
                        timeout=request_timeout,
                    )
                status_code = getattr(r, "status_code", None)
                success = 200 <= r.status_code < 300
                if not success:
                    # Capture response text for failed requests
                    try:
                        error_detail = r.text
                    except Exception:
                        error_detail = "[Unable to read response text]"
            except requests.exceptions.Timeout:
                is_timeout = True
                success = False
                exception_name = "Timeout"
                error_detail = f"Request timed out after {request_timeout}s"
            except Exception as e:
                logger.debug(f"Request error: {e}")
                success = False
                exception_name = type(e).__name__
                error_detail = str(e)
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._metrics.add(
                success=success,
                latency_ms=elapsed_ms,
                is_timeout=is_timeout,
                status_code=status_code,
                exception_name=exception_name,
                error_detail=error_detail,
            )

    def _metrics_reporter(self):
        """定期上报指标。注意：不能在此调用 stop()，否则会死锁（stop 会 join 本线程）"""
        while not self._stop_event.is_set():
            self._stop_event.wait(self._metrics_interval)
            if self._stop_event.is_set():
                break
            start_mono = self._start_monotonic or time.monotonic()
            elapsed = time.monotonic() - start_mono
            snap = self._metrics.snapshot()
            if elapsed > 0 and snap["success_count"]:
                snap["qps"] = round(snap["success_count"] / elapsed, 2)
            else:
                snap["qps"] = 0
            snap["elapsed_sec"] = round(elapsed, 1)
            history_point = dict(snap)
            history_point.pop("status_code_counts", None)
            history_point.pop("exception_counts", None)
            self._history.append(history_point)
            if self.on_metrics:
                try:
                    self.on_metrics(snap)
                except Exception as e:
                    logger.warning(f"on_metrics callback error: {e}")

    def start(self) -> None:
        self._stop_event.clear()
        self._metrics = LoadMetrics()
        self._history = []
        self._start_time = time.time()
        self._start_monotonic = time.monotonic()
        self._end_monotonic = None
        self._deadline_monotonic = (self._start_monotonic or time.monotonic()) + float(self.duration_sec)
        for _ in range(self.concurrency):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)
        reporter = threading.Thread(target=self._metrics_reporter, daemon=True)
        reporter.start()
        self._threads.append(reporter)

        # 定时结束：使用 monotonic，避免系统时间回拨/跳变导致提前结束或卡住
        def timer():
            while True:
                if self._stop_event.is_set():
                    break
                deadline = self._deadline_monotonic
                if deadline is None:
                    break
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                self._stop_event.wait(min(0.2, max(0.01, remain)))

            if not self._stop_event.is_set():
                self.stop()
            try:
                metrics = self.get_final_metrics()
                if self.on_complete:
                    self.on_complete(metrics)
            except Exception as e:
                logger.warning(f"on_complete error: {e}")

        threading.Thread(target=timer, daemon=True).start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._end_monotonic is None:
            self._end_monotonic = time.monotonic()

        join_deadline = time.monotonic() + 2.0
        for t in self._threads:
            if not t.is_alive():
                continue
            remain = join_deadline - time.monotonic()
            if remain <= 0:
                break
            t.join(timeout=min(0.2, remain))
        self._threads.clear()

    def get_final_metrics(self) -> Dict[str, Any]:
        snap = self._metrics.snapshot()
        start_mono = self._start_monotonic or time.monotonic()
        end_mono = self._end_monotonic or time.monotonic()
        elapsed = (end_mono - start_mono) or 0.001
        snap["qps"] = round(snap["success_count"] / elapsed, 2)
        snap["elapsed_sec"] = round(elapsed, 1)
        snap["duration_sec"] = self.duration_sec
        snap["start_time"] = self._start_time
        snap["end_time"] = (self._start_time + elapsed) if self._start_time else None
        timeline = list(self._history)
        final_point = dict(snap)
        final_point.pop("timeline", None)
        final_point.pop("status_code_counts", None)
        final_point.pop("exception_counts", None)
        final_point.pop("duration_sec", None)
        final_point.pop("start_time", None)
        final_point.pop("end_time", None)
        if not timeline or timeline[-1].get("elapsed_sec") != final_point.get("elapsed_sec"):
            timeline.append(final_point)
        snap["timeline"] = timeline
        return snap
