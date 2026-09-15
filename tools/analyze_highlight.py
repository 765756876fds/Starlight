"""
离线 Highlight 分析脚本
使用 librosa + RMS Energy Peak 算法定位歌曲高潮位置

算法说明：
- 计算全曲短时能量（RMS）
- 用 20 秒滑动窗口找平均能量最高的位置
- 排除歌曲前 20% 和后 5%
- 位置加权：越靠后权重越高（高潮通常在后半段）
- confidence = heuristic score（能量峰值归一化值），不是模型概率

注意：这是离线预处理脚本，不修改正式播放业务。
"""
import os
import sys
import time
import argparse
import numpy as np
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.music.database import MusicDatabase


def analyze_highlight(song_path: str, window_sec: float = 20.0):
    """
    分析单首歌曲的高潮位置

    Args:
        song_path: FLAC 文件路径
        window_sec: 高潮窗口长度（秒）

    Returns:
        dict: {
            'start_time': float,
            'end_time': float,
            'confidence': float,
            'duration': float,
        }
        或 None（分析失败）
    """
    try:
        import librosa
    except ImportError:
        print("❌ librosa 未安装，请先运行: pip install librosa")
        return None

    try:
        # 加载音频（降采样到 22050Hz 加速）
        y, sr = librosa.load(song_path, sr=22050)
        total_duration = len(y) / sr

        # 歌曲太短，直接用 50% 位置
        if total_duration < window_sec:
            return {
                'start_time': total_duration * 0.5,
                'end_time': total_duration,
                'confidence': 0.5,  # 短歌曲置信度低
                'duration': total_duration,
            }

        # 计算短时能量（RMS）
        hop_length = 512
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]

        window_frames = int(window_sec * sr / hop_length)

        # 排除开头 20% 和结尾 5%
        start_frame = int(total_duration * 0.2 * sr / hop_length)
        end_frame = len(rms) - int(total_duration * 0.05 * sr / hop_length)

        if end_frame - start_frame < window_frames:
            # 有效范围太短，放宽限制
            start_frame = 0
            end_frame = len(rms)

        # 滑动窗口找能量最高的位置
        best_score = -1
        best_start_sec = 0

        for i in range(start_frame, end_frame - window_frames):
            avg_energy = np.mean(rms[i:i+window_frames])
            start_sec = i * hop_length / sr

            # 位置加权：越靠后权重越高（温和加权）
            position_ratio = start_sec / total_duration
            position_weight = 0.7 + position_ratio * 0.6  # 从 0.7 到 1.3

            score = avg_energy * position_weight

            if score > best_score:
                best_score = score
                best_start_sec = start_sec

        # confidence：归一化的启发式得分（不是模型概率）
        # 简单归一化：相对于最大可能得分
        max_possible = np.max(rms) * 1.3  # 最大能量 × 最大权重
        confidence = min(1.0, best_score / max_possible) if max_possible > 0 else 0.5

        return {
            'start_time': best_start_sec,
            'end_time': best_start_sec + window_sec,
            'confidence': confidence,
            'duration': total_duration,
        }

    except Exception as e:
        print(f"    ❌ 分析失败: {e}")
        return None


def get_db_path():
    """获取音乐数据库路径"""
    # 优先从配置读，默认 data/music.db
    return PROJECT_ROOT / "data" / "music.db"


def scan_songs(db: MusicDatabase):
    """从数据库获取所有歌曲"""
    cursor = db._conn.execute("SELECT id, path, title, artist FROM music")
    return [dict(row) for row in cursor.fetchall()]


def has_highlight(db: MusicDatabase, song_id: int):
    """检查是否已有 highlight"""
    cursor = db._conn.execute(
        "SELECT COUNT(*) as cnt FROM music_highlight WHERE song_id = ?",
        (song_id,)
    )
    return cursor.fetchone()["cnt"] > 0


