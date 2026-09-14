"""审计测试：LCU 腾讯服连接验证"""
import sys, os
sys.path.insert(0, '.')

from app.lol.lcu import LCUConnection

print('=== LCU 腾讯服连接测试 ===')
print()

# 创建连接管理器
lcu = LCUConnection()

print('1. 测试连接信息发现...')
# 不启动后台线程，直接调用内部方法测试
port, token, protocol = lcu._discover_connection_info()

if port:
    print(f'   ✅ 发现端口: {port}')
    print(f'   ✅ 发现 token: {token[:4]}...{token[-2:] if len(token) > 6 else "****"}')
    print(f'   ✅ 协议: {protocol}')
else:
    print('   ❌ 未发现连接信息（LoL 客户端可能未运行）')
    sys.exit(1)

print()
print('2. 测试实际连接...')
lcu._port = port
lcu._auth_token = token
lcu._protocol = protocol

import requests
import urllib3
urllib3.disable_warnings()

session = requests.Session()
session.verify = False
session.auth = ('riot', token)
session.headers.update({'Accept': 'application/json'})

base_url = f'{protocol}://127.0.0.1:{port}'

# 测试 gameflow-phase
print()
print('   --- /lol-gameflow/v1/gameflow-phase ---')
try:
    r = session.get(f'{base_url}/lol-gameflow/v1/gameflow-phase', timeout=5)
    print(f'   状态码: {r.status_code}')
    print(f'   响应: {r.text}')
except Exception as e:
    print(f'   失败: {e}')

# 测试 current-summoner
print()
print('   --- /lol-summoner/v1/current-summoner ---')
try:
    r = session.get(f'{base_url}/lol-summoner/v1/current-summoner', timeout=5)
    print(f'   状态码: {r.status_code}')
    if r.status_code == 200:
        data = r.json()
        print(f'   等级: {data.get("summonerLevel", "N/A")}')
        print(f'   puuid: {str(data.get("puuid", "N/A"))[:30]}...')
    else:
        print(f'   响应: {r.text[:200]}')
except Exception as e:
    print(f'   失败: {e}')

# 测试 champion-select（如果在英雄选择中）
print()
print('   --- /lol-champ-select/v1/session ---')
try:
    r = session.get(f'{base_url}/lol-champ-select/v1/session', timeout=5)
    print(f'   状态码: {r.status_code}')
    if r.status_code == 200:
        print(f'   ✅ 正在英雄选择中')
    else:
        print(f'   不在英雄选择中（正常）')
except Exception as e:
    print(f'   失败: {e}')

# 测试 matchmaking
print()
print('   --- /lol-matchmaking/v1/search ---')
try:
    r = session.get(f'{base_url}/lol-matchmaking/v1/search', timeout=5)
    print(f'   状态码: {r.status_code}')
except Exception as e:
    print(f'   失败: {e}')

print()
print('=== LCU 测试完成 ===')
