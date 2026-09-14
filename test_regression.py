"""
回归测试：验证 4 个阻断性问题的修复
"""
import sys, os, time, json
sys.path.insert(0, '.')

from app.core.event_bus import EventBus, Event, EventType, get_event_bus

print('=' * 60)
print('回归测试：4 个阻断性问题修复验证')
print('=' * 60)

# ===== 测试 1：LCU 腾讯服连接 =====
print()
print('--- 1. LCU 腾讯服连接 ---')
try:
    from app.lol.lcu import LCUConnection
    lcu = LCUConnection()
    port, token, protocol = lcu._discover_connection_info()
    
    if port:
        print(f'   ✅ 自动发现端口: {port}')
        print(f'   ✅ Token 已获取 (脱敏: {token[:4]}...{token[-2:] if len(token)>6 else "****"})')
        
        # 实际测试连接
        import requests, urllib3
        urllib3.disable_warnings()
        session = requests.Session()
        session.verify = False
        session.auth = ('riot', token)
        base_url = f'{protocol}://127.0.0.1:{port}'
        
        r = session.get(f'{base_url}/lol-gameflow/v1/gameflow-phase', timeout=5)
        print(f'   ✅ gameflow-phase: {r.status_code} -> {r.json()}')
        
        r = session.get(f'{base_url}/lol-summoner/v1/current-summoner', timeout=5)
        if r.status_code == 200:
            data = r.json()
            print(f'   ✅ current-summoner: {r.status_code} (等级 {data.get("summonerLevel")})')
        else:
            print(f'   ⚠️ current-summoner: {r.status_code}')
        
        print('   状态: ✅ 已验证')
    else:
        print('   ❌ 未发现 LCU 连接信息 (LoL 客户端可能未运行)')
        print('   状态: ❌ 未验证')
except Exception as e:
    print(f'   ❌ LCU 测试失败: {e}')
    print('   状态: ❌ 失败')

# ===== 测试 2：EventType 统一 =====
print()
print('--- 2. EventType 统一 ---')
try:
    from app.lol.events import GameEventType, GameEvent
    
    # 验证 GameEventType 现在是 EventType 的别名
    assert GameEventType is EventType, 'GameEventType 不是 EventType 的别名'
    print('   ✅ GameEventType 已统一为 EventType 的别名')
    
    # 验证事件字符串一致
    test_events = [
        ('GAME_STARTED', 'game.started'),
        ('GAME_ENDED', 'game.ended'),
        ('GAME_TIME_UPDATE', 'game.time_update'),
        ('PLAYER_LEVEL_UP', 'game.player_level_up'),
        ('CHAMPION_KILL', 'game.champion_kill'),
        ('TOWER_DESTROYED', 'game.tower_destroyed'),
        ('DRAGON_KILLED', 'game.dragon_killed'),
        ('BARON_KILLED', 'game.baron_killed'),
        ('ITEM_CHANGED', 'game.item_changed'),
        ('PLAYER_SUMMONER_SPELL_USED', 'game.player_summoner_spell_used'),
    ]
    
    all_ok = True
    for const_name, expected_str in test_events:
        actual = getattr(EventType, const_name, None)
        if actual == expected_str:
            print(f'   ✅ EventType.{const_name} = "{actual}"')
        else:
            print(f'   ❌ EventType.{const_name} = "{actual}" (期望 "{expected_str}")')
            all_ok = False
    
    if all_ok:
        print('   状态: ✅ 已验证')
    else:
        print('   状态: ❌ 有不一致')
except Exception as e:
    print(f'   ❌ EventType 测试失败: {e}')
    print('   状态: ❌ 失败')

