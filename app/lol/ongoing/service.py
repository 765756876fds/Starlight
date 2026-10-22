"""OngoingGameService：独立轮询 gameflow-phase，不依赖 automation 事件。"""
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable

from app.core.logger import get_logger

logger = get_logger()

from .deobfuscate import deobfuscate_puuid
from .diagnostic import ChampSelectDiagnostic
from .gsm_provider import merge_gsm_players
from .loader import OngoingGameLoader
from .models import PlayerSnapshot
from .session import CurrentGamePlayer, CurrentGameSession

# liveclientdata 玩家对象没有 championId 字段（只有 championName / rawChampionName），
# 需要用名称反查 cid 才能与 gameflow 身份 cell 的 championId 对齐匹配。
_CATALOG = None
_CATALOG_LOCK = threading.Lock()


def _resolve_live_champion_id(p) -> int:
    """从 live 玩家对象解析 championId：优先已有字段，缺失时用名称反查 ChampionCatalog。

    容错链：rawChampionName 专用解析 → 英文名 → 中文名 → 去空格/小写兜底；
    全部未命中时 logger.warning 记录名字，便于后续补充别名库。
    """
    cid = getattr(p, "champion_id", 0) or 0
    if cid:
        return int(cid)
    global _CATALOG
    if _CATALOG is None:
        with _CATALOG_LOCK:
            if _CATALOG is None:
                try:
                    from app.lol.data.champion_catalog import ChampionCatalog
                    _CATALOG = ChampionCatalog()
                except Exception:
                    _CATALOG = False
    if not _CATALOG:
        return 0
    raw = (getattr(p, "raw_champion_name", "") or "").strip()
    zh = (getattr(p, "champion_name", "") or "").strip()
    en = raw.split("_")[-1] if raw else ""  # "game_character_displayname_MissFortune" → "MissFortune"
    c = None
    if raw:
        # 专用解析（rawChampionName 前缀剥离）优先，失败再退英文名
        c = _CATALOG.resolve_raw_champion_name(raw) or _CATALOG.get_by_name(en)
    if c is None and en:
        c = _CATALOG.get_by_name(en)
    if c is None and zh:
        c = _CATALOG.get_by_name(zh)
    if c is None and en:
        # 去空格 + 小写兜底（如 "Twisted Fate" → "twistedfate"，目录含 name_en.lower() 键）
        c = _CATALOG.get_by_name(en.replace(" ", "").lower())
    if c is None:
        logger.warning(f"[Ongoing] 2999 玩家英雄名未命中目录: raw={raw!r} zh={zh!r} en={en!r}")
    return c.cid if c else 0


def _build_direct_getter(lcu):
    """优先独立 requests.Session 直连 LCU，绕开共享 LCUConnection 断连窗口；退化共享 lcu.get。

    主进程任一请求异常会触发共享连接 _disconnect()，重连窗口内 lcu.get 全部返回 None，
    导致 loader 的 summoner/ranked 查询偶发失败（队友 ID=#、段位= -、等级错位）。
    惰性直连：dget 每次实时取 lcu 当前 base_url/_auth_token，
    避免构造时序早于 lcu discovery（_port/_auth_token 未就绪）导致直连永久失效。
    """
    if lcu is not None:
        try:
            import requests

            def dget(ep):
                base = getattr(lcu, "base_url", "") or ""
                token = getattr(lcu, "_auth_token", "") or ""
                if not (base and token):
                    # discovery 未完成：实时退化共享 lcu.get
                    return lcu.get(ep)
                try:
                    sess = requests.Session()
                    sess.auth = ("riot", token)
                    sess.verify = False
                    r = sess.get(f"{base}{ep}", timeout=5)
                    return r.json() if r.status_code in (200, 201, 204) else None
                except Exception:
                    return None

            return dget
        except Exception:
            pass
    return (lcu.get if lcu is not None else (lambda ep: None))


