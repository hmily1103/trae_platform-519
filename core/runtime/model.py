from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional
import uuid
import time

class RuntimeStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class RuntimeObject:
    """
    The Core Atom of the Thunderstone Platform.
    Every execution (Monkey, Reboot, Stress, etc.) is a RuntimeObject.
    """
    runtime_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Untitled Task"
    module: str = "unknown"  # e.g., 'reboot', 'monkey', 'server_stress'
    status: RuntimeStatus = RuntimeStatus.PENDING
    
    # Lifecycle Timestamps
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    
    # Context (Input Parameters)
    context: Dict[str, Any] = field(default_factory=dict)
    
    # Result (Output Data)
    result: Dict[str, Any] = field(default_factory=dict)
    
    # Owner (User or System)
    owner: str = "system"

    def to_dict(self):
        data = asdict(self)
        data['status'] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data):
        # Handle Enum conversion
        if 'status' in data and isinstance(data['status'], str):
            data['status'] = RuntimeStatus(data['status'])
        return cls(**data)
