import os, sys, time, json
sys.path.insert(0, '.')
import allin1_infer

song_path = r'D:\落雪\音乐文件\K歌之王 - 陈奕迅.flac'
out_dir = 'tools/ai1_poc_output/test1'
os.makedirs(out_dir, exist_ok=True)

print('开始分析（CPU 模式，可能需要几分钟）...')
t0 = time.time()

result = allin1_infer.analyze(
    paths=song_path,
    out_dir=out_dir,
    visualize=True,
    device='cpu',
    multiprocess=False,
)

t1 = time.time()
print(f'分析完成，耗时: {t1 - t0:.1f}s')

# 查看 AnalysisResult 的属性
print(f'\nAnalysisResult 类型: {type(result)}')
print(f'AnalysisResult 属性: {[a for a in dir(result) if not a.startswith("_")]}')

# 获取 segments
segments = result.segments if hasattr(result, 'segments') else []
print(f'\n结构段数: {len(segments)}')
for i, seg in enumerate(segments):
    label = seg.label if hasattr(seg, 'label') else seg['label']
    start = seg.start if hasattr(seg, 'start') else seg['start']
    end = seg.end if hasattr(seg, 'end') else seg['end']
    print(f'  [{i}] {label}: {start:.1f}s - {end:.1f}s')

# 保存结果
output = {
    'song': song_path,
    'time_sec': t1 - t0,
    'segments': [
        {
            'label': seg.label if hasattr(seg, 'label') else seg['label'],
            'start': seg.start if hasattr(seg, 'start') else seg['start'],
            'end': seg.end if hasattr(seg, 'end') else seg['end'],
        }
        for seg in segments
    ]
}

with open('tools/ai1_poc_output/test1_result.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'\n结果已保存: tools/ai1_poc_output/test1_result.json')