class OngoingGameService:
    def __init__(
        self,
        lcu=None,
        on_event: Callable[[str, dict], None] | None = None,
        cache_ttl: float = 300.0,
        max_workers: int = 4,
        rps: float = 20.0,
        recent_n: int = 20, account_history_service=None,
        poll_interval: float = 1.0,
        snapshot_provider=None,
        gsm_provider=None,
        deobfuscate_enabled: bool = True,
    ):
        self._lcu = lcu
        self._gsm = gsm_provider  # 可选 GSM 补充身份源（None=不启用，纯 Gameflow 行为）
        self._deobfuscate_enabled = deobfuscate_enabled
        self._snapshot_provider = snapshot_provider
        self._on_event = on_event or (lambda c, d: None)
        # loader 的 summoner/ranked/history 查询全部走独立直连 getter，
        # 绕开共享 LCUConnection 的游戏内断连窗口（否则队友 ID/段位/等级查询偶发失败）
        getter = _build_direct_getter(lcu)
        self._dget = getter  # _tick 的 phase 读取复用同一独立直连（P0a）
        self._loader = OngoingGameLoader(
            lcu_get=getter,
            on_event=self._on_event,
            max_workers=max_workers,
            rps=rps,
            recent_matches_count=recent_n, fetch_count=50, account_history_service=account_history_service,
        )
        if getattr(self, "_loader", None):
            self._loader.on_name_resolved = self._on_player_name_resolved
        self._snapshots: list[PlayerSnapshot] = []
        # 当前对局 session（game_id 为生命周期锚点；players 由 gameflow teamOne/teamTwo 构建）
        self._session: CurrentGameSession | None = None
        self._current_session_key: str | None = None
        self._last_loaded_key: str | None = None
        self._loading_in_progress = False
        self._poll_interval = poll_interval
        # 历史战绩缺失自动重试状态（按 game_id 持久；离开对局清空，防腾讯服 gameId 复用串局）
        self._retry_state: dict = {}
        # session_created 防抖：已宣布过的 session 标识（离开对局清空，防 gameId 复用误判同局）
        self._announced_session_id = None
        # gameflow dump 日志去重：记录上次打印的 (gameMode/人数/puuid数)，相同内容不重复打印
        self._last_gf_dump = None
        # force 重载：retry 触发时置 True，下一次加载绕过 ERROR cache 真正重新请求
        self._force_reload = False
        # ChampSelect 完整 session 缓存：人机/匹配选人阶段 10 人含 puuid，
        # InProgress 阶段 liveclientdata 无 puuid，必须复用该缓存才能查到队友历史战绩
        self._champ_select_session: dict | None = None

        self._diag = ChampSelectDiagnostic(lcu.request_raw, on_event=self._on_event) if lcu is not None else None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ----- lifecycle -----
    def get_direct_getter(self):
        """返回独立直连 getter（绕开共享连接断连窗口）。"""
        return self._dget

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        if self._diag: self._diag.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._diag: self._diag.stop()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ----- poll loop -----
    def _run(self) -> None:
        while not self._stop_event.wait(self._next_interval()):
            try:
                self._tick()
            except Exception as e:
                self._on_event("ongoing.error", {"err": str(e)})

    def _next_interval(self) -> float:
        """进局初期动态加速：InProgress/GameStart 且仍有玩家 championId 未解析（2999 未齐）
        时 0.2s 轮询，尽快把英雄/名字渲染出来；其余情况用默认 poll_interval。"""
        if getattr(self, "_last_phase", "") in ("InProgress", "GameStart"):
            with self._lock:
                players = getattr(self._session, "players", []) if self._session is not None else []
            has_missing_champ = any(
                getattr(p, "champion_id", 0) == 0 for p in players
            )
            if has_missing_champ:
                return 0.2
        return self._poll_interval

    def _tick(self) -> None:
        if self._lcu is None:
            return
        lcu = self._lcu
        # P0a 断连熔断：连接不可用（_connected=False 或 _session 为空）→ 跳过本轮，
        # 不重建/不清空 session，等连接恢复后自然继续（防止"连接断"被误判成"对局结束"）。
        if not (getattr(lcu, "_connected", False) and getattr(lcu, "_session", None)):
            self._on_event("ongoing.conn_skip", {"connected": False})
            return
        # phase 优先独立直连（绕共享断连窗口）；_dget 未初始化成功则回退共享 lcu.get
        dget = getattr(self, "_dget", None) or lcu.get
        phase = dget("/lol-gameflow/v1/gameflow-phase")
        # 直连失败时复查共享连接：连接刚断 → 跳过本轮；连接正常 → 接口真返回 None（对局结束）
        if phase is None and not getattr(lcu, "_connected", False):
            self._on_event("ongoing.conn_skip", {"connected": False, "phase_none": True})
            return
        # 选人/加载/对局中加载玩家数据；其他阶段清空
        # GameStart/Reconnect 也纳入：gameflow teamOne/teamTwo 在加载界面即就绪（实测明文 puuid），
        # 进入对局即可显示全部玩家，无需等待 liveclientdata
        self._last_phase = phase  # 供 _run 动态加速轮询（InProgress/GameStart 且英雄未齐时 0.2s）
        if phase not in ("ChampSelect", "GameStart", "InProgress", "Reconnect"):
            with self._lock:
                if (self._current_session_key is not None or self._snapshots
                        or self._session is not None):
                    self._current_session_key = None
                    self._snapshots = []
                    self._session = None
                    self._loading_in_progress = False
                    # 跨局隔离：离开对局阶段即重置 last_loaded_key。
                    # 腾讯服实测 gameId 在相邻对局间会复用（两局同为 9009578125），
                    # 若保留旧 last_loaded_key，新局 key==旧 key 会误判"同一局"而跳过
                    # 强制重载，导致跨局复用上一局 PlayerSnapshot（history.cache_hit）。
                    self._last_loaded_key = None
                    # 离开对局：重置 session_created 防抖 + retry 计数。
                    # 腾讯服复用 gameId 的新局必须在此清空，否则被误判为同局
                    # （漏发 session_created / retry 计数不重置）。
                    self._announced_session_id = None
                    self._retry_state = {}
            return

        # 选人阶段缓存 champ-select session（仅作 gameflow 偶发为空的合理回退，非玩家集合来源）
        cs_session = None
        if phase == "ChampSelect":
            cs_session = self._lcu.get("/lol-champ-select/v1/session")
            if isinstance(cs_session, dict):
                # 缓存完整选人 session：10 人含 puuid，供 InProgress 阶段 gameflow 空时补自己
                self._champ_select_session = cs_session

        # 选人残留清理（2026-10-18 修 + 本修）：从上一把对局（InProgress/GameStart 等）直接
        # 切到新选人而未经过上面清理分支时（训练营重开 / phase 跳变 / 新选人 session
        # 构建失败——gameflow 未就绪 + deobfuscate 未完成），清空上一把 session，
        # 严禁 UI 显示上一把队友。
        # 本修：加"同局判定"，避免同一局选人期间每个 tick 都清空重建（同 gameId 的连续
        # ChampSelect tick 不清理、不重置 _loading_in_progress，消除选人阶段每秒
        # 清空→重建→force 重拉全部玩家的瀑布式重复加载）。
        # 清理仅当：① session 不是本次选人构建的（session.phase != "ChampSelect"，即上一把
        # InProgress/GameStart 残留或 phase 跳变）；或 ② cs session 已就绪且 gameId 与现有
        # session 不同（确属换局）。cs 暂不可用且 session 是本次选人构建的 → 保留（同局）。
        if phase == "ChampSelect" and self._session is not None:
            with self._lock:
                if self._session is not None:
                    cs_gid = (cs_session or {}).get("gameId") if isinstance(cs_session, dict) else None
                    # 仅在"确认换局"时才清理：cs 已带有效 gameId 且与现有 session 不同；
                    # cs 缺失/为 0（瞬态）不当作换局证据，避免同局选人期间误清重建。
                    diff_game_confirmed = (
                        bool(cs_gid)
                        and bool(self._session.game_id)
                        and cs_gid != self._session.game_id
                    )
                    same_game = (
                        self._session.phase == "ChampSelect"
                        and not diff_game_confirmed
                    )
                    if not same_game:
                        self._current_session_key = None
                        self._snapshots = []
                        self._session = None
                        self._loading_in_progress = False
                        self._last_loaded_key = None
                        self._announced_session_id = None
                        self._retry_state = {}

        me_puuid = None
        try:
            me = self._lcu.get("/lol-summoner/v1/current-summoner")
            me_puuid = me.get("puuid") if isinstance(me, dict) else None
        except Exception:
            me_puuid = None

        # 身份主源 = gameflow session.gameData.teamOne/teamTwo
        # （LCU 官方明文 puuid + summonerId + championId；
        #   实测：匹配局 10/10 明文 puuid，人机局己方 5/5，bot 不在 session）
        # ChampSelect 阶段 gameflow 未就绪时不构建玩家集合（players 主源=gameflow，
        # 禁止以 2999/champ-select 充当玩家集合来源），等 GameStart 后 gameflow 就绪再构建。
        session = self._build_gameflow_session(me_puuid)
        if session is not None:
            # GSM 补充身份源（仅 GameStart/InProgress/Reconnect 启用；ChampSelect 不请求）：
            # Gameflow 仍是主源，GSM 只按 puuid 追加 Gameflow 完全没有的玩家（不覆盖）。
            session = self._supplement_gsm(session, phase, me_puuid)
            # liveclientdata 无 puuid：身份归属由 gameflow 决定，
            # 实时 KDA/位置/等级按 (team, championId) 可关联字段合并进对应 cell
            self._merge_live_fields(session)
        elif phase == "ChampSelect":
            # 选人阶段提前显示（feature flag：deobfuscate_enabled）：
            # gameflow 未就绪时，cs session 队友为 HIDDEN 混淆（summonerId=0/puuid=""/obfuscatedPuuid 有值），
            # 用 deobfuscate_puuid() 本地 XOR 还原 → 写回 cell.puuid → 走现有 intel 链路查战绩。
            # 合规边界：只处理 LCU 公开返回的 obfuscatedPuuid 字段，纯本地 XOR，无注入/内存/hook；
            # 反解失败（返回空）保持原样（HIDDEN 无身份，UI 显示占位）。
            # 反解包 try/except：任何异常绝不影响主流程（session 保持 None → 原 return 路径）。
            if self._deobfuscate_enabled:
                try:
                    cs = self._champ_select_session
                    if cs and me_puuid:
                        session = self._deobfuscate_champ_select(cs, me_puuid)
                except Exception:
                    session = None
        elif phase != "ChampSelect":
            # 非 ChampSelect 阶段（GameStart/InProgress 等）：gameflow 未就绪时走同局
            # ChampSelect 缓存兜底。（ChampSelect 阶段已由上方新分支处理）
            cs = self._champ_select_session
            if cs and me_puuid:
                cs_puuids = set(
                    (cell.get("puuid") or "")
                    for cell in (cs.get("myTeam") or []) + (cs.get("theirTeam") or [])
                )
                if me_puuid in cs_puuids and len(cs_puuids) >= 5:
                    session = cs  # 同一局的选人缓存
                    self._on_event("ongoing.session_source", {"source": "champ_select_cache", "players": len(cs_puuids)})
        if not isinstance(session, dict):
            return
        key = self._session_key(session)
        _same_session = False
        _need_load = False
        with self._lock:
            if key == self._current_session_key:
                _same_session = True
                # 同一局：更新 phase/updated_at；身份升级——gameflow 比现有 session 更完整时
                # 重建 players，并保留已加载 history / realtime（覆盖"gameflow 偶发为空回退 cs 后恢复"场景）
                if self._session is not None:
                    self._session.phase = phase
                    self._session.updated_at = time.time()
                    rebuilt = self._to_current_session(session, phase, me_puuid)
                    old_puuid_n = sum(1 for p in self._session.players if p.puuid)
                    new_puuid_n = sum(1 for p in rebuilt.players if p.puuid)
                    # 重建触发条件改为"有 puuid 的玩家数"（2026-10-18 修）：
                    # 选人阶段只 resolve 出己方 5 puuid、进游戏 gameflow 10 人全 puuid 时，
                    # 旧实现按 len(players) 比较（10==10 不触发）→ 敌方 5 人永远缺身份/战绩。
                    # 按 puuid 数比较 → 10>5 → 触发重建，并立即补拉缺失玩家。
                    # 本修追加"身份升级"触发（summonerId 数）：选人 HIDDEN cell summonerId=0，
                    # 进游戏 gameflow cell summonerId 为真实值（人数相同、puuid 数不变的
                    # ARAM/人机局等），此时也必须重建并重查身份，否则队友名字/等级永远
                    # 是空的（UI 显示 "? Lv.?"）。
                    old_sid_n = sum(1 for p in self._session.players if p.summoner_id)
                    new_sid_n = sum(1 for p in rebuilt.players if p.summoner_id)
                    identity_upgraded = new_sid_n > old_sid_n
                    if new_puuid_n > old_puuid_n or identity_upgraded or (
                            rebuilt.game_id and not self._session.game_id):
                        old_by_puuid = {p.puuid: p for p in self._session.players if p.puuid}
                        match_n = 0
                        for np_ in rebuilt.players:
                            op = old_by_puuid.get(np_.puuid)
                            if op is not None:
                                match_n += 1
                                np_.history = op.history
                                # 继承旧 display_name：防止进游戏重建瞬间名字被刷成 "?"
                                if not np_.display_name and op.display_name:
                                    np_.display_name = op.display_name
                                if not np_.realtime_available:
                                    np_.live_kills = op.live_kills
                                    np_.live_deaths = op.live_deaths
                                    np_.live_assists = op.live_assists
                                    np_.live_position = op.live_position
                                    np_.live_level = op.live_level
                                    np_.realtime_available = op.realtime_available
                        self._session = rebuilt
                        # 人数/身份突变：立即补拉缺失玩家（loader 缓存复用已加载玩家，
                        # 只对新 puuid / 缺名字玩家发请求）。锁内只置标志，线程启动在锁外。
                        if (new_puuid_n > old_puuid_n or identity_upgraded) and not self._loading_in_progress:
                            self._loading_in_progress = True
                            _need_load = True
                        reason = "puuid_upgrade" if new_puuid_n > old_puuid_n else ("identity_upgrade" if identity_upgraded else "game_id_fill")
                        self._on_event("ongoing.session_rebuilt", {
                            "game_id": rebuilt.game_id,
                            "puuid": new_puuid_n,
                            "summoner": new_sid_n,
                            "reason": reason,
                            "old_puuid": old_puuid_n,
                            "old_summoner": old_sid_n,
                        })
                # 同一局：若历史战绩缺失，周期性自动重试（每 game_id 最多10次，间隔60秒，
                # 覆盖进游戏初期 SGP 暂不可用）。直接 force 重载当前 session——
                # 不重建 session、不重发 session_created（原实现清 _current_session_key
                # 触发重建，是 session_created 反复刷屏的主因之一）。
                if phase == "InProgress" and not self._loading_in_progress:
                    now = time.monotonic()
                    rk = f"gid:{session.get('gameId')}" if session.get("gameId") else key
                    count, last_ts = self._retry_state.get(rk, [0, 0.0])
                    # 重试条件 = 战绩缺失 + 名字缺失（summoner 曾失败但 history 成功时，
                    # data_state=AVAILABLE 而 display_name 空——missing_names_count 兜住，
                    # 重载时用 InProgress 的 gameflow session（summonerId 真实）→ by-id 补名）。
                    # 缺失计数含"session 有 puuid 但 cache 无快照"（2026-10-18 修）：
                    # 敌方从未加载的玩家也计入，60s 自动重试才能触发。
                    session_puuids = ({p.puuid for p in self._session.players if p.puuid}
                                      if self._session else set())
                    missing = (self._loader.missing_history_count(session_puuids)
                               + self._loader.missing_names_count())
                    if (count < 10 and now - last_ts > 60.0 and missing > 0):
                        self._retry_state[rk] = [count + 1, now]
                        self._force_reload = True  # retry：绕过 ERROR cache，真正重新请求 SGP
                        self._loading_in_progress = True  # 防重入（_load_in_thread finally 复位）
                        _need_load = True
            elif self._loading_in_progress:
                return
            else:
                self._current_session_key = key
                self._loading_in_progress = True
                self._snapshots = []
                # 新 session：game_id 为生命周期锚点；旧 session（含 players）立即失效
                self._session = self._to_current_session(session, phase, me_puuid)
                # 新对局 retry 计数从 0 开始（_retry_state 按 game_id 持久，新 game_id 自动归零；
                # 复用 gameId 的新局已在离开对局分支清空 _retry_state）
                if key != self._last_loaded_key:
                    # 真正的新对局：不信任旧局 history cache（TTL 不承担 session 隔离），
                    # 强制重新请求全部玩家，禁止跨局复用上一局 PlayerSnapshot
                    self._force_reload = True
                self._last_loaded_key = key
                # session_created 防抖：同一 session（game_id）只宣布一次。
                # 离开对局已清空 _announced_session_id，故腾讯服复用 gameId 的新局会重新宣布。
                sid = f"gid:{self._session.game_id}" if self._session.game_id else key
                if sid != self._announced_session_id:
                    self._announced_session_id = sid
                    self._on_event("ongoing.session_created", {
                        "game_id": self._session.game_id,
                        "players": len(self._session.players),
                        "puuid": sum(1 for p in self._session.players if p.puuid),
                        "my_team": len(self._session.my_team),
                        "their_team": len(self._session.their_team),
                    })
        # 锁外启动线程（_load_in_thread 的 finally 会重取锁，锁内启动有死锁窗口）
        if _need_load:
            threading.Thread(target=self._load_in_thread, args=(session,), daemon=True).start()
            return
        if _same_session:
            return
        threading.Thread(target=self._load_in_thread, args=(session,), daemon=True).start()

    def _build_gameflow_session(self, me_puuid: str | None):
        """身份主源：gameflow session.gameData.teamOne/teamTwo → myTeam/theirTeam。

        gameflow 的 teamOne/teamTwo 不携带 team 字段，用当前召唤师 puuid 判定我方；
        判定失败时默认 teamOne=我方（与原 liveclient ORDER=己方语义一致）。
        实测：匹配局 teamOne=5 + teamTwo=5 / puuid=10；人机局 teamOne=5（bot 不在 session，
        不会把 bot 当真人身份加载）。返回 champ-select 兼容格式或 None。
        """
        try:
            gf = self._lcu.get("/lol-gameflow/v1/session")
        except Exception:
            return None
        if not isinstance(gf, dict):
            return None
        gd = gf.get("gameData") or {}
        t1, t2 = gd.get("teamOne") or [], gd.get("teamTwo") or []
        if not t1 and not t2:
            return None
        one_puuids = {(m.get("puuid") or "") for m in t1 if m.get("puuid")}
        two_puuids = {(m.get("puuid") or "") for m in t2 if m.get("puuid")}
        # 脱敏诊断：只打数量，不打 puuid；仅在 (gameMode/人数/puuid数) 变化时打一次
        dump_key = (gd.get("gameMode"), len(t1), len(t2), len(one_puuids), len(two_puuids))
        if self._last_gf_dump != dump_key:
            self._last_gf_dump = dump_key
            print(f"[Ongoing] gameflow session: gameMode={gd.get('gameMode')} "
                  f"teamOne={len(t1)}(puuid{len(one_puuids)}) teamTwo={len(t2)}(puuid{len(two_puuids)})")
        my_is_one = (me_puuid in one_puuids) if me_puuid else True
        my_src, their_src = (t1, t2) if my_is_one else (t2, t1)
        my_team = [self._gf_cell(m) for m in my_src if (m.get("puuid") or "")]
        their_team = [self._gf_cell(m) for m in their_src if (m.get("puuid") or "")]
        if not my_team and not their_team:
            return None
        game_id = gf.get("gameId") or gd.get("gameId") or 0
        self._on_event("ongoing.session_source",
                       {"source": "gameflow", "players": len(my_team) + len(their_team)})
        return {"myTeam": my_team, "theirTeam": their_team, "gameId": game_id}

    def _deobfuscate_champ_select(self, cs: dict, me_puuid: str | None) -> dict | None:
        """ChampSelect 阶段：cs session → 可加载 session（HIDDEN 成员反解 puuid）。

        只对 nameVisibilityType=="HIDDEN" 且 obfuscatedPuuid 非空的 cell 做还原；
        返回浅拷贝（不改 self._champ_select_session 缓存），反解失败保持原样。
        自己不在 my_team 或总人数 < 5 → 返回 None（不构建），并发 champ_select_skip
        事件留痕（排查"为什么没提前显示"时有据可查）。
        """
        def _resolve(cell: dict) -> dict:
            if cell.get("puuid"):
                return cell
            if (cell.get("nameVisibilityType") == "HIDDEN"
                    and cell.get("obfuscatedPuuid")):
                real = deobfuscate_puuid(str(cell.get("obfuscatedPuuid")))
                if real:
                    nc = dict(cell)
                    nc["puuid"] = real
                    return nc
            return cell

        my_team = [_resolve(c) for c in (cs.get("myTeam") or [])]
        their_team = [_resolve(c) for c in (cs.get("theirTeam") or [])]
        my_puuids = {c.get("puuid") for c in my_team if c.get("puuid")}
        resolved = sum(1 for c in my_team + their_team if c.get("puuid"))
        total = len(my_team) + len(their_team)
        # 诊断：分队记录反解明细（my/their 各有几人有真实 puuid、几个 HIDDEN 且带
        # obfuscatedPuuid）。用于直接回答"选人阶段敌方 puuid 解密成功了吗"——
        # 若 their_has_obfuscated > 0 但 their_resolved 少，说明 XOR 反解失败；
        # 若 their_has_obfuscated == 0，说明该对局 LCU 选人接口根本不下发敌方混淆身份，
        # 必须等进游戏后 gameflow 10/10 明文 puuid 补拉（identity_upgrade/puuid_upgrade）。
        def _team_stats(cells):
            return {
                "resolved": sum(1 for c in cells if c.get("puuid")),
                "total": len(cells),
                "hidden_with_obfuscated": sum(
                    1 for c in cells
                    if c.get("nameVisibilityType") == "HIDDEN" and c.get("obfuscatedPuuid")
                ),
            }
        self._on_event("ongoing.cs_team_resolve", {
            "my": _team_stats(my_team),
            "their": _team_stats(their_team),
        })
        if not me_puuid or me_puuid not in my_puuids:
            self._on_event("ongoing.champ_select_skip", {
                "reason": "me_puuid_not_in_resolved_team",
                "resolved": resolved,
                "total": total,
            })
            return None
        if total < 5:
            self._on_event("ongoing.champ_select_skip", {
                "reason": "total_less_than_5",
                "resolved": resolved,
                "total": total,
            })
            return None
        self._on_event("ongoing.session_source", {
            "source": "champ_select_deobfuscated",
            "players": total,
            "resolved": resolved,
        })
        return {"myTeam": my_team, "theirTeam": their_team,
                "gameId": cs.get("gameId") or 0}

    def _supplement_gsm(self, session: dict, phase: str,
                        me_puuid: str | None) -> dict:
        """GSM 补充身份源：仅 GameStart/InProgress/Reconnect 启用；ChampSelect 不请求。

        Gameflow 永远是主源：GSM 只在同局校验通过后追加 Gameflow 完全没有的 puuid；
        任何失败（token/resolver/404/5xx/超时/JSON/结构异常/gameId 不一致/self 不在
        GSM 两队）都原样返回 session，无损降级——GSM 异常不会导致 session 消失/变空。
        """
        if self._gsm is None:
            return session
        if phase not in ("GameStart", "InProgress", "Reconnect"):
            return session
        if not me_puuid:
            return session
        try:
            status, gsm_game = self._gsm.get_current_game(me_puuid)
        except Exception:
            status, gsm_game = "request_failed", None
        if status != "ok" or not isinstance(gsm_game, dict):
            self._on_event("ongoing.gsm_supplement",
                           {"added": 0, "game_id_matched": False, "result": status})
            return session
        try:
            merged, added, matched = merge_gsm_players(session, gsm_game, me_puuid)
        except Exception:
            self._on_event("ongoing.gsm_supplement",
                           {"added": 0, "game_id_matched": False, "result": "merge_error"})
            return session
        if not matched:
            self._on_event("ongoing.gsm_supplement",
                           {"added": 0, "game_id_matched": False, "result": "game_id_mismatch"})
            return session
        self._on_event("ongoing.gsm_supplement",
                       {"added": added, "game_id_matched": True, "result": "merged"})
        return merged

    @staticmethod
    def _gf_cell(m: dict) -> dict:
        """gameflow 成员 → champ-select 兼容 cell（含实时字段占位，供 _merge_live_fields 合并）。"""
        return {
            "puuid": m.get("puuid") or "",
            "summonerId": m.get("summonerId") or 0,
            "cellId": m.get("cellId") or 0,
            "riotIdGameName": m.get("riotIdGameName") or "",
            "riotId": m.get("riotId") or "",
            "championId": m.get("championId") or 0,
            "assignedPosition": m.get("assignedPosition") or "",
        }

    def _merge_live_fields(self, session: dict) -> None:
        """liveclientdata 无 puuid：按 (我方/敌方, championId) 关联合并实时 KDA/位置到 gameflow 身份 cell。

        身份归属由 gameflow 决定；liveclientdata 只补充实时字段。
        优先用 snapshot 的 ally_team/enemy_team 分组（内部已按"我所在队"判定，与 gameflow 的
        myTeam/theirTeam 判定天然对齐，不依赖 ORDER/CHAOS 固定映射）；
        匹配不到的玩家（如人机 bot）直接跳过，不会被当成真人身份。不改变 session 成员集合。
        """
        try:
            snap = self._snapshot_provider() if self._snapshot_provider else None
        except Exception:
            snap = None
        if snap is None:
            return
        # 宽进建库：收集所有 2999 玩家（不因 cid 解析失败而丢弃任何人）
        live_by_cid: dict[tuple[str, int], object] = {}
        live_by_name: dict[tuple[str, str], object] = {}
        all_live_players: list[tuple[str, object]] = []
        if getattr(snap, "ally_team", None):
            all_live_players.extend(("myTeam", p) for p in snap.ally_team)
        if getattr(snap, "enemy_team", None):
            all_live_players.extend(("theirTeam", p) for p in snap.enemy_team)
        # 兜底：snapshot 无 ally/enemy 分组时，用 players + ORDER/CHAOS 映射
        if not all_live_players and getattr(snap, "players", None):
            team_map = {"ORDER": "myTeam", "CHAOS": "theirTeam"}
            for p in snap.players:
                t = team_map.get((getattr(p, "team", "") or "").upper())
                if t:
                    all_live_players.append((t, p))

        for t, p in all_live_players:
            cid = _resolve_live_champion_id(p)
            if cid:
                live_by_cid[(t, cid)] = p
            # 宽进策略：不管有没有解析出 cid，只要有名字，就按名字建索引
            names = set()
            for attr in ("summoner_name", "riot_game_name"):
                val = (getattr(p, attr, "") or "").strip()
                if val:
                    names.add(val)
            for n in names:
                live_by_name[(t, n)] = p

        merged = 0
        for team_key in ("myTeam", "theirTeam"):
            for cell in (session.get(team_key) or []):
                cell_cid = cell.get("championId") or 0
                cell_name = (cell.get("riotIdGameName") or "").strip()
                # 1. 优先按 cid 匹配
                p = live_by_cid.get((team_key, cell_cid))
                # 2. 没匹配到则按名字匹配（不依赖先解析出 cid）
                if p is None and cell_name:
                    p = live_by_name.get((team_key, cell_name))
                if p is None:
                    continue
                # 匹配到玩家后，尝试解析并回填真实英雄 ID
                cid = _resolve_live_champion_id(p)
                if cid:
                    # 回填 2999 真实英雄 ID：UI 本局英雄/绝活判定的数据源
                    cell["championId"] = cid
                cell["live_kills"] = getattr(p, "kills", 0) or 0
                cell["live_deaths"] = getattr(p, "deaths", 0) or 0
                cell["live_assists"] = getattr(p, "assists", 0) or 0
                cell["live_position"] = getattr(p, "position", "") or ""
                cell["live_level"] = getattr(p, "level", 0) or 0
                cell["live_merged"] = True
                merged += 1
        if merged:
            self._on_event("ongoing.live_merged", {"players": merged})

    def _build_session_from_snapshot(self):
        """[已移除] 从 live client snapshot 构建 session。

        阶段1 起 2999 不再决定"当前有哪些玩家"：玩家集合唯一主源 = Gameflow。
        此函数（2999 → 玩家集合）违反该约束，已从 _tick 调用链中删除。
        """
        raise NotImplementedError("2999 不再作为玩家集合来源（阶段1 已移除）")

    @staticmethod
    def _to_player(cell: dict, team: str, me_puuid: str | None) -> CurrentGamePlayer:
        """gameflow/champ-select cell → CurrentGamePlayer（identity + realtime 两区）。

        identity 唯一来源：gameflow teamOne/teamTwo（或同局 champ-select 缓存回退）；
        realtime 唯一来源：_merge_live_fields 合并后的 2999 字段（cell.live_*）。
        history 区由 _load_in_thread 加载完成后挂载。
        """
        puuid = cell.get("puuid") or ""
        return CurrentGamePlayer(
            puuid=puuid,
            summoner_id=cell.get("summonerId") or 0,
            display_name=(cell.get("riotIdGameName") or cell.get("riotId")
                          or cell.get("name") or cell.get("summonerName") or ""),
            cell_id=cell.get("cellId") or 0,
            team=team,
            assigned_position=(cell.get("assignedPosition") or "") or None,
            champion_id=cell.get("championId") or 0,
            champion_pick_intent=cell.get("championPickIntent") or 0,
            is_self=(puuid == me_puuid),
            live_kills=cell.get("live_kills") or 0,
            live_deaths=cell.get("live_deaths") or 0,
            live_assists=cell.get("live_assists") or 0,
            live_position=(cell.get("live_position") or "") or None,
            live_level=cell.get("live_level") or 0,
            realtime_available=bool(cell.get("live_merged")),
        )

    def _to_current_session(self, session: dict, phase: str,
                            me_puuid: str | None) -> CurrentGameSession:
        now = time.time()
        return CurrentGameSession(
            game_id=session.get("gameId") or 0,
            phase=phase,
            my_team=[self._to_player(c, "myTeam", me_puuid) for c in (session.get("myTeam") or [])],
            their_team=[self._to_player(c, "theirTeam", me_puuid) for c in (session.get("theirTeam") or [])],
            started_at=now,
            updated_at=now,
        )

    @staticmethod
    def _session_key(session: dict) -> str:
        game_id = session.get("gameId")
        if game_id:
            return f"gid:{game_id}"
        # fallback: puuid 集合 hash
        puuids = sorted(
            (c.get("puuid") or "")
            for c in (session.get("myTeam") or []) + (session.get("theirTeam") or [])
            if c.get("puuid")
        )
        h = hashlib.md5(",".join(puuids).encode()).hexdigest()[:12]
        return f"hash:{h}"

    def _load_in_thread(self, session: dict) -> None:
        try:
            snaps = self._loader.load_for_champ_select(session, force=self._force_reload)
            with self._lock:
                self._snapshots = snaps
                # 把 loader 产出的历史快照挂到当前 session 对应玩家的 history 区
                # （CurrentGamePlayer.history = loader -> acct -> SGP/LCU 单链产物）
                if self._session is not None:
                    snap_by_puuid = {s.puuid: s for s in snaps}
                    for p in self._session.players:
                        s = snap_by_puuid.get(p.puuid)
                        if s is not None:
                            p.history = s
                            # 玩家名回填：腾讯服 gameflow cell 不含/缺 riotIdGameName 时，
                            # 用 loader→SGP 链路查到的真名（PlayerSnapshot.display_name）补 identity，
                            # UI 保持纯渲染（display_name 始终在 identity 区，history 只作后备来源）
                            if not p.display_name and getattr(s, "display_name", ""):
                                p.display_name = s.display_name
                    self._session.updated_at = time.time()
                    states = {}
                    for s in snaps:
                        st = getattr(s, "data_state", "ERROR")
                        states[st] = states.get(st, 0) + 1
                    self._on_event("ongoing.history_summary", {
                        "loaded": len(snaps),
                        "available": states.get("AVAILABLE", 0),
                        "no_data": states.get("NO_DATA", 0),
                        "error": states.get("ERROR", 0),
                    })
        except Exception as e:
            self._on_event("ongoing.error", {"err": str(e)})
        finally:
            with self._lock:
                self._loading_in_progress = False
                self._force_reload = False

    def _on_player_name_resolved(self, puuid, display_name):
        # 名字秒出回调：loader 子线程查到名字后立即更新 session（先出名字，战绩后台加载）
        if not (puuid and display_name):
            return
        with self._lock:
            if self._session is not None:
                for p in self._session.players:
                    if p.puuid == puuid and not p.display_name:
                        p.display_name = display_name
                        break

    # ----- UI -----
    def get_session(self) -> CurrentGameSession | None:
        """当前对局 session（UI 玩家集合唯一来源；2999 不参与玩家存在性）。"""
        with self._lock:
            return self._session

    def get_snapshots(self) -> list[PlayerSnapshot]:
        with self._lock:
            return list(self._snapshots)

    def status(self) -> dict:
        return self._loader.status()
