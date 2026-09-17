"""EventLogReader - 只读扫描 JSONL 事件日志"""
import json
import os
import glob
from typing import List, Dict, Any, Optional


class EventLogReader:
    """Read-only reader for data/events/*.jsonl.
    Filters events by payload.local_match_id. Does not modify any files.
    """

    def __init__(self, log_dir: str = "data/events"):
        self.log_dir = log_dir

    def read_for_match(self, local_match_id: str) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.log_dir):
            return []
        results: List[Dict[str, Any]] = []
        paths = sorted(glob.glob(os.path.join(self.log_dir, "*.jsonl")))
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        payload = entry.get("payload") or {}
                        if payload.get("local_match_id") == local_match_id:
                            results.append({
                                "event_type": entry.get("event_type"),
                                "timestamp": entry.get("timestamp"),
                                "payload": payload,
                            })
            except Exception:
                continue
        return results
