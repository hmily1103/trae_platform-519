# -*- coding: utf-8 -*-
"""API 压测管理器：单例，管理当前运行的压测任务"""
import threading
import logging
import os
from typing import Dict, Any, Optional, Tuple

from .api_load_runner import ApiLoadRunner, DEFAULT_PRESETS
from .report_store import save_report

logger = logging.getLogger(__name__)


class ApiStressManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._runner: Optional[ApiLoadRunner] = None
        self._job_lock = threading.Lock()
        self._last_metrics: Dict[str, Any] = {}
        self._last_config: Dict[str, Any] = {}
        self._last_report_id: Optional[str] = None
        self._report_saved: bool = False  # 防止定时器与手动停止重复保存报告
        self._metrics_callback_lock = threading.Lock()
        self._app_root: Optional[str] = None

    def set_app_root(self, app_root: str) -> None:
        self._app_root = app_root

    @classmethod
    def get_instance(cls) -> "ApiStressManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = ApiStressManager()
        return cls._instance

    def get_presets(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in DEFAULT_PRESETS.items()}

    def _ensure_state_consistent(self) -> None:
        """若 runner 存在且工作线程已全部结束，则强制清理。threads 为空时不清除（timer 可能刚执行 stop 尚未 on_complete）"""
        if not self._runner:
            return
        try:
            threads = getattr(self._runner, "_threads", [])
            if threads and all(not t.is_alive() for t in threads):
                self._runner = None
                self._report_saved = False
        except Exception:
            self._runner = None

    def _on_metrics(self, metrics: Dict[str, Any]) -> None:
        with self._metrics_callback_lock:
            self._last_metrics = metrics

    def get_last_metrics(self) -> Dict[str, Any]:
        with self._metrics_callback_lock:
            return dict(self._last_metrics)

    def _save_report(self, metrics: Dict[str, Any], end_reason: str = "completed") -> Optional[str]:
        """保存报告，返回 report_id；同一任务仅保存一次"""
        if self._report_saved:
            return self._last_report_id
        try:
            rid = save_report(
                config=self._last_config,
                results=metrics,
                end_reason=end_reason,
                app_root=self._app_root,
            )
            if rid:
                self._last_report_id = rid
                self._report_saved = True
            return rid
        except Exception as e:
            logger.warning(f"Save report failed: {e}")
            return None

    def start(
        self,
        url: str,
        method: str = "POST",
        query_params: str = "",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        concurrency: int = 10,
        duration_sec: int = 60,
        timeout_sec: int = 10,
        keywords: Optional[list] = None,
    ) -> Tuple[bool, str]:
        """启动压测，返回 (success, message)"""
        with self._job_lock:
            self._ensure_state_consistent()
            if self._runner:
                return False, "已有压测任务在运行，请先停止"

            config = {
                "url": url, "method": method, "query_params": query_params,
                "headers": headers, "body": body, "concurrency": concurrency,
                "duration_sec": duration_sec, "timeout_sec": timeout_sec,
                "keywords": keywords,
            }
            self._last_config = config
            self._report_saved = False

            def on_complete(metrics: Dict[str, Any]) -> None:
                try:
                    with self._job_lock:
                        with self._metrics_callback_lock:
                            self._last_metrics = metrics
                        self._save_report(metrics, "completed")
                except Exception as e:
                    logger.warning(f"on_complete error: {e}")
                finally:
                    with self._job_lock:
                        self._runner = None

            try:
                runner = ApiLoadRunner(
                    url=url,
                    method=method,
                    query_params=query_params,
                    headers=headers,
                    body=body,
                    concurrency=concurrency,
                    duration_sec=duration_sec,
                    timeout_sec=timeout_sec,
                    keywords=keywords,
                    on_metrics=self._on_metrics,
                    on_complete=on_complete,
                )
                runner.start()
                self._runner = runner
                return True, "压测已启动"
            except Exception as e:
                logger.exception("Start api stress failed")
                self._runner = None
                self._report_saved = False
                return False, str(e)

    def stop(self) -> Tuple[bool, str, Optional[Dict[str, Any]], Optional[str]]:
        """停止压测，返回 (success, message, metrics, report_id)"""
        with self._job_lock:
            if not self._runner:
                return False, "当前没有运行中的压测任务", None, None
            try:
                self._runner.stop()
                metrics = self._runner.get_final_metrics()
                with self._metrics_callback_lock:
                    self._last_metrics = metrics
                self._runner = None
                report_id = self._save_report(metrics, "stopped")
                return True, "压测已停止", metrics, report_id
            except Exception as e:
                logger.exception("Stop api stress failed")
                self._runner = None
                return False, str(e), None, None

    def force_reset(self) -> bool:
        """强制清除压测状态，返回是否曾存在运行中的任务"""
        with self._job_lock:
            had = self._runner is not None
            self._runner = None
            self._report_saved = False
            return had

    def is_running(self) -> bool:
        with self._job_lock:
            self._ensure_state_consistent()
            return self._runner is not None

    def get_metrics(self) -> Optional[Dict[str, Any]]:
        with self._job_lock:
            self._ensure_state_consistent()
            if self._runner:
                m = self.get_last_metrics()
                # 实时计算 QPS
                return m
        return self.get_last_metrics() or None
