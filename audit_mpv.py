"""审计测试：mpv 实际控制测试"""
import os, sys, time, json
sys.path.insert(0, '.')

from app.music.mpv import MpvController

print('=== mpv 实际控制审计 ===')

mpv_path = r'D:\新建文件夹 (2)\mpv.exe'
ipc_pipe = r'\\.\pipe\audit_mpv_test'

print(f'mpv 路径: {mpv_path}')
print(f'mpv 存在: {os.path.exists(mpv_path)}')

# 1. 启动 mpv
print('\n--- 1. 启动 mpv ---')
try:
    mpv = MpvController(mpv_path, ipc_pipe)
    mpv.start()
    print(f'mpv 运行中: {mpv.is_running()}')
    print(f'管道连接: {mpv._pipe is not None}')
except Exception as e:
    print(f'启动失败: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 2. 加载文件
print('\n--- 2. loadfile 测试 ---')
test_file = r'D:\落雪\音乐文件\brave heart - 宮崎歩.flac'
print(f'测试文件: {test_file}')
print(f'文件存在: {os.path.exists(test_file)}')
try:
    mpv.loadfile(test_file)
    time.sleep(1)
    print(f'当前文件名: {mpv.get_filename()}')
    print(f'当前播放位置: {mpv.get_current_time():.2f}s')
    print(f'总时长: {mpv.get_duration():.2f}s')
    print(f'是否暂停: {mpv.is_paused()}')
    print(f'是否空闲: {mpv.is_idle()}')
except Exception as e:
    print(f'loadfile 失败: {e}')

# 3. 暂停
print('\n--- 3. pause 测试 ---')
try:
    mpv.pause()
    time.sleep(0.5)
    print(f'暂停后 is_paused: {mpv.is_paused()}')
except Exception as e:
    print(f'pause 失败: {e}')

# 4. 继续播放
print('\n--- 4. resume 测试 ---')
try:
    mpv.play()
    time.sleep(0.5)
    print(f'继续后 is_paused: {mpv.is_paused()}')
except Exception as e:
    print(f'resume 失败: {e}')

# 5. 音量
print('\n--- 5. 音量测试 ---')
try:
    mpv.set_volume(50)
    time.sleep(0.3)
    print(f'设置音量 50 后: {mpv.get_volume()}')
    mpv.set_volume(70)
    time.sleep(0.3)
    print(f'设置音量 70 后: {mpv.get_volume()}')
except Exception as e:
    print(f'音量测试失败: {e}')

# 6. seek
print('\n--- 6. seek 测试 ---')
try:
    mpv.seek(10, "absolute")
    time.sleep(0.5)
    print(f'seek 到 10s 后位置: {mpv.get_current_time():.2f}s')
except Exception as e:
    print(f'seek 失败: {e}')

# 7. next/prev（播放列表为空时的行为）
print('\n--- 7. next/prev 测试 ---')
try:
    mpv.next()
    time.sleep(0.5)
    print(f'next 后文件名: {mpv.get_filename()}')
    print(f'next 后是否空闲: {mpv.is_idle()}')
except Exception as e:
    print(f'next 测试: {e}')

# 8. stop
print('\n--- 8. stop 测试 ---')
try:
    mpv.stop()
    time.sleep(0.5)
    print(f'stop 后是否空闲: {mpv.is_idle()}')
    print(f'stop 后是否运行: {mpv.is_running()}')
except Exception as e:
    print(f'stop 失败: {e}')

# 9. 停止进程
print('\n--- 9. 停止 mpv 进程 ---')
try:
    mpv.stop_process()
    time.sleep(0.5)
    print(f'进程停止后 is_running: {mpv.is_running()}')
except Exception as e:
    print(f'停止进程失败: {e}')

print('\n=== mpv 审计完成 ===')
