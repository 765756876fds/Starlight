"""
LM Studio API Server 测试
"""
import time
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:1234/v1",
    api_key="lm-studio",
)

print('=== LM Studio API 测试 ===')
print()

# 1. 列出可用模型
print('1. 可用模型:')
models = client.models.list()
for m in models.data:
    print(f'   - {m.id}')
print()

# 2. 测试 Intent 识别
system_prompt = """你是一个意图识别助手。将用户请求转换成 JSON 格式的 Intent。

支持的意图：
- music.next: 下一首/换一首
- music.pause: 暂停播放
- music.resume: 继续播放
- music.volume_up: 音量大一点
- music.volume_down: 音量小一点
- alarm.flash: 记录闪现
- alarm.spell: 记录技能
- alarm.custom: 自定义提醒
- music.search_play: 搜索并播放

只输出 JSON，格式：{"intent": "...", "confidence": 0.95, "params": {}}
不要输出其他内容。"""

test_cases = [
    "下一首",
    "暂停音乐",
    "两分钟提醒我",
    "播放一首比较安静的音乐",
]

print('2. Intent 识别测试:')
for i, prompt in enumerate(test_cases, 1):
    print(f'   Test {i}: "{prompt}"')
    t0 = time.time()

    response = client.chat.completions.create(
        model=models.data[0].id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=256,
    )

    t1 = time.time()
    text = response.choices[0].message.content.strip()

    print(f'     结果: {text}')
    print(f'     耗时: {t1-t0:.2f}s')
    print()

print('=== 测试完成 ===')
