from enum import Enum
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
import uuid

try:
    from pydantic import BaseModel, Field
except ImportError:
    # Fallback for when pydantic is not installed, though it's recommended
    class BaseModel:
        def dict(self, *args, **kwargs):
            return self.__dict__
        def json(self, *args, **kwargs):
            import json
            return json.dumps(self.dict(), default=str)
            
    def Field(*args, **kwargs):
        return None

# ==========================================
# 1. Enums & Constants (Enumerations)
# ==========================================

class TaskType(str, Enum):
    MONKEY = "monkey"
    UI_AUTO = "ui_auto"
    REGRESSION = "regression"
    PERF = "perf"
    MANUAL = "manual"

class RuntimeStatus(str, Enum):
    INIT = "INIT"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    FINISHED = "FINISHED"

class StreamSource(str, Enum):
    MONKEY = "monkey"
    UI_AUTO = "ui_auto"
    MANUAL = "manual"
    LOGCAT = "logcat"
    SYSTEM = "system"

class EventLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"

# ==========================================
# 2. Meta Layer (Governance Data)
# ==========================================

class Meta(BaseModel):
    """
    Governance data: Audit, Traceability, Compliance.
    Immutable facts about the test run creation.
    """
    runtime_id: str = Field(..., description="Unique ID for the test runtime")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = Field(..., description="User or system that triggered the run")
    trigger_type: str = Field(..., description="manual | schedule | pipeline")
    tool_version: str = Field(default="trae_platform_v1.0.0")
    schema_version: str = Field(default="TROM_v1.0")

# ==========================================
# 3. Context Layer (Environment Snapshot)
# ==========================================

class DeviceContext(BaseModel):
    device_id: str
    brand: Optional[str] = None
    model: Optional[str] = None
    os_version: Optional[str] = None
    cpu: Optional[str] = None
    memory: Optional[str] = None

class AppContext(BaseModel):
    package_name: str
    version_name: Optional[str] = None
    version_code: Optional[int] = None
    build_hash: Optional[str] = None
    channel: Optional[str] = None

class NetworkContext(BaseModel):
    type: str = Field(default="wifi", description="wifi | mobile | offline")
    bandwidth: Optional[str] = None
    latency: Optional[str] = None

class Context(BaseModel):
    """
    Reproducible experiment environment definition.
    """
    device: DeviceContext
    app: AppContext
    network: Optional[NetworkContext] = None
    env_vars: Dict[str, Any] = Field(default_factory=dict)

# ==========================================
# 4. Streams Layer (Core Data Flow)
# ==========================================

class BehaviorItem(BaseModel):
    ts: datetime
    source: StreamSource
    action: str
    target: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    page: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

class PerformanceItem(BaseModel):
    ts: datetime
    cpu_percent: float
    mem_pss_mb: float
    fps: Optional[float] = None
    net_tx_kb: Optional[float] = None
    net_rx_kb: Optional[float] = None
    gpu_usage: Optional[float] = None

class EventItem(BaseModel):
    ts: datetime
    type: str = Field(..., description="CRASH | ANR | EXCEPTION | WARNING")
    source: str
    level: EventLevel
    message: str
    stack_trace: Optional[str] = None

class LogItem(BaseModel):
    ts: datetime
    level: str
    tag: str
    msg: str

class Streams(BaseModel):
    """
    The blood of the system: Time-series data streams.
    """
    behavior_stream: List[BehaviorItem] = Field(default_factory=list)
    performance_stream: List[PerformanceItem] = Field(default_factory=list)
    event_stream: List[EventItem] = Field(default_factory=list)
    log_stream: List[LogItem] = Field(default_factory=list)

# ==========================================
# 5. Analysis Layer (Intelligence)
# ==========================================

class Correlation(BaseModel):
    event_id: str
    related_perf_indices: List[int]
    pattern: str
    confidence: float

class Analysis(BaseModel):
    """
    AI / Rule Engine Entry Layer.
    """
    correlations: List[Correlation] = Field(default_factory=list)
    patterns: List[str] = Field(default_factory=list, description="Detected patterns e.g. MEMORY_LEAK")
    risk_tags: List[str] = Field(default_factory=list, description="Risk assessments e.g. P0_CRASH_RISK")

# ==========================================
# 6. Summary Layer (Decision View)
# ==========================================

class Summary(BaseModel):
    """
    Management / CI / Reporting View.
    """
    result: str = Field(..., description="PASS | FAIL | WARNING")
    crash_count: int = 0
    anr_count: int = 0
    max_mem_mb: float = 0.0
    avg_fps: float = 0.0
    duration_sec: float = 0.0
    quality_score: float = 0.0
    stability_level: str = "UNKNOWN"
    perf_level: str = "UNKNOWN"
    conclusion: Optional[str] = None

# ==========================================
# 7. Artifacts Layer (Assets)
# ==========================================

class Artifacts(BaseModel):
    """
    File assets.
    """
    screenshots: List[str] = Field(default_factory=list)
    videos: List[str] = Field(default_factory=list)
    reports: List[str] = Field(default_factory=list)
    raw_logs: List[str] = Field(default_factory=list)

# ==========================================
# 8. Root Object: TestRuntime
# ==========================================

class TestRuntime(BaseModel):
    """
    The System Core Object.
    A single test execution instance.
    """
    runtime_id: str
    task_type: TaskType
    status: RuntimeStatus
    
    meta: Meta
    context: Context
    streams: Streams = Field(default_factory=Streams)
    analysis: Analysis = Field(default_factory=Analysis)
    summary: Optional[Summary] = None
    artifacts: Artifacts = Field(default_factory=Artifacts)

    class Config:
        use_enum_values = True
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

# ==========================================
# 9. Module Interface (Producer Contract)
# ==========================================

class ModuleProducer(BaseModel):
    """
    Interface for modules to bind to a runtime.
    """
    module_name: str
    module_type: str = Field(..., description="behavior_producer | perf_producer | event_producer")
    output_streams: List[str]
    runtime_binding: str  # runtime_id
