"""
Thinking ON vs OFF A/B 测试
"""
import time, json
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:1234/v1",
    api_key="lm-studio",
)

MODEL = "qwen3/qwen3-4b-k_m"

system_prompt = """你是一个意图识别助手。将用户请求转换成 JSON 格式的 Intent。

支持的意图：
- music.next: 下一首/换一首
- music.pause: 暂停播放
- music.resume: 继续播放
- music.volume_up: 音量大一点
- music.volume_down: 音量小一点
- alarm.custom: 自定义提醒
- music.search_play: 搜索并播放，参数 query

只输出 JSON，格式：{"intent": "...", "confidence": 0.95, "params": {}}
不要输出其他内容。"""

test_cases = [
    "下一首",
    "暂停音乐",
    "两分钟提醒我",
    "播放一首比较安静的音乐",
]

def run_test(prompt, use_no_think=False):
    """运行单次测试"""
    sys = system_prompt
    if use_no_think:
        sys = "/no_think\n" + system_prompt

    t0 = time.time()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=256,
    )
    t1 = time.time()

    text = response.choices[0].message.content.strip()
    usage = response.usage

    # 尝试解析 JSON
    try:
        parsed = json.loads(text)
        is_valid_json = True
        intent = parsed.get("intent", "?")
    except:
        is_valid_json = False
        intent = "(非 JSON)"

    return {
        "text": text,
        "time": t1 - t0,
        "is_valid_json": is_valid_json,
        "intent": intent,
        "tokens": getattr(usage, 'total_tokens', 0) if usage else 0,
    }

print('=' * 70)
print('Thinking ON vs OFF A/B 测试')
print('=' * 70)
print()

# ===== Thinking ON =====
print('=== Thinking ON（默认） ===')
results_on = {}
for prompt in test_cases:
    print(f'  测试: "{prompt}"')
    r = run_test(prompt, use_no_think=False)
    results_on[prompt] = r
    print(f'    结果: {r["text"][:80]}')
    print(f'    耗时: {r["time"]:.2f}s | JSON合法: {r["is_valid_json"]} | Intent: {r["intent"]}')
    print()

# ===== Thinking OFF (/no_think) =====
print('=== Thinking OFF (/no_think) ===')
results_off = {}
for prompt in test_cases:
    print(f'  测试: "{prompt}"')
    r = run_test(prompt, use_no_think=True)
    results_off[prompt] = r
    print(f'    结果: {r["text"][:80]}')
    print(f'    耗时: {r["time"]:.2f}s | JSON合法: {r["is_valid_json"]} | Intent: {r["intent"]}')
    print()

# ===== 对比汇总 =====
print('=' * 70)
print('对比汇总')
print('=' * 70)
print()
print(f'{"输入":<25} {"Thinking ON":<30} {"Thinking OFF":<30}')
print('-' * 85)
for prompt in test_cases:
    on = results_on[prompt]
    off = results_off[prompt]
    on_str = f'{on["time"]:.2f}s | {on["intent"]}'
    off_str = f'{off["time"]:.2f}s | {off["intent"]}'
    print(f'{prompt:<25} {on_str:<30} {off_str:<30}')

print()
print('=== 详细对比 ===')
print()
for prompt in test_cases:
    on = results_on[prompt]
    off = results_off[prompt]
    speedup = on["time"] / off["time"] if off["time"] > 0 else 0
    print(f'"{prompt}":')
    print(f'  ON:  {on["time"]:.2f}s | JSON={on["is_valid_json"]} | 输出="{on["text"][:60]}"')
    print(f'  OFF: {off["time"]:.2f}s | JSON={off["is_valid_json"]} | 输出="{off["text"][:60]}"')
    print(f'  加速: {speedup:.1f}x')
    print()
