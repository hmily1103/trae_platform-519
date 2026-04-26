# -*- coding: utf-8 -*-
import os
import json
import time
import uuid
import threading
from typing import Any, Dict, Optional, Callable

from utils.logger import setup_logger

logger = setup_logger("prd_audit_job_queue")


class JobQueue:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, storage_dir: str):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(JobQueue, cls).__new__(cls)
                cls._instance._init(storage_dir=storage_dir)
            return cls._instance

    def _init(self, storage_dir: str) -> None:
        self.storage_dir = storage_dir
        self.jobs_file = os.path.join(storage_dir, "learning_repo", "jobs.json")
        self._jobs_lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._load()

    def register_handler(self, job_type: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self._handlers[job_type] = handler

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def enqueue(self, job_type: str, payload: Dict[str, Any]) -> str:
        self.start()
        job_id = uuid.uuid4().hex
        now_ts = int(time.time())
        job = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "queued",
            "created_at": now_ts,
            "started_at": 0,
            "finished_at": 0,
            "payload": payload or {},
            "result": None,
            "error": "",
            "cancel_requested": False,
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
            self._save_locked()
        return job_id

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return dict(j) if isinstance(j, dict) else None

    def list(self, limit: int = 50) -> Dict[str, Any]:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
        return {"jobs": jobs[: max(1, min(limit, 200))]}

    def cancel(self, job_id: str) -> bool:
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            if not isinstance(j, dict):
                return False
            if str(j.get("status")) in ("succeeded", "failed", "canceled"):
                return False
            j["cancel_requested"] = True
            self._save_locked()
            return True

    def _load(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.jobs_file), exist_ok=True)
            if os.path.exists(self.jobs_file):
                with open(self.jobs_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
                    self._jobs = data["jobs"]
        except Exception as e:
            logger.warning("load jobs failed: %s", e)
            self._jobs = {}

    def _save_locked(self) -> None:
        data = {"jobs": self._jobs}
        tmp = self.jobs_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.jobs_file)

    def _loop(self) -> None:
        while self._running:
            job = None
            with self._jobs_lock:
                for j in self._jobs.values():
                    if str(j.get("status")) == "queued":
                        job = j
                        break
                if job:
                    job["status"] = "running"
                    job["started_at"] = int(time.time())
                    self._save_locked()
            if not job:
                time.sleep(0.5)
                continue

            job_id = str(job.get("job_id"))
            job_type = str(job.get("job_type"))
            handler = self._handlers.get(job_type)
            if not handler:
                with self._jobs_lock:
                    j = self._jobs.get(job_id)
                    if isinstance(j, dict):
                        j["status"] = "failed"
                        j["finished_at"] = int(time.time())
                        j["error"] = f"no handler for job_type={job_type}"
                        self._save_locked()
                continue

            if bool(job.get("cancel_requested")):
                with self._jobs_lock:
                    j = self._jobs.get(job_id)
                    if isinstance(j, dict):
                        j["status"] = "canceled"
                        j["finished_at"] = int(time.time())
                        self._save_locked()
                continue

            try:
                result = handler(dict(job.get("payload") or {}))
                with self._jobs_lock:
                    j = self._jobs.get(job_id)
                    if isinstance(j, dict):
                        if bool(j.get("cancel_requested")):
                            j["status"] = "canceled"
                        else:
                            j["status"] = "succeeded"
                            j["result"] = result
                        j["finished_at"] = int(time.time())
                        self._save_locked()
            except Exception as e:
                with self._jobs_lock:
                    j = self._jobs.get(job_id)
                    if isinstance(j, dict):
                        j["status"] = "failed"
                        j["finished_at"] = int(time.time())
                        j["error"] = str(e)
                        self._save_locked()

