"""验证音量缓存问题"""
import sys, time
sys.path.insert(0, '.')

from app.music.mpv import MpvController

mpv = MpvController(r'D:\新建文件夹 (2)\mpv.exe', r'\\.\pipe\lol_music_volume_test')
mpv.start()

print('=== 初始状态 ===')
vol = mpv.get_property('volume')
cached = mpv._properties.get('volume', '未设置')
print(f'mpv 真实 volume: {vol}')
print(f'缓存 volume: {cached}')

print()
print('=== set_volume(50) ===')
mpv.set_volume(50)
time.sleep(0.2)
vol = mpv.get_property('volume')
cached = mpv._properties.get('volume', '未设置')
print(f'mpv 真实 volume: {vol}')
print(f'缓存 volume: {cached}')

print()
print('=== 连续 5 次 volume_down (每次 -5) ===')
for i in range(5):
    actual_before = mpv.get_property('volume')
    mpv.set_volume(actual_before - 5)
    time.sleep(0.1)
    actual_after = mpv.get_property('volume')
    cached = mpv._properties.get('volume', '未设置')
    print(f'  #{i+1}: 真实before={actual_before} -> 真实after={actual_after} | 缓存={cached}')

mpv.stop_process()
print()
print('完成')
