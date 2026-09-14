"""审计测试：mpv IPC 修复验证"""
import os, sys, time
sys.path.insert(0, '.')

from app.music.mpv import MpvController

print('=== mpv IPC 修复验证 ===')
print()

mpv_path = r'D:\新建文件夹 (2)\mpv.exe'
ipc_pipe = r'\\.\pipe\audit_mpv_fix'

print(f'mpv 路径: {mpv_path}')
print(f'管道: {ipc_pipe}')
print()

# 1. 启动 mpv
print('--- 1. 启动 mpv ---')
try:
    mpv = MpvController(mpv_path, ipc_pipe)
    mpv.start()
    print(f'   ✅ mpv 启动成功, PID={mpv._process.pid}')
    print(f'   ✅ 写管道: {mpv._write_pipe is not None}')
    print(f'   ✅ 读管道: {mpv._read_pipe is not None}')
except Exception as e:
    print(f'   ❌ 启动失败: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)

time.sleep(1)

# 2. 获取版本（等待一下让属性观察完成）
print()
print('--- 2. 等待属性初始化 ---')
time.sleep(1)
print(f'   ✅ 空闲状态: {mpv.is_idle()}')
print(f'   ✅ 文件名: "{mpv.get_filename()}"')

# 3. 加载文件
print()
print('--- 3. loadfile 测试 ---')
test_file = r'D:\落雪\音乐文件\brave heart - 宮崎歩.flac'
print(f'   测试文件: {test_file}')
print(f'   文件存在: {os.path.exists(test_file)}')
try:
    mpv.loadfile(test_file)
    time.sleep(2)
    print(f'   ✅ loadfile 已发送')
    print(f'   当前文件名: {mpv.get_filename()}')
    print(f'   当前播放时间: {mpv.get_current_time():.2f}s')
    print(f'   总时长: {mpv.get_duration():.2f}s')
except Exception as e:
    print(f'   ❌ loadfile 失败: {e}')

# 4. 暂停
print()
print('--- 4. pause 测试 ---')
try:
    mpv.pause()
    time.sleep(0.5)
    print(f'   ✅ pause 已发送')
    print(f'   是否暂停: {mpv.is_paused()}')
except Exception as e:
    print(f'   ❌ pause 失败: {e}')

# 5. 继续播放
print()
print('--- 5. play (继续) 测试 ---')
try:
    mpv.play()
    time.sleep(0.5)
    print(f'   ✅ play 已发送')
    print(f'   是否暂停: {mpv.is_paused()}')
except Exception as e:
    print(f'   ❌ play 失败: {e}')

# 6. 音量
print()
print('--- 6. 音量测试 ---')
try:
    mpv.set_volume(50)
    time.sleep(0.3)
    print(f'   ✅ 设置音量 50')
    print(f'   当前音量: {mpv.get_volume()}')
except Exception as e:
    print(f'   ❌ 音量测试失败: {e}')

# 7. seek
print()
print('--- 7. seek 测试 ---')
try:
    mpv.seek(10, "absolute")
    time.sleep(0.5)
    print(f'   ✅ seek 到 10s')
    print(f'   当前播放时间: {mpv.get_current_time():.2f}s')
except Exception as e:
    print(f'   ❌ seek 失败: {e}')

# 8. next/prev
print()
print('--- 8. next/prev 测试 ---')
try:
    mpv.next()
    time.sleep(0.5)
    print(f'   ✅ next 已发送')
    print(f'   空闲状态: {mpv.is_idle()}')
except Exception as e:
    print(f'   ❌ next 测试失败: {e}')

# 9. stop
print()
print('--- 9. stop 测试 ---')
try:
    mpv.stop()
    time.sleep(0.5)
    print(f'   ✅ stop 已发送')
    print(f'   空闲状态: {mpv.is_idle()}')
except Exception as e:
    print(f'   ❌ stop 失败: {e}')

# 10. 停止进程
print()
print('--- 10. 停止 mpv 进程 ---')
try:
    mpv.stop_process()
    time.sleep(0.5)
    print(f'   ✅ 进程已停止')
    print(f'   是否运行: {mpv.is_running()}')
except Exception as e:
    print(f'   ❌ 停止失败: {e}')

print()
print('=== mpv IPC 测试完成 ===')