def main():
    parser = argparse.ArgumentParser(description="离线分析歌曲高潮位置")
    parser.add_argument("--db", type=str, default=None, help="数据库路径")
    parser.add_argument("--song-id", type=int, default=None, help="只分析指定 song_id")
    parser.add_argument("--song-path", type=str, default=None, help="只分析指定文件（不写数据库）")
    parser.add_argument("--force", action="store_true", help="强制重新分析（即使已有 highlight）")
    parser.add_argument("--window-sec", type=float, default=20.0, help="高潮窗口长度（秒）")
    args = parser.parse_args()

    print("=" * 70)
    print("离线 Highlight 分析")
    print("算法: librosa + RMS Energy Peak")
    print("=" * 70)

    # 只分析单首文件（不写数据库）
    if args.song_path:
        print(f"\n分析单首文件（不写数据库）: {args.song_path}")
        result = analyze_highlight(args.song_path, args.window_sec)
        if result:
            print(f"\n✅ 结果:")
            print(f"  高潮位置: {result['start_time']:.1f}s - {result['end_time']:.1f}s")
            print(f"  歌曲时长: {result['duration']:.1f}s")
            print(f"  confidence: {result['confidence']:.3f} (heuristic score)")
        else:
            print("\n❌ 分析失败")
        return

    # 连接数据库
    db_path = args.db or str(get_db_path())
    print(f"\n数据库: {db_path}")

    if not os.path.exists(db_path):
        print(f"❌ 数据库不存在: {db_path}")
        return

    db = MusicDatabase(db_path)
    print(f"连接成功")

    # 获取歌曲列表
    if args.song_id:
        cursor = db._conn.execute(
            "SELECT id, path, title, artist FROM music WHERE id = ?",
            (args.song_id,)
        )
        row = cursor.fetchone()
        songs = [dict(row)] if row else []
        print(f"只分析 song_id={args.song_id}")
    else:
        songs = scan_songs(db)
        print(f"数据库中共 {len(songs)} 首歌曲")

    # 统计
    success = 0
    failed = 0
    skipped = 0
    t_start = time.time()

    # 分析每首歌
    for i, song in enumerate(songs, 1):
        song_id = song['id']
        path = song['path']
        title = song.get('title', 'unknown')
        artist = song.get('artist', 'unknown')

        print(f"\n[{i}/{len(songs)}] {title} - {artist}")
        print(f"  路径: {path}")

        # 检查是否已有 highlight
        if not args.force and has_highlight(db, song_id):
            print(f"  ⏭️  已有 highlight，跳过（使用 --force 重新分析）")
            skipped += 1
            continue

        # 检查文件是否存在
        if not os.path.exists(path):
            print(f"  ❌ 文件不存在")
            failed += 1
            continue

        # 分析
        result = analyze_highlight(path, args.window_sec)
        if not result:
            failed += 1
            continue

        # 保存到数据库
        try:
            # 如果 force，先删除旧的
            if args.force:
                db._conn.execute("DELETE FROM music_highlight WHERE song_id = ?", (song_id,))

            db.save_highlight(
                song_id=song_id,
                start_time=result['start_time'],
                end_time=result['end_time'],
                confidence=result['confidence'],
                source="librosa_rms_energy",
            )
            print(f"  ✅ 已保存: {result['start_time']:.1f}s - {result['end_time']:.1f}s (conf={result['confidence']:.3f})")
            success += 1
        except Exception as e:
            print(f"  ❌ 数据库写入失败: {e}")
            failed += 1

    t_end = time.time()
    total_time = t_end - t_start

    # 汇总
    print("\n" + "=" * 70)
    print("分析完成")
    print("=" * 70)
    print(f"总歌曲数: {len(songs)}")
    print(f"成功: {success}")
    print(f"失败: {failed}")
    print(f"跳过: {skipped}")
    print(f"总耗时: {total_time:.1f}s")
    if success > 0:
        print(f"平均每首: {total_time / success:.2f}s")

    db.close()
    print("\n完成！")


if __name__ == "__main__":
    main()
