"""ComboManager：一键连招对外接口。

职责：
- 加载 combos.yaml（配置驱动，一期只读）
- 启动/停止全局键盘监听
- 维护总开关（默认关闭，UI 手动开启）
- 从 Ongoing 服务实时解析"我方玩家自己用的英雄 ID"（只读，不改 Ongoing）
- 按键 → 查连招 → 执行；自我触发屏蔽（is_executing + 执行后 grace 窗口）
- 使用频率记录（data/combo_usage.json，供 UI 排序；不污染 combos.yaml）

红线遵守：纯键盘、无鼠标、无注入、无内存读取、无整连招重试、默认关闭。
"""
from __future__ import annotations

import json
import os
import threading
import time

from .config import ComboConfig
from .executor import ComboExecutor, KeySender
from .listener import ComboListener
from .models import ChampionCombos
from .win_focus import is_lol_foreground

try:
    from app.core.path_resolve import (
        PROJECT_ROOT,  # 兼容保留
        USER_DATA_DIR,  # R1: 用户可写数据走 LOCALAPPDATA
    )
except ImportError:
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    USER_DATA_DIR = PROJECT_ROOT
USAGE_PATH = os.path.join(str(USER_DATA_DIR), "data", "combo_usage.json")

# 连招执行结束后仍忽略按键的窗口（秒）：拦住"程序自己发的键"的延迟回调，
# 防止发出的键恰好也是 trigger 时自我触发死循环。200ms 是防御回调延迟与手感的平衡点。
EXEC_GRACE_S = 0.2


class UsageStore:
    """常用英雄使用频率（本地 json，按 champion_id 计数）。"""

    def __init__(self, path: str | None = None):
        self.path = path or USAGE_PATH
        self._data: dict[str, int] = {}
        self._lock = threading.Lock()
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                    self._data = {str(k): int(v or 0) for k, v in raw.items()}
        except Exception:
            self._data = {}

    def get(self, champion_id: int) -> int:
        return self._data.get(str(int(champion_id)), 0)

    def bump(self, champion_id: int) -> None:
        cid = str(int(champion_id))
        with self._lock:
            self._data[cid] = self._data.get(cid, 0) + 1
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass


