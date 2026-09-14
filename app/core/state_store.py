"""
全局状态存储 - 保存系统运行时的共享状态

各模块读写自己的状态命名空间，避免直接互相调用。
"""
import threading
import time
from typing import Any, Dict, Optional


class StateStore:
    """
    线程安全的键值状态存储

    支持命名空间隔离，每个模块有自己的命名空间。
    支持状态变更通知。
    """

    def __init__(self):
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._listeners: Dict[str, list] = {}

    def set(self, namespace: str, key: str, value: Any):
        """设置状态值"""
        with self._lock:
            if namespace not in self._data:
                self._data[namespace] = {}
            old_value = self._data[namespace].get(key)
            self._data[namespace][key] = value
            self._data[namespace][f"{key}__updated_at"] = time.time()

        # 通知监听器（在锁外执行）
        if old_value != value:
            self._notify(namespace, key, old_value, value)

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """获取状态值"""
        with self._lock:
            ns_data = self._data.get(namespace, {})
            return ns_data.get(key, default)

    def get_namespace(self, namespace: str) -> Dict[str, Any]:
        """获取整个命名空间的状态"""
        with self._lock:
            return self._data.get(namespace, {}).copy()

    def has(self, namespace: str, key: str) -> bool:
        """检查键是否存在"""
        with self._lock:
            return key in self._data.get(namespace, {})

    def delete(self, namespace: str, key: str):
        """删除状态值"""
        with self._lock:
            if namespace in self._data and key in self._data[namespace]:
                del self._data[namespace][key]

    def clear_namespace(self, namespace: str):
        """清空命名空间"""
        with self._lock:
            self._data.pop(namespace, None)

    def keys(self, namespace: str) -> list:
        """获取命名空间所有键"""
        with self._lock:
            return [k for k in self._data.get(namespace, {}).keys()
                    if not k.endswith("__updated_at")]

    def on_change(self, namespace: str, key: str, callback):
        """
        注册状态变更监听器
        callback(namespace, key, old_value, new_value)
        """
        listener_key = f"{namespace}:{key}"
        with self._lock:
            if listener_key not in self._listeners:
                self._listeners[listener_key] = []
            self._listeners[listener_key].append(callback)

    def _notify(self, namespace: str, key: str, old_value: Any, new_value: Any):
        """通知状态变更"""
        listener_key = f"{namespace}:{key}"
        with self._lock:
            listeners = list(self._listeners.get(listener_key, []))

        for callback in listeners:
            try:
                callback(namespace, key, old_value, new_value)
            except Exception as e:
                print(f"[StateStore] 监听器异常: {e}")

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """获取全部状态快照"""
        with self._lock:
            result = {}
            for ns, data in self._data.items():
                result[ns] = {k: v for k, v in data.items()
                               if not k.endswith("__updated_at")}
            return result


# 全局状态存储单例
_state_store: Optional[StateStore] = None


def get_state_store() -> StateStore:
    """获取全局状态存储单例"""
    global _state_store
    if _state_store is None:
        _state_store = StateStore()
    return _state_store


# 常用命名空间常量
class StateNamespace:
    MUSIC = "music"
    GAME = "game"
    ALARM = "alarm"
    VOICE = "voice"
    LCU = "lcu"
    PLAYER = "player"
    SPEECH = "speech"
    SYSTEM = "system"
    UI = "ui"
