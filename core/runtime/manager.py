from typing import Dict, List, Optional
import threading
import time
from .model import RuntimeObject, RuntimeStatus
from .storage import get_runtime_storage


class RuntimeManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(RuntimeManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._runtimes: Dict[str, RuntimeObject] = {}
        self._lock = threading.Lock()
        self._initialized = True

    def _persist(self, r: RuntimeObject) -> None:
        try:
            get_runtime_storage().save(r)
        except Exception:
            pass

    def create_runtime(self, name: str, module: str, context: Dict = None, owner: str = "system") -> RuntimeObject:
        """Create and register a new RuntimeObject (memory + SQLite)."""
        runtime = RuntimeObject(
            name=name,
            module=module,
            context=context or {},
            owner=owner
        )
        with self._lock:
            self._runtimes[runtime.runtime_id] = runtime
        self._persist(runtime)
        return runtime

    def get_runtime(self, runtime_id: str) -> Optional[RuntimeObject]:
        """Retrieve a RuntimeObject by ID (memory first, then SQLite)."""
        with self._lock:
            r = self._runtimes.get(runtime_id)
        if r:
            return r
        r = get_runtime_storage().load(runtime_id)
        if r:
            with self._lock:
                self._runtimes[runtime_id] = r
        return r

    def list_runtimes(self, module: str = None, status: RuntimeStatus = None, limit: int = 50) -> List[RuntimeObject]:
        """List runtimes (memory + SQLite merged), filtered and sorted by created_at desc."""
        with self._lock:
            mem_ids = set(self._runtimes.keys())
            mem_list = list(self._runtimes.values())
        stored = get_runtime_storage().list_all(module=module, status=status, limit=limit * 2)
        merged: Dict[str, RuntimeObject] = {r.runtime_id: r for r in mem_list}
        for r in stored:
            if r.runtime_id not in merged:
                merged[r.runtime_id] = r
        all_runtimes = sorted(merged.values(), key=lambda x: x.created_at, reverse=True)
        filtered = []
        for r in all_runtimes:
            if module and r.module != module:
                continue
            if status and r.status != status:
                continue
            filtered.append(r)
            if len(filtered) >= limit:
                break
        return filtered

    def update_status(self, runtime_id: str, status: RuntimeStatus, result: Dict = None):
        """Update the lifecycle status of a runtime (memory + SQLite)."""
        runtime = self.get_runtime(runtime_id)
        if not runtime:
            raise ValueError(f"Runtime {runtime_id} not found")

        with self._lock:
            runtime.status = status
            if status == RuntimeStatus.RUNNING and not runtime.started_at:
                runtime.started_at = time.time()
            if status in (RuntimeStatus.COMPLETED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED):
                if status != RuntimeStatus.PAUSED:
                    runtime.ended_at = time.time()
            if result:
                runtime.result.update(result)
        self._persist(runtime)

# Global Accessor
def get_runtime_manager():
    return RuntimeManager()
