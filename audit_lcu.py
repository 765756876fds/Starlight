"""审计测试：LCU 腾讯服连接测试"""
import requests, urllib3, json, sys
urllib3.disable_warnings()

# 从日志获取的 LCU 信息
# 注意：token 不要硬编码在代码中，应从进程命令行动态获取
port = 58334
token = 'YOUR_TOKEN_HERE'  # 从 LeagueClientUx 进程命令行动态获取
base_url = f'https://127.0.0.1:{port}'

print('=== LCU 连接测试 (腾讯服) ===')
print(f'端口: {port}')
print(f'Base URL: {base_url}')

session = requests.Session()
session.verify = False
session.auth = ('riot', token)
session.headers.update({'Accept': 'application/json'})

# 测试 1: gameflow-phase
print('\n--- 1. /lol-gameflow/v1/gameflow-phase ---')
try:
    r = session.get(f'{base_url}/lol-gameflow/v1/gameflow-phase', timeout=5)
    print(f'状态码: {r.status_code}')
    print(f'响应: {r.text}')
except Exception as e:
    print(f'失败: {e}')

# 测试 2: 当前 summoner
print('\n--- 2. /lol-summoner/v1/current-summoner ---')
try:
    r = session.get(f'{base_url}/lol-summoner/v1/current-summoner', timeout=5)
    print(f'状态码: {r.status_code}')
    if r.status_code == 200:
        data = r.json()
        print(f'召唤师名: {data.get("displayName", "N/A")}')
        print(f'等级: {data.get("summonerLevel", "N/A")}')
        puuid = data.get("puuid", "N/A")
        print(f'puuid: {puuid[:30]}...' if puuid and len(puuid) > 30 else f'puuid: {puuid}')
    else:
        print(f'响应: {r.text[:200]}')
except Exception as e:
    print(f'失败: {e}')

# 测试 3: champion-select
print('\n--- 3. /lol-champ-select/v1/session ---')
try:
    r = session.get(f'{base_url}/lol-champ-select/v1/session', timeout=5)
    print(f'状态码: {r.status_code}')
    if r.status_code == 200:
        print('英雄选择中!')
    else:
        print('不在英雄选择 (正常)')
except Exception as e:
    print(f'失败: {e}')

# 测试 4: 可用 endpoint 列表
print('\n--- 4. /help (可用端点列表) ---')
try:
    r = session.get(f'{base_url}/help', timeout=5)
    print(f'状态码: {r.status_code}')
    if r.status_code == 200:
        endpoints = r.json()
        print(f'可用端点数量: {len(endpoints)}')
        key_endpoints = [e for e in endpoints if any(k in str(e) for k in ['gameflow', 'champ-select', 'summoner', 'matchmaking', 'lobby'])]
        print(f'关键端点: {len(key_endpoints)} 个')
        for e in key_endpoints[:15]:
            print(f'  {e}')
except Exception as e:
    print(f'失败: {e}')

# 测试 5: matchmaking
print('\n--- 5. /lol-matchmaking/v1/search ---')
try:
    r = session.get(f'{base_url}/lol-matchmaking/v1/search', timeout=5)
    print(f'状态码: {r.status_code}')
    if r.status_code == 200:
        print('匹配中!')
    else:
        print('不在匹配中 (正常)')
except Exception as e:
    print(f'失败: {e}')

print('\n=== LCU 测试完成 ===')