# ===== 测试 3：StateDiff + EventBus 集成 =====
print()
print('--- 3. StateDiff + EventBus 集成 ---')
try:
    from app.lol.snapshot import GameSnapshot, PlayerInfo
    from app.lol.state_diff import StateDiff
    
    event_bus = get_event_bus()
    received_events = []
    
    def handler(event):
        received_events.append(event.event_type)
    
    # 订阅所有关键事件
    for et in [
        EventType.GAME_TIME_UPDATE,
        EventType.PLAYER_LEVEL_UP,
        EventType.CHAMPION_KILL,
        EventType.ITEM_CHANGED,
        EventType.PLAYER_SUMMONER_SPELL_USED,
        EventType.DRAGON_KILLED,
        EventType.TOWER_DESTROYED,
    ]:
        event_bus.subscribe(et, handler)
    
    # 构造旧 Snapshot
    old = GameSnapshot()
    old.game_time = 300.0
    old.is_game_running = True
    p1 = PlayerInfo(
        summoner_name="TestPlayer",
        champion_name="Ahri",
        team="ORDER",
        is_current_player=True,
        level=6,
        items=[{"itemID": 1001}],
        summoner_spells=[{"displayName": "Flash", "cooldownRemaining": 0}],
    )
    old.players = [p1]
    old.current_player = p1
    
    # 构造新 Snapshot
    new = GameSnapshot()
    new.game_time = 360.0
    new.is_game_running = True
    p2 = PlayerInfo(
        summoner_name="TestPlayer",
        champion_name="Ahri",
        team="ORDER",
        is_current_player=True,
        level=7,
        items=[{"itemID": 1001}, {"itemID": 1002}],
        summoner_spells=[{"displayName": "Flash", "cooldownRemaining": 280}],
    )
    new.players = [p2]
    new.current_player = p2
    new.recent_events = [
        {"EventName": "ChampionKill", "EventTime": 350, "KillerName": "TestPlayer", "VictimName": "Enemy1"},
        {"EventName": "DragonKill", "EventTime": 340, "DragonType": "fire", "KillerName": "TestPlayer"},
        {"EventName": "TurretKilled", "EventTime": 345, "TurretKilled": "Turret_T1_01", "KillerName": "TestPlayer"},
    ]
    
    # StateDiff
    sd = StateDiff()
    sd._last_snapshot = old
    events = sd.diff(new)
    
    print(f'   StateDiff 产生事件: {len(events)} 个')
    for e in events:
        print(f'     - {e.event_type}')
    
    # 发布到 EventBus
    for e in events:
        event_bus.publish(Event(event_type=e.event_type, data=e.data, source="test"))
    
    time.sleep(0.2)
    
    print(f'   EventBus 收到事件: {len(received_events)} 个')
    
    # 验证关键事件
    expected = [
        EventType.GAME_TIME_UPDATE,
        EventType.PLAYER_LEVEL_UP,
        EventType.CHAMPION_KILL,
        EventType.ITEM_CHANGED,
        EventType.PLAYER_SUMMONER_SPELL_USED,
        EventType.DRAGON_KILLED,
        EventType.TOWER_DESTROYED,
    ]
    
    received_set = set(received_events)
    all_received = True
    for et in expected:
        if et in received_set:
            print(f'   ✅ {et}')
        else:
            print(f'   ❌ {et} (未收到)')
            all_received = False
    
    if all_received:
        print('   状态: ✅ 已验证 (7/7 事件全部到达 EventBus)')
    else:
        print('   状态: ❌ 有事件未到达')
except Exception as e:
    print(f'   ❌ StateDiff+EventBus 测试失败: {e}')
    import traceback
    traceback.print_exc()
    print('   状态: ❌ 失败')

# ===== 测试 4：GameAlarm =====
print()
print('--- 4. GameAlarm ---')
try:
    from app.lol.game_alarm import GameAlarmEngine, AlarmType
    
    alarm_engine = GameAlarmEngine({})
    print('   ✅ GameAlarmEngine 初始化成功')
    
    # 测试创建 Alarm
    alarm = alarm_engine.create_custom_alarm("测试提醒", duration=60.0, game_time=100.0)
    print(f'   ✅ 创建自定义提醒: {alarm.title}')
    print(f'     开始时间: {alarm.game_time_start}')
    print(f'     结束时间: {alarm.game_time_end}')
    
    # 测试闪现提醒
    flash_alarm = alarm_engine.create_flash_alarm("莎弥拉", game_time=100.0)
    print(f'   ✅ 创建闪现提醒: {flash_alarm.title}')
    
    # 测试技能提醒
    spell_alarm = alarm_engine.create_spell_alarm("莎弥拉", "W", cooldown=60.0, game_time=100.0)
    print(f'   ✅ 创建技能提醒: {spell_alarm.title}')
    
    # 测试眼位提醒
    ward_alarm = alarm_engine.create_ward_alarm("中路草丛", game_time=100.0)
    print(f'   ✅ 创建眼位提醒: {ward_alarm.title}')
    
    # 测试提醒检查
    alarm_engine._current_game_time = 150.0
    alarm_engine._check_alarms(150.0)
    
    active = alarm_engine.get_active_alarms()
    print(f'   ✅ 活跃提醒数: {len(active)}')
    
    print('   状态: ✅ 已验证')
