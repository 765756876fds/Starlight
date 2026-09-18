import requests, urllib3
urllib3.disable_warnings()
r = requests.get('https://127.0.0.1:2999/liveclientdata/playerlist', verify=False)
players = r.json()
self_team = None
self_name = None
for p in players:
    if p.get('isSelf'):
        self_team = p['team']
        self_name = p['summonerName']
print(f'self: {self_name} team={self_team}')
print()
print('ALLIES:')
for p in players:
    if p['team'] == self_team:
        print(f'  {p["summonerName"]:25s} {p["championName"]:15s} {p["team"]}')
print('ENEMIES:')
for p in players:
    if p['team'] != self_team:
        print(f'  {p["summonerName"]:25s} {p["championName"]:15s} {p["team"]}')
