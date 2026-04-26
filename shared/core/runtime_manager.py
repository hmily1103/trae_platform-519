import os
import json
import threading
from datetime import datetime
from typing import Optional, Dict, List
from pathlib import Path

# Import TROM models
try:
    from trae_platform.shared.core.trom import (
        TestRuntime, Meta, Context, TaskType, RuntimeStatus, 
        DeviceContext, AppContext, Streams, Analysis, Summary, Artifacts
    )
except ImportError:
    # Relative import fallback for local testing
    from .trom import (
        TestRuntime, Meta, Context, TaskType, RuntimeStatus,
        DeviceContext, AppContext, Streams, Analysis, Summary, Artifacts
    )

class TestRuntimeManager:
    """
    Manages the lifecycle and persistence of TestRuntime objects.
    Acts as the 'Government' enforcing the 'Constitution' (TROM).
    """
    
    _instance = None
    _lock = threading.RLock()
    
    # Configuration
    STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "trom_runs")
    
    def __init__(self):
        self.active_runtimes: Dict[str, TestRuntime] = {}
        self._ensure_storage()

    @classmethod
    def get_instance(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = cls()
        return cls._instance

    def _ensure_storage(self):
        """Ensure the storage directory exists."""
        os.makedirs(self.STORAGE_DIR, exist_ok=True)

    def _get_file_path(self, runtime_id: str) -> str:
        return os.path.join(self.STORAGE_DIR, f"{runtime_id}.json")

    def create_runtime(self, 
                       task_type: TaskType, 
                       created_by: str, 
                       trigger_type: str = "manual",
                       device_id: str = "unknown",
                       package_name: str = "unknown") -> TestRuntime:
        """
        Initialize a new TestRuntime session.
        """
        # Generate Runtime ID: type_date_uuid_short
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        import uuid
        short_uuid = str(uuid.uuid4())[:8]
        runtime_id = f"{task_type}_{timestamp_str}_{short_uuid}"

        # 1. Create Meta
        meta = Meta(
            runtime_id=runtime_id,
            created_by=created_by,
            trigger_type=trigger_type
        )

        # 2. Create Context (Initial stub, can be updated later)
        context = Context(
            device=DeviceContext(device_id=device_id),
            app=AppContext(package_name=package_name)
        )

        # 3. Create Runtime Object
        runtime = TestRuntime(
            runtime_id=runtime_id,
            task_type=task_type,
            status=RuntimeStatus.INIT,
            meta=meta,
            context=context,
            streams=Streams(),
            analysis=Analysis(),
            summary=None,
            artifacts=Artifacts()
        )

        # Register in memory
        with self._lock:
            self.active_runtimes[runtime_id] = runtime
        
        # Initial Save
        self.save_runtime(runtime)
        
        return runtime

    def get_runtime(self, runtime_id: str) -> Optional[TestRuntime]:
        """Get a runtime from memory or disk."""
        with self._lock:
            if runtime_id in self.active_runtimes:
                return self.active_runtimes[runtime_id]
        
        # Try load from disk
        return self.load_runtime_from_disk(runtime_id)

    def save_runtime(self, runtime: TestRuntime):
        """Persist the runtime state to disk."""
        file_path = self._get_file_path(runtime.runtime_id)
        
        # Use Pydantic's serialization
        # Note: model_dump_json is for Pydantic v2, dict() or json() for v1
        # We try to be compatible
        try:
            data_json = runtime.json()
        except AttributeError:
            # Fallback if Pydantic v2 calls it model_dump_json
            data_json = runtime.model_dump_json()

        with self._lock:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(data_json)

    def load_runtime_from_disk(self, runtime_id: str) -> Optional[TestRuntime]:
        """Load a runtime from JSON file."""
        file_path = self._get_file_path(runtime_id)
        if not os.path.exists(file_path):
            return None
            
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Pydantic parsing
            runtime = TestRuntime.parse_obj(data)
            
            # Cache it? Maybe not always, but for now let's say yes if we load it
            with self._lock:
                self.active_runtimes[runtime_id] = runtime
                
            return runtime
        except Exception as e:
            print(f"Error loading runtime {runtime_id}: {e}")
            return None

    def update_status(self, runtime_id: str, status: RuntimeStatus):
        """Update runtime status and save."""
        runtime = self.get_runtime(runtime_id)
        if runtime:
            runtime.status = status
            self.save_runtime(runtime)

    def finish_runtime(self, runtime_id: str, result: str = "pass", conclusion: str = ""):
        """Mark runtime as finished and generate summary."""
        runtime = self.get_runtime(runtime_id)
        if not runtime:
            return

        runtime.status = RuntimeStatus.FINISHED
        
        # Auto-calculate summary basics
        if not runtime.summary:
            runtime.summary = Summary(
                result=result,
                duration_sec=(datetime.utcnow() - runtime.meta.created_at).total_seconds(),
                conclusion=conclusion
            )
        
        # Remove from active memory to free resources (optional strategy)
        with self._lock:
            if runtime_id in self.active_runtimes:
                del self.active_runtimes[runtime_id]
        
        self.save_runtime(runtime)

# Helper for singleton access
def get_runtime_manager():
    return TestRuntimeManager.get_instance()
