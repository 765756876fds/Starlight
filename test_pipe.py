"""快速测试 mpv 管道连接"""
import os, time, subprocess, sys

pipe = r'\\.\pipe\audit_test3'
mpv_path = r'D:\新建文件夹 (2)\mpv.exe'

print(f'管道路径: {pipe}')
print(f'mpv 路径: {mpv_path}')

# 启动 mpv
proc = subprocess.Popen(
    [mpv_path, '--no-video', '--no-terminal', '--force-window=no',
     f'--input-ipc-server={pipe}', '--idle=yes'],
    creationflags=subprocess.CREATE_NO_WINDOW,
)
print(f'mpv PID: {proc.pid}')

# 等待管道
for i in range(20):
    time.sleep(0.25)
    exists = os.path.exists(pipe)
    print(f'  等待 {i*0.25:.1f}s: 管道存在={exists}')
    if exists:
        break

# 尝试打开
try:
    f = open(pipe, 'r+b', buffering=0)
    print('管道打开成功!')
    # 发送一个命令
    import json
    cmd = json.dumps({"command": ["get_property", "mpv-version"]}) + '\n'
    f.write(cmd.encode())
    time.sleep(0.5)
    # 读取响应
    import select
    data = f.read(4096)
    print(f'响应: {data}')
    f.close()
except Exception as e:
    print(f'管道打开失败: {type(e).__name__}: {e}')

# 清理
proc.terminate()
proc.wait(timeout=3)
print('测试完成')
