"""
Qwen3-ASR 真人测试统计分析
"""
import json
import re
import os

RESULT_FILE = r"C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent\tools\asr_records\test_results.json"

with open(RESULT_FILE, 'r', encoding='utf-8') as f:
    results = json.load(f)

def classify(expected, actual):
    """分类识别结果"""
    # 去掉标点和空格
    e = re.sub(r'[。，！？、\s《》]', '', expected)
    a = re.sub(r'[。，！？、\s《》]', '', actual)
    
    if not a or len(a) < 2:
        return "FAILED"
    
    # 泰文等非中文
    if not re.match(r'^[\u4e00-\u9fff]+$', a):
        return "FAILED"
    
    if e == a:
        return "EXACT"
    
    # 计算编辑距离
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, e, a).ratio()
    
    if ratio >= 0.8:
        return "INTENT_OK"
    
    return "WRONG"

# 分类统计
categories = {
    "普通": {"EXACT": 0, "INTENT_OK": 0, "WRONG": 0, "FAILED": 0, "total": 0},
    "音乐": {"EXACT": 0, "INTENT_OK": 0, "WRONG": 0, "FAILED": 0, "total": 0},
    "LoL": {"EXACT": 0, "INTENT_OK": 0, "WRONG": 0, "FAILED": 0, "total": 0},
    "英雄": {"EXACT": 0, "INTENT_OK": 0, "WRONG": 0, "FAILED": 0, "total": 0},
}

all_latencies = []
all_results = []

for item in results:
    name = item["name"]
    expected = item["expected"]
    
    # 确定分类
    if name.startswith("普通"):
        cat = "普通"
    elif name.startswith("音乐"):
        cat = "音乐"
    elif name.startswith("LoL"):
        cat = "LoL"
    elif name.startswith("英雄"):
        cat = "英雄"
    else:
        cat = "普通"
    
    for run in item["runs"]:
        text = run["text"]
        latency = run["latency_ms"]
        
        cls = classify(expected, text)
        categories[cat][cls] += 1
        categories[cat]["total"] += 1
        all_latencies.append(latency)
        all_results.append({
            "name": name,
            "expected": expected,
            "actual": text,
            "class": cls,
            "latency_ms": latency,
        })

# 汇总
total = len(all_latencies)
total_exact = sum(c["EXACT"] for c in categories.values())
total_intent = sum(c["INTENT_OK"] for c in categories.values())
total_wrong = sum(c["WRONG"] for c in categories.values())
total_failed = sum(c["FAILED"] for c in categories.values())

all_latencies.sort()
p50 = all_latencies[len(all_latencies) // 2]
p95 = all_latencies[int(len(all_latencies) * 0.95)]
max_lat = all_latencies[-1]
min_lat = all_latencies[0]
avg_lat = sum(all_latencies) / len(all_latencies)

print("=" * 60)
print("Qwen3-ASR 真人测试统计")
print("=" * 60)
print()

print(f"总测试次数: {total}")
print(f"EXACT: {total_exact} ({total_exact/total*100:.1f}%)")
print(f"INTENT_OK: {total_intent} ({total_intent/total*100:.1f}%)")
print(f"WRONG: {total_wrong} ({total_wrong/total*100:.1f}%)")
print(f"FAILED: {total_failed} ({total_failed/total*100:.1f}%)")
print()
print(f"Exact rate: {total_exact/total*100:.1f}%")
print(f"Intent-preserving rate: {(total_exact+total_intent)/total*100:.1f}%")
print()
print(f"延迟统计:")
print(f"  最快: {min_lat:.0f}ms")
print(f"  P50: {p50:.0f}ms")
print(f"  P95: {p95:.0f}ms")
print(f"  平均: {avg_lat:.0f}ms")
print(f"  最大: {max_lat:.0f}ms")

print()
print("=" * 60)
print("分类统计")
print("=" * 60)
for cat, data in categories.items():
    t = data["total"]
    if t == 0:
        continue
    exact = data["EXACT"]
    intent = data["INTENT_OK"]
    print(f"\n{cat} ({t}次):")
    print(f"  EXACT: {exact} ({exact/t*100:.1f}%)")
    print(f"  INTENT_OK: {intent} ({intent/t*100:.1f}%)")
    print(f"  WRONG: {data['WRONG']} ({data['WRONG']/t*100:.1f}%)")
    print(f"  FAILED: {data['FAILED']} ({data['FAILED']/t*100:.1f}%)")
    print(f"  Intent OK rate: {(exact+intent)/t*100:.1f}%")

# 列出所有错误
print()
print("=" * 60)
print("错误/异常详情")
print("=" * 60)
for r in all_results:
    if r["class"] != "EXACT":
        print(f"  [{r['class']}] {r['name']}: 预期「{r['expected']}」→ 实际「{r['actual'][:30]}」")
