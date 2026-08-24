import json
import os
import re
import threading
import time


SAFE_ID = re.compile(r"^pt_[a-zA-Z0-9_-]{1,80}$")


class PrecisionTestStore:
    def __init__(self, base_dir=None):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.base_dir = base_dir or os.path.join(root, "data", "precision_test")
        self.index_path = os.path.join(self.base_dir, "index.json")
        self.lock = threading.RLock()
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, analysis_id):
        if not SAFE_ID.match(str(analysis_id or "")):
            raise ValueError("非法 analysis_id")
        return os.path.join(self.base_dir, f"{analysis_id}.json")

    def save(self, model):
        analysis_id = model["analysis_id"]
        with self.lock:
            path = self._path(analysis_id)
            tmp_path = f"{path}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(model, file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            index = self._read_index()
            row = {
                "analysis_id": analysis_id,
                "version": model.get("change", {}).get("version"),
                "summary": model.get("change", {}).get("summary"),
                "decision": model.get("quality_gate", {}).get("decision"),
                "coverage_rate": model.get("quality_gate", {}).get("coverage_rate"),
                "created_at": model.get("created_at"),
                "updated_at": model.get("updated_at"),
            }
            rows = [item for item in index if item.get("analysis_id") != analysis_id]
            rows.append(row)
            rows.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
            index_tmp = f"{self.index_path}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
            with open(index_tmp, "w", encoding="utf-8") as file:
                json.dump(rows[:200], file, ensure_ascii=False, indent=2)
            os.replace(index_tmp, self.index_path)
        return analysis_id

    def get(self, analysis_id):
        try:
            with open(self._path(analysis_id), "r", encoding="utf-8") as file:
                value = json.load(file)
                return value if isinstance(value, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def list(self, limit=50):
        return self._read_index()[:max(1, min(int(limit), 200))]

    def _read_index(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as file:
                value = json.load(file)
                return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError):
            return []


_STORE = None
_LOCK = threading.RLock()


def get_precision_test_store():
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = PrecisionTestStore()
        return _STORE
