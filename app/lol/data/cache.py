"""本地缓存读写（原子写）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.path_resolve import PROJECT_ROOT

DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "lol_data"


def load_json(cache_dir: Path, name: str) -> Optional[Dict[str, Any]]:
    p = Path(cache_dir) / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_json(cache_dir: Path, name: str, data: Dict[str, Any]) -> bool:
    """原子写：.tmp + replace。"""
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False
