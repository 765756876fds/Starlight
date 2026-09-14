"""
SpeechQueue - 语音优先级队列

职责：
- 按优先级调度语音播报
- 高优先级打断低优先级
- 同优先级 FIFO
- event_id 去重
- TTL 过期清理
- 队列上限保护

不负责：
- 生成播报文本（SpeechJudge 负责）
- 实际播放（TTSController 负责）
"""
import heapq
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# 优先级常量
PRIORITY_S = 0
PRIORITY_A = 1
PRIORITY_B = 2

MAX_QUEUE_SIZE = 10


@dataclass(order=False)
class SpeechItem:
    """
    语音播报项

    不实现自身 ordering，由 heapq 元组控制顺序。
    """
    text: str
    priority: int
    expire_at: float
    category: str = "general"
    event_id: str = ""
    interruptible: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    # 内部状态
    status: str = "pending"  # pending / playing / cancelled / finished / expired


class SpeechQueue:
    """
    语音优先级队列

    线程模型：
    - 1 个 worker 线程
    - 调用线程 enqueue() 快速返回
    - worker 负责消费队列、调用 TTS
    - Lock + Condition 同步
    """

    def __init__(self, tts_controller: Any):
        """
        Args:
            tts_controller: TTSController 实例
        """
        self.tts = tts_controller

        # 队列状态
        self._pending: List[Tuple[int, int, SpeechItem]] = []  # (priority, seq, item)
        self._current: Optional[SpeechItem] = None
        self._sequence = 0

        # 去重
        self._seen_event_ids: set = set()

        # 线程控制
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        """启动 worker 线程"""
        if self._running:
            return

        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="speech-queue-worker",
        )
        self._worker_thread.start()

    def shutdown(self):
        """停止 worker，等待退出"""
        self._running = False

        with self._condition:
            self._condition.notify_all()

        if self._worker_thread:
            self._worker_thread.join(timeout=2)
            self._worker_thread = None

        # 停止当前播放
        self.stop_current()

    def enqueue(self, item: SpeechItem) -> bool:
        """
        加入队列

        Returns:
            True = 成功入队
            False = 去重/过期/溢出 失败
        """
        now = time.time()

        # 入队时检查过期
        if now >= item.expire_at:
            return False

        with self._lock:
            # 去重检查
            if item.event_id and item.event_id in self._seen_event_ids:
                return False

            # 队列上限检查
            if len(self._pending) >= MAX_QUEUE_SIZE:
                # 新事件优先级 >= 最低优先级 → DROP
                lowest_priority = max(p for p, _, _ in self._pending)
                if item.priority >= lowest_priority:
                    return False

                # 新事件更高 → 弹出最低优先级（同优先级弹最旧的）
                # 找到要删除的项
                to_remove_idx = -1
                to_remove_seq = -1
                for i, (p, seq, it) in enumerate(self._pending):
                    if p == lowest_priority:
                        if seq > to_remove_seq:
                            to_remove_seq = seq
                            to_remove_idx = i

                if to_remove_idx >= 0:
                    removed = self._pending.pop(to_remove_idx)
                    removed_item = removed[2]
                    removed_item.status = "expired"
                    # 解除 dedup
                    if removed_item.event_id and removed_item.event_id in self._seen_event_ids:
                        self._seen_event_ids.remove(removed_item.event_id)

            # 入堆
            self._sequence += 1
            heapq.heappush(self._pending, (item.priority, self._sequence, item))

            # 加入 dedup
            if item.event_id:
                self._seen_event_ids.add(item.event_id)

            # 检查是否需要打断当前播放
            self._check_interrupt_needed(item)

            # 通知 worker
            self._condition.notify()

        return True

    def _check_interrupt_needed(self, new_item: SpeechItem):
        """检查新事件是否需要打断当前播放（在锁内调用）"""
        if self._current is None:
            return

        # 新优先级更高（数字更小）
        if new_item.priority >= self._current.priority:
            return

        # 当前项不可打断
        if not self._current.interruptible:
            return

        # 需要打断：标记当前为 cancelled
        self._current.status = "cancelled"

        # 通知 worker 去停止当前播放
        # worker 在 wait_finished 期间会检查这个标志
        self._interrupt_requested = True

    # 打断请求标志（在锁内访问）
    _interrupt_requested = False

    def clear_pending(self):
        """清空 pending 队列，不停止当前播放"""
        with self._lock:
            # 清空 pending，解除对应 dedup
            for _, _, item in self._pending:
                if item.event_id and item.event_id in self._seen_event_ids:
                    self._seen_event_ids.remove(item.event_id)
            self._pending.clear()

    def stop_current(self):
        """停止当前播放，pending 不受影响"""
        # 标记当前为 cancelled
        with self._lock:
            if self._current:
                self._current.status = "cancelled"

        # 调用 TTS 停止（不在锁内）
        self.tts.stop_playback()

    def clear_all(self):
        """停止当前 + 清空 pending"""
        self.stop_current()
        self.clear_pending()

    def size(self) -> int:
        """pending 队列长度"""
        with self._lock:
            return len(self._pending)

    def is_empty(self) -> bool:
        """pending 是否为空"""
        return self.size() == 0

    def get_current(self) -> Optional[SpeechItem]:
        """获取当前正在播放的项"""
        with self._lock:
            return self._current

    def is_running(self) -> bool:
        """worker 是否在运行"""
        return self._running

    def _worker_loop(self):
        """worker 主循环"""
        while self._running:
            try:
                self._worker_iteration()
            except Exception as e:
                print(f"[SpeechQueue] worker 错误: {e}")
                time.sleep(0.5)

    def _worker_iteration(self):
        """一次迭代"""
        # 等待有 pending 或打断请求
        with self._condition:
            # 等待：有 pending 或者有打断请求 或者 停止
            while self._running and not self._pending and not self._interrupt_requested:
                self._condition.wait(timeout=0.5)

            if not self._running:
                return

            # 处理打断请求
            if self._interrupt_requested:
                self._interrupt_requested = False
                # 停止当前播放（不在锁内）
                self._current_status = "stopping"
                # 先退出锁，再调用 TTS
                pass

        # 停止当前播放（如果有打断请求）
        if self._current and self._current.status == "cancelled":
            self.tts.stop_playback()
            # 等待停止完成
            self._wait_tts_idle(timeout=1.0)

        # 取出最高优先级且未过期的 item
        item = None
        with self._lock:
            while self._pending:
                priority, seq, candidate = heapq.heappop(self._pending)

                # 检查过期
                if time.time() >= candidate.expire_at:
                    candidate.status = "expired"
                    # 解除 dedup
                    if candidate.event_id and candidate.event_id in self._seen_event_ids:
                        self._seen_event_ids.remove(candidate.event_id)
                    continue

                item = candidate
                break

            if item is None:
                self._current = None
                return

            # 设置为当前
            item.status = "playing"
            self._current = item

        # 播放（不在锁内）
        self.tts.speak(item.text)

        # 等待播放完成，期间定期检查是否有更高优先级打断
        self._wait_for_playback_with_interrupt_check(item)

        # 播放完成
        with self._lock:
            if self._current is item:
                self._current = None
            item.status = "finished"

    def _wait_for_playback_with_interrupt_check(self, item: SpeechItem):
        """
        等待播放完成，每 100ms 检查一次是否被打断
        """
        while True:
            # 等待 100ms
            time.sleep(0.1)

            # 检查是否被打断
            with self._lock:
                if item.status == "cancelled":
                    # 退出锁，停止 TTS
                    break

                # 检查是否有更高优先级在 pending
                if self._pending:
                    top_priority = self._pending[0][0]
                    if top_priority < item.priority and item.interruptible:
                        # 有更高优先级，需要打断
                        item.status = "cancelled"
                        # 退出锁，停止 TTS
                        break

            # 检查 TTS 是否还在播放
            if not self.tts.is_playing():
                return

        # 停止当前播放
        self.tts.stop_playback()
        self._wait_tts_idle(timeout=1.0)

    def _wait_tts_idle(self, timeout: float = 1.0):
        """等待 TTS 回到空闲"""
        start = time.time()
        while time.time() - start < timeout:
            if not self.tts.is_playing():
                return
            time.sleep(0.05)
