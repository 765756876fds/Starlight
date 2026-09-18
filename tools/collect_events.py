"""GAME-EVENT-07: 持续采集 eventdata 直到游戏结束"""
import requests
import urllib3
import json
import time
import sys
from datetime import datetime
from collections import Counter

urllib3.disable_warnings()

BASE = 'https://127.0.0.1:2999/liveclientdata'
MAX_DURATION = 15 * 60  # 15 分钟
POLL_INTERVAL = 3  # 每 3 秒采集一次
OUTPUT_FILE = 'tools/eventdata_real_sample_full.json'


def main():
    print(f"=== GAME-EVENT-07 持续采集开始 ===")
    print(f"目标时长: {MAX_DURATION}s")
    print(f"轮询间隔: {POLL_INTERVAL}s")
    print(f"输出文件: {OUTPUT_FILE}")
    print()

    start_time = time.time()
    all_events = {}  # 用 EventID 去重
    poll_count = 0
    game_ended = False

    while time.time() - start_time < MAX_DURATION:
        poll_count += 1
        elapsed = time.time() - start_time

        try:
            # 采集 eventdata
            resp = requests.get(f'{BASE}/eventdata', verify=False, timeout=3)

            if resp.status_code == 200:
                data = resp.json()
                events = data.get('Events', [])

                new_count = 0
                for e in events:
                    eid = e.get('EventID')
                    if eid is not None and eid not in all_events:
                        all_events[eid] = e
                        new_count += 1

                if new_count > 0:
                    now = datetime.now().strftime('%H:%M:%S')
                    print(f"[{now}] +{elapsed:.0f}s | poll #{poll_count} | 新事件: {new_count} | 总事件: {len(all_events)}")

            else:
                print(f"[poll #{poll_count}] HTTP {resp.status_code}")

        except requests.exceptions.ConnectionError:
            # 游戏结束，2999 关闭
            print(f"\n[!] 游戏结束，2999 已关闭")
            game_ended = True
            break

        except Exception as e:
            print(f"[poll #{poll_count}] Error: {type(e).__name__}: {str(e)[:100]}")

        time.sleep(POLL_INTERVAL)

    # 保存完整数据
    events_list = sorted(all_events.values(), key=lambda x: x.get('EventID', 0))
    output_data = {"Events": events_list}

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # 统计
    print(f"\n=== 采集完成 ===")
    print(f"总轮询次数: {poll_count}")
    print(f"总事件数: {len(events_list)}")
    print(f"游戏结束: {'是' if game_ended else '否（超时）'}")
    print(f"总时长: {time.time() - start_time:.1f}s")

    # EventName 统计
    event_names = Counter(e.get('EventName') for e in events_list)
    print(f"\n=== EventName 统计 ===")
    for name, count in sorted(event_names.items()):
        print(f"  {name}: {count}")

    # 核心事件 JSON
    print(f"\n=== 核心事件真实 JSON ===")
    target_events = ['TurretKilled', 'DragonKill', 'RiftHeraldKill', 'BaronKill',
                     'InhibitorKilled', 'GameEnd', 'Multikill', 'FirstBlood', 'Ace']

    for target in target_events:
        found = [e for e in events_list if e.get('EventName') == target]
        if found:
            print(f"\n--- {target} ({len(found)} 个) ---")
            print(json.dumps(found[0], indent=2, ensure_ascii=False))
        else:
            print(f"\n--- {target}: 未采集到 ---")

    print(f"\n完整数据已保存到: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