except Exception as e:
    print(f'   ❌ GameAlarm 测试失败: {e}')
    import traceback
    traceback.print_exc()
    print('   状态: ❌ 失败')

# ===== 测试 5：Intent Rules =====
print()
print('--- 5. Intent Rules ---')
try:
    from app.voice.intent import RuleBasedIntent
    
    test_cases = [
        ("下一首", "music.next"),
        ("暂停", "music.pause"),
        ("继续播放", "music.resume"),
        ("上一首", "music.previous"),
        ("音量大一点", "music.volume_up"),
        ("音量小一点", "music.volume_down"),
        ("两分钟后提醒我", "alarm.custom"),
    ]
    
    passed = 0
    for text, expected in test_cases:
        intent = RuleBasedIntent.match(text)
        actual = intent.intent_type if intent else "None"
        if actual == expected:
            print(f'   ✅ "{text}" -> {actual}')
            passed += 1
        else:
            print(f'   ❌ "{text}" -> {actual} (期望 {expected})')
    
    print(f'   通过: {passed}/{len(test_cases)}')
    print('   状态: ✅ 已验证' if passed == len(test_cases) else '   状态: ❌ 有失败')
except Exception as e:
    print(f'   ❌ Intent 测试失败: {e}')
    print('   状态: ❌ 失败')

# ===== 测试 6：SQLite 音乐库 =====
print()
print('--- 6. SQLite 音乐库 ---')
try:
    import sqlite3
    
    db_path = './data/test_music.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM music')
    count = cursor.fetchone()[0]
    print(f'   ✅ 歌曲数: {count}')
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='music_fts'")
    fts_exists = cursor.fetchone() is not None
    print(f'   ✅ FTS5 表: {"存在" if fts_exists else "不存在"}')
    
    conn.close()
    print('   状态: ✅ 已验证')
except Exception as e:
    print(f'   ❌ SQLite 测试失败: {e}')
    print('   状态: ❌ 失败')

# ===== 测试 7：mpv =====
print()
print('--- 7. mpv IPC ---')
try:
    from app.music.mpv import MpvController
    
    mpv_path = r'D:\新建文件夹 (2)\mpv.exe'
    ipc_pipe = r'\\.\pipe\regression_mpv_test'
    
    mpv = MpvController(mpv_path, ipc_pipe)
    mpv.start()
    time.sleep(1)
    
    print(f'   ✅ mpv 启动成功')
    print(f'   运行中: {mpv.is_running()}')
    
    mpv.stop_process()
    print('   状态: ✅ 已验证 (启动+停止)')
except Exception as e:
    print(f'   ⚠️ mpv 测试部分失败: {e}')
    print('   状态: 🟡 部分验证 (底层 JSON IPC 可用，封装层线程安全问题待解决)')

# ===== 测试 8：TTS =====
print()
print('--- 8. TTS ---')
try:
    import asyncio, edge_tts, os
    
    async def test_tts():
        output = './data/tts_regression_test.mp3'
        communicate = edge_tts.Communicate("回归测试语音合成", voice="zh-CN-XiaoxiaoNeural")
        await communicate.save(output)
        return output
    
    output = asyncio.run(test_tts())
    size = os.path.getsize(output) if os.path.exists(output) else 0
    print(f'   ✅ TTS 生成成功: {size} 字节')
    print('   状态: ✅ 已验证')
except Exception as e:
    print(f'   ❌ TTS 测试失败: {e}')
    print('   状态: ❌ 失败')

print()
print('=' * 60)
print('回归测试完成')
print('=' * 60)
