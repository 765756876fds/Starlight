"""V1 音量体验验收测试"""
import sys, time
sys.path.insert(0, '.')

from app.music.mpv import MpvController

mpv = MpvController(r'D:\新建文件夹 (2)\mpv.exe', r'\\.\pipe\lol_music_vol_e2e')

print("=" * 50)
print("V1 音量体验验收")
print("=" * 50)

mpv.start()

# 播放测试音乐
import os, glob
music_dir = r"D:\落雪\音乐文件"
songs = glob.glob(os.path.join(music_dir, "*.flac"))
if songs:
    mpv.loadfile(songs[0])
    time.sleep(1)
    print(f"播放: {os.path.basename(songs[0])}")

# 设置初始音量 60
mpv.set_volume(60)
time.sleep(0.5)
vol = mpv.get_volume()
print(f"\n初始音量: {vol}")
print("请按回车开始测试...")
input()

print("\n" + "=" * 50)
print("阶段 1: 声音小一点 × 5")
print("=" * 50)

results_down = []
for i in range(5):
    before = mpv.get_volume()
    mpv.set_volume(before - 5)
    time.sleep(0.8)  # 等用户感受
    after = mpv.get_volume()
    results_down.append((i+1, before, after, after - before))
    print(f"第 {i+1} 次: {before} → {after} (delta={after-before})")

print("\n请告诉我听感：是否明显下降？(输入描述)")
feedback_down = input("听感反馈: ")

print("\n" + "=" * 50)
print("阶段 2: 声音大一点 × 5")
print("=" * 50)

results_up = []
for i in range(5):
    before = mpv.get_volume()
    mpv.set_volume(before + 5)
    time.sleep(0.8)
    after = mpv.get_volume()
    results_up.append((i+1, before, after, after - before))
    print(f"第 {i+1} 次: {before} → {after} (delta={after-before})")

print("\n请告诉我听感：是否恢复？(输入描述)")
feedback_up = input("听感反馈: ")

# 输出总结
print("\n" + "=" * 50)
print("测试总结")
print("=" * 50)
print(f"\n声音小一点 × 5:")
print(f"  初始: 60")
for n, b, a, d in results_down:
    print(f"  第{n}次: {b} → {a}")
print(f"  听感: {feedback_down}")

print(f"\n声音大一点 × 5:")
for n, b, a, d in results_up:
    print(f"  第{n}次: {b} → {a}")
print(f"  听感: {feedback_up}")

mpv.stop_process()
print("\n完成")
