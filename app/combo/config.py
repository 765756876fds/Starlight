"""combos.yaml 配置加载：启动时读一次；文件不存在自动创建默认内容（盲僧+锤石）。

二期：UI 编辑保存后写回（save()），模块级 _IO_LOCK 保证 保存写文件 与 监听线程读文件 互斥。
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional

import yaml

from .models import ChampionCombos, parse_champion

try:
    from app.core.path_resolve import PROJECT_ROOT  # 打包态: exe 目录（combos.yaml 可写）
except ImportError:
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COMBO_YAML_PATH = os.path.join(str(PROJECT_ROOT), "config", "combos.yaml")

# UI 保存写文件 与 配置读取/重载 的互斥锁（防并发读写）
_IO_LOCK = threading.Lock()

# 文件不存在时的默认内容（照施工指令：盲僧 + 锤石）
DEFAULT_YAML = """\
version: 1

# 各类按键的默认延迟（毫秒），单个步骤可覆盖
defaults:
  delay_skill_ms: 50        # 技能键 Q/W/E/R 之间
  delay_item_ms: 80         # 物品栏键 1-7 之间
  delay_summoner_ms: 50     # 召唤师技能 D/F 之间

# 英雄连招库
champions:
  盲僧:
    champion_id: 64
    aliases: ["lee sin", "leesin", "李青", "盲僧", "瞎子"]
    combos:
      - trigger: "3"
        note: "摸眼位移"
        sequence:
          - { key: "3", delay_after_ms: 50 }
          - { key: "w", delay_after_ms: 0 }
          - { key: "w", delay_after_ms: 50 }
      - trigger: "4"
        note: "R闪（眼版）"
        sequence:
          - { key: "4", delay_after_ms: 50 }
          - { key: "w", delay_after_ms: 0 }
          - { key: "w", delay_after_ms: 50 }
      - trigger: "a"
        note: "4wd（插眼-摸眼-闪现补距离）"
        sequence:
          - { key: "4", delay_after_ms: 50 }
          - { key: "d", delay_after_ms: 50 }
          - { key: "w", delay_after_ms: 0 }
          - { key: "w", delay_after_ms: 50 }
  锤石:
    champion_id: 412
    aliases: ["thresh", "魂锁典狱长", "锤石"]
    combos:
      - trigger: "a"
        note: "超远丢灯笼"
        sequence:
          - { key: "d", delay_after_ms: 50 }
          - { key: "w", delay_after_ms: 50 }
          - { key: "q", delay_after_ms: 50 }
"""


class ComboConfig:
    """已加载的连招配置（启动时读一次，一期只读，不写回）。"""

    def __init__(self, champions: List[ChampionCombos], defaults: Dict[str, int],
                 path: str):
        self.champions = champions
        self.defaults = dict(defaults or {})
        self.path = path
        self.by_id: Dict[int, ChampionCombos] = {
            c.champion_id: c for c in champions if c.champion_id
        }
        self.aliases: Dict[str, ChampionCombos] = {}
        for c in champions:
            names = [c.name] + list(c.aliases)
            for a in names:
                key = (a or "").strip().lower()
                if key:
                    self.aliases.setdefault(key, c)

    @staticmethod
    def load(path: Optional[str] = None) -> "ComboConfig":
        """加载 combos.yaml；文件不存在时自动创建默认内容，绝不崩。"""
        path = path or COMBO_YAML_PATH
        if not os.path.exists(path):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with _IO_LOCK:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(DEFAULT_YAML)
                print(f"[Combo] combos.yaml 不存在，已创建默认配置: {path}")
            except Exception as e:
                print(f"[Combo] 创建默认配置失败（使用内置默认）: {e}")
        data: dict = {}
        if os.path.exists(path):
            try:
                with _IO_LOCK:
                    with open(path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[Combo] combos.yaml 解析失败（使用内置默认）: {e}")
        champions: List[ChampionCombos] = []
        raw = data.get("champions") or {}
        if isinstance(raw, dict):
            for name, d in raw.items():
                cc = parse_champion(name, d)
                if cc is not None:
                    champions.append(cc)
        cfg = ComboConfig(champions, data.get("defaults") or {}, path)
        if champions:
            print(f"[Combo] 配置加载成功: {len(champions)} 个英雄 "
                  f"({', '.join(c.name for c in champions[:10])})")
        else:
            print("[Combo] 配置加载成功（当前无英雄连招数据）")
        return cfg

    def find_by_id(self, champion_id: Optional[int]) -> Optional[ChampionCombos]:
        if not champion_id:
            return None
        return self.by_id.get(int(champion_id))

    def find_by_alias(self, name: str) -> Optional[ChampionCombos]:
        return self.aliases.get((name or "").strip().lower())

    # ---------- 序列化 / 保存（二期：UI 编辑写回） ----------

    def to_yaml_dict(self) -> dict:
        """当前内存配置 → yaml dict（不丢英雄；不手拼字符串）。"""
        return {
            "version": 1,
            "defaults": dict(self.defaults or {}),
            "champions": {f"{c.name}_{int(c.champion_id)}": c.to_dict()
                          for c in self.champions},
        }

    def save(self, path: Optional[str] = None) -> bool:
        """序列化写回 combos.yaml（加锁，与 load 互斥）。

        重写会丢失原文件注释（yaml 不保留注释），顶部保留一句说明。
        """
        path = path or self.path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with _IO_LOCK:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("# 此文件由 UI 保存时会重写，注释会丢失\n")
                    yaml.safe_dump(self.to_yaml_dict(), f,
                                  allow_unicode=True, sort_keys=False)
            return True
        except Exception as e:
            print(f"[Combo] 保存配置失败: {e}")
            return False