class ComboManager:
    def __init__(self, config_path: str | None = None,
                 key_sender: KeySender | None = None,
                 ongoing_service=None,
                 usage_path: str | None = None):
        self.config: ComboConfig = ComboConfig.load(config_path)
        self._executor = ComboExecutor(key_sender)
        self._listener = ComboListener(self._handle_key)
        self._ongoing_service = ongoing_service
        self._usage = UsageStore(usage_path)
        self._enabled = False
        # 仅 LoL 前台时触发（默认开；一期不持久化，每次启动默认开）
        self._window_gate_enabled = True
        self._current_champion_id: int | None = None
        self._current_champion_name: str | None = None
        self._ignore_until: float = 0.0
        self._last_exec_time: dict = {}  # trigger -> 上次执行时间戳（冷却保护）
        self._log_lock = threading.Lock()  # 保护切换日志不重复刷屏
        # 前台检测缓存（200ms，monotonic）：钩子回调里避免每次按键都调 win32/psutil
        self._fg_cache: bool | None = None
        self._fg_cache_ts: float = 0.0
        # 当前英雄解析缓存（5s，monotonic）：对局内 championId 固定，避免每次按键都查 ongoing
        self._champ_cache: ChampionCombos | None = None
        self._champ_cache_ts: float = 0.0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
        print("[Combo] 已停止")

    # ---------- 开关 ----------

    def set_enabled(self, enabled: bool) -> bool:
        self._enabled = bool(enabled)
        print(f"[Combo] 总开关: {'开' if self._enabled else '关'}")
        return self._enabled

    def is_enabled(self) -> bool:
        return self._enabled

    def is_listener_running(self) -> bool:
        """区分总开关与底层键盘监听器是否实际可用。"""
        return self._listener.is_running()

    def set_window_gate(self, enabled: bool) -> bool:
        """仅 LoL 前台时触发（默认开，一期不持久化）。"""
        self._window_gate_enabled = bool(enabled)
        self._fg_cache = None          # 门控变化，前台缓存立即失效
        self._fg_cache_ts = 0.0
        print(f"[Combo] 仅 LoL 前台触发: {'开' if self._window_gate_enabled else '关'}")
        return self._window_gate_enabled

    def is_window_gate_enabled(self) -> bool:
        return self._window_gate_enabled

    # ---------- UI 查询接口 ----------

    def get_champions(self):
        """全量已配置连招的英雄（UI 英雄列表数据源之一）。"""
        return list(self.config.champions)

    def find_champion_by_id(self, champion_id):
        return self.config.find_by_id(champion_id)

    def get_usage(self, champion_id: int) -> int:
        return self._usage.get(champion_id)

    def bump_usage(self, champion_id: int) -> None:
        self._usage.bump(champion_id)

    # ---------- 配置保存 / 重载（二期：UI 编辑后写回并立即生效） ----------

    def reload_config(self) -> None:
        """重新加载 combos.yaml（替换 self.config，无需重启）。"""
        self.config = ComboConfig.load(self.config.path)
        print("[Combo] 配置已重新加载（无需重启）")

    def save_config(self) -> bool:
        """将当前内存配置序列化写回 combos.yaml，成功后 reload。

        正在执行的连招不打断（executor 持有 combo 快照，自然跑完）。
        """
        ok = self.config.save()
        if ok:
            self.reload_config()
        return ok

    def add_champion(self, name: str, champion_id: int,
                     aliases: list | None = None):
        """添加新英雄（连招为空）并保存。已存在返回 None，不重复添加。"""
        cid = int(champion_id)
        if not name or self.config.find_by_id(cid) is not None:
            return None
        cc = ChampionCombos(
            name=str(name).strip(),
            champion_id=cid,
            aliases=[str(a).strip() for a in (aliases or []) if str(a).strip()],
        )
        self.config.champions.append(cc)
        self.config.by_id[cid] = cc
        self.config.aliases.setdefault(cc.name.strip().lower(), cc)
        if self.save_config():
            return self.config.find_by_id(cid)  # reload 后的新对象
        return None

    def remove_champion(self, champion_id: int) -> bool:
        """删除英雄及其全部连招（不设保护，允许删任何英雄），保存并立即生效。

        正在执行的连招不打断（executor 持有 combo 快照，自然跑完）。
        """
        cc = self.config.find_by_id(int(champion_id))
        if cc is None:
            return False
        self.config.champions.remove(cc)
        # 同步清理索引，避免 reload 前 find_by_id/find_by_alias 命中已删英雄
        self.config.by_id.pop(int(champion_id), None)
        for a in [cc.name] + list(cc.aliases):
            key = (a or "").strip().lower()
            if key and self.config.aliases.get(key) is cc:
                self.config.aliases.pop(key, None)
        return self.save_config()

    def current_champion_name(self) -> str | None:
        """当前对局中我方玩家使用的英雄名（自动识别）。"""
        self._resolve_self_champion()
        return self._current_champion_name

    def current_champion_id(self) -> int | None:
        self._resolve_self_champion()
        return self._current_champion_id

    # ---------- 核心处理 ----------

    def _handle_key(self, key: str) -> None:
        """监听回调：总开关 / 前台门控 / 当前英雄 / 执行中 / grace 窗口 / trigger 匹配。"""
        if not self._enabled:
            return
        # 前台门控：开关开启时只认 LoL 游戏本体前台；检测失败放行（宁误触发不吞按键）
        if self._window_gate_enabled:
            # 前台检测 200ms 缓存（monotonic）：钩子回调不做实时 win32/psutil 查询
            if time.monotonic() - self._fg_cache_ts < 0.2:
                fg = self._fg_cache
            else:
                try:
                    fg = is_lol_foreground()
                    self._fg_cache = fg
                    self._fg_cache_ts = time.monotonic()
                except Exception:
                    print("[Combo] 前台检测失败，已放行")
                    fg = True
            if not fg:
                return
        now = time.time()
        if now < self._ignore_until:
            return  # 自己发的键的延迟回调窗口
        if self._executor.is_executing:
            return  # 执行中，防自我触发
        champ = self._resolve_self_champion()
        if champ is None:
            return  # 没进对局 / 英雄未知
        combo = champ.find_by_trigger(key)
        if combo is None:
            return
        # 冷却保护：使用 time.monotonic() 防系统时钟跳变（时间源铁律）
        mono_now = time.monotonic()
        combo_id = getattr(combo, "id", getattr(combo, "trigger", "default"))
        cooldown_sec = float(getattr(combo, "cooldown_ms", 1000) or 1000) / 1000.0
        if mono_now - self._last_exec_time.get(combo_id, 0.0) < cooldown_sec:
            return  # 处于冷却期，直接忽略，绝不重发
        self._last_exec_time[combo_id] = mono_now
        print(f"[Combo] 触发 {champ.name} [{combo.trigger}] {combo.note}")
        self._spawn_combo_exec(combo)

    def _spawn_combo_exec(self, combo) -> None:
        """连招执行移出键盘钩子线程（Windows 低级钩子回调 >300ms 会被静默移除）。
        钩子回调只做识别 + 派发（<1ms 返回）；执行/延时/收尾在新 daemon 线程。
        异常在线程内捕获，不影响钩子线程与后续按键。"""

        def _run():
            try:
                ok = self._executor.run(combo)
                if ok:
                    # 执行结束后再放开窗口：覆盖"程序自己发的键"的延迟回调
                    self._ignore_until = time.time() + EXEC_GRACE_S
                    print(f"[Combo] 连招执行完毕（{len(combo.sequence)} 步）: {combo.summary}")
            except Exception as e:
                print(f"[Combo] 连招执行异常: {e}")

        threading.Thread(target=_run, daemon=True, name="combo-exec").start()

    def _resolve_self_champion(self) -> ChampionCombos | None:
        """从 Ongoing 服务取"我方玩家自己"的 champion_id → ChampionCombos。

        只读访问 ongoing_service._session（CurrentGameSession.my_team 中
        is_self=True 的玩家）。对局结束 session 被清空 → 返回 None。
        5s 缓存（monotonic）：对局内 championId 固定，命中即不查 ongoing（钩子回调轻量化）。
        """
        cid = self._read_self_champion_id()
        # 缓存命中：英雄 id 非空且未变（_read_self_champion_id 是内存读，微秒级；
        # 缓存避免的是 find_by_id 组合解析重活）。cid 为空/变化 → 立即失效，
        # 保证"对局结束"第一时间清掉旧英雄，不残留 5s 假缓存。
        if (self._champ_cache is not None
                and cid is not None
                and cid == self._current_champion_id
                and time.monotonic() - self._champ_cache_ts < 5.0):
            return self._champ_cache
        with self._log_lock:
            if cid != self._current_champion_id:
                changed = True
                self._current_champion_id = cid
                self._current_champion_name = None
                if cid:
                    cc = self.config.find_by_id(cid)
                    self._current_champion_name = cc.name if cc else f"id={cid}"
            else:
                changed = False
        if changed:
            if cid:
                print(f"[Combo] 当前英雄切换: {self._current_champion_name}")
            else:
                print("[Combo] 对局结束，当前英雄已清空")
        if not cid:
            self._champ_cache = None
            self._champ_cache_ts = 0.0
            return None
        cc = self.config.find_by_id(cid)
        self._champ_cache = cc
        self._champ_cache_ts = time.monotonic()
        return cc

    def _read_self_champion_id(self) -> int | None:
        svc = self._ongoing_service
        if svc is None:
            return None
        try:
            # 用 Ongoing 服务的公开访问方法 get_session()，不读私有属性
            getter = getattr(svc, "get_session", None)
            if getter is None:
                return None
            session = getter()
            if session is None:
                return None
            for p in getattr(session, "my_team", None) or []:
                if getattr(p, "is_self", False):
                    cid = getattr(p, "champion_id", 0) or 0
                    return int(cid) if cid else None
        except Exception:
            return None
        return None
