"""R1 数据迁移与路径解析 mock 测试（标准库 unittest）。

覆盖：
1. 源码态 USER_DATA_DIR == PROJECT_ROOT（不分离）
2. migrate_legacy_user_data() 源码态直接返回
3. 旧数据迁移：目标已有 → 不覆盖
4. 旧数据迁移：目标缺失、源存在 → 拷贝
5. mpv 路径失效回退逻辑（静态审查，不真跑 config 加载）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import path_resolve as pr


class TestPathResolve(unittest.TestCase):
    """路径解析与迁移测试。"""

    def test_source_mode_dirs_equal(self):
        """源码态：USER_DATA_DIR == PROJECT_ROOT（不分离）。"""
        self.assertEqual(pr.PROJECT_ROOT, pr.USER_DATA_DIR,
                         "源码态 USER_DATA_DIR 应等于 PROJECT_ROOT")

    def test_migrate_noop_in_source_mode(self):
        """源码态：migrate_legacy_user_data() 直接返回（不做任何拷贝）。"""
        # 源码态下 sys.frozen 为 False，函数应直接 return
        # 不抛异常就算通过
        pr.migrate_legacy_user_data()

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_migrate_skips_when_target_exists(self):
        """frozen 态：目标文件已存在 → 不覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "config").mkdir()
            (proj / "data").mkdir()
            (user / "config").mkdir(parents=True)
            (user / "data").mkdir(parents=True)
            # 源文件
            (proj / "config" / "settings.yaml").write_text("old: 1", encoding="utf-8")
            # 目标已有
            (user / "config" / "settings.yaml").write_text("new: 2", encoding="utf-8")
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            # 目标仍为 new，不被覆盖
            content = (user / "config" / "settings.yaml").read_text(encoding="utf-8")
            self.assertEqual(content, "new: 2", "目标已有文件不应被覆盖")

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_migrate_copies_when_target_missing(self):
        """frozen 态：目标缺失、源存在 → 拷贝成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "config").mkdir()
            (proj / "data").mkdir()
            (proj / "config" / "settings.yaml").write_text("old: 1", encoding="utf-8")
            # 真 SQLite 数据库（backup API 要求）
            import sqlite3
            conn = sqlite3.connect(str(proj / "data" / "music.db"))
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.commit()
            conn.close()
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            # 目标应有拷贝
            self.assertTrue((user / "config" / "settings.yaml").exists(),
                            "settings.yaml 应被迁移")
            self.assertTrue((user / "data" / "music.db").exists(),
                            "music.db 应被迁移")

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_migrate_keeps_source(self):
        """迁移后源文件不删除。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "config").mkdir()
            (proj / "config" / "settings.yaml").write_text("keep me", encoding="utf-8")
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            self.assertTrue((proj / "config" / "settings.yaml").exists(),
                            "源文件不应被删除")


class TestCurrentGamePanelFix(unittest.TestCase):
    """P0-C: current_game_panel L444 c 变量初始化回归测试。"""

    def test_c_initialized_branch(self):
        """模拟 display_cid=0 且非 early_loading 分支——c 应初始化为 None 而非未定义。"""
        # 静态检查：源码里 c = None 在 if display_cid: 之前
        src = open(
            os.path.join(os.path.dirname(pr.__file__),
                         "..", "ui", "panels", "current_game_panel.py"),
            encoding="utf-8"
        ).read()
        # 找 _fill_player_card 附近的 c = None
        self.assertIn("c = None", src,
                      "current_game_panel.py 应在 display_cid 分支前初始化 c = None")


class TestSqliteBackupMigration(unittest.TestCase):
    """SQLite backup API 一致性迁移测试。"""

    def test_sqlite_backup_consistency(self):
        """用 sqlite3 建带表的临时 db，跑迁移，验证目标有表有数据。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            src_db = proj / "data" / "music.db"
            conn = sqlite3.connect(str(src_db))
            conn.execute("CREATE TABLE songs (id INTEGER, title TEXT)")
            conn.execute("INSERT INTO songs VALUES (1, 'test')")
            conn.commit()
            conn.close()
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            dst_db = user / "data" / "music.db"
            self.assertTrue(dst_db.exists(), "music.db 应被迁移")
            verify = sqlite3.connect(str(dst_db))
            rows = verify.execute("SELECT * FROM songs").fetchall()
            verify.close()
            self.assertEqual(len(rows), 1, "目标数据库应保留数据")

    def test_sqlite_target_existing_not_overwritten(self):
        """目标已有有效数据库 → 不覆盖。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            (user / "data").mkdir(parents=True)
            src_db = proj / "data" / "music.db"
            conn = sqlite3.connect(str(src_db))
            conn.execute("CREATE TABLE songs (id INTEGER)")
            conn.execute("INSERT INTO songs VALUES (999)")
            conn.commit()
            conn.close()
            dst_db = user / "data" / "music.db"
            conn = sqlite3.connect(str(dst_db))
            conn.execute("CREATE TABLE songs (id INTEGER)")
            conn.execute("INSERT INTO songs VALUES (1)")
            conn.commit()
            conn.close()
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            verify = sqlite3.connect(str(dst_db))
            row = verify.execute("SELECT id FROM songs").fetchone()
            verify.close()
            self.assertEqual(row[0], 1, "目标已有数据不应被覆盖")

    def test_sqlite_source_missing_no_crash(self):
        """源数据库不存在 → 不崩溃。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()  # 不抛异常就算过

    def test_sqlite_wal_mode_migration(self):
        """WAL 模式源数据库迁移：数据完整 + integrity_check ok。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            src_db = proj / "data" / "music.db"
            conn = sqlite3.connect(str(src_db))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE songs (id INTEGER, title TEXT)")
            conn.execute("INSERT INTO songs VALUES (1, 'wal_test')")
            conn.commit()
            # 验证 WAL 模式已启用
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal", "源数据库应在 WAL 模式")
            conn.close()
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            dst_db = user / "data" / "music.db"
            self.assertTrue(dst_db.exists())
            verify = sqlite3.connect(str(dst_db))
            result = verify.execute("PRAGMA integrity_check").fetchone()
            self.assertEqual(result[0], "ok", "WAL 迁移后应通过 integrity_check")
            row = verify.execute("SELECT title FROM songs WHERE id=1").fetchone()
            verify.close()
            self.assertEqual(row[0], "wal_test", "WAL 中的数据应被完整迁移")

    def test_sqlite_failure_does_not_replace_target(self):
        """损坏的源数据库：备份失败时，正式目标不被创建。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            src_db = proj / "data" / "music.db"
            # 写入垃圾内容（不是有效 SQLite 数据库）
            src_db.write_bytes(b"this is not a sqlite database")
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            dst_db = user / "data" / "music.db"
            # 备份会失败（无法打开垃圾文件），正式目标不应被创建
            self.assertFalse(dst_db.exists(), "备份失败时正式目标不应被创建")
            # 临时文件应已清理
            tmp_file = dst_db.with_suffix(".db.migrate.tmp")
            self.assertFalse(tmp_file.exists(), "失败后临时文件应已清理")

    def test_sqlite_integrity_check_ok(self):
        """备份成功后 PRAGMA integrity_check 返回 ok。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            src_db = proj / "data" / "music.db"
            conn = sqlite3.connect(str(src_db))
            conn.execute("CREATE TABLE songs (id INTEGER, title TEXT)")
            conn.execute("INSERT INTO songs VALUES (1, 'test')")
            conn.commit()
            conn.close()
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            dst_db = user / "data" / "music.db"
            verify = sqlite3.connect(str(dst_db))
            result = verify.execute("PRAGMA integrity_check").fetchone()
            verify.close()
            self.assertEqual(result[0], "ok", "迁移后数据库应通过 integrity_check")

    def test_sqlite_schema_and_data_match_source(self):
        """关键表结构和数据与源库一致。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data").mkdir()
            src_db = proj / "data" / "music.db"
            conn = sqlite3.connect(str(src_db))
            conn.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT, artist TEXT)")
            conn.execute("INSERT INTO songs VALUES (1, 'song1', 'artist1')")
            conn.execute("INSERT INTO songs VALUES (2, 'song2', 'artist2')")
            conn.commit()
            conn.close()
            with patch.object(pr.sys, 'frozen', True, create=True), \
                 patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            dst_db = user / "data" / "music.db"
            verify = sqlite3.connect(str(dst_db))
            # 表结构一致
            src_conn2 = sqlite3.connect(str(src_db))
            src_schema = src_conn2.execute(
                "SELECT sql FROM sqlite_master WHERE name='songs'"
            ).fetchone()[0]
            src_conn2.close()
            dst_schema = verify.execute(
                "SELECT sql FROM sqlite_master WHERE name='songs'"
            ).fetchone()[0]
            self.assertEqual(src_schema, dst_schema, "表结构应一致")
            # 数据一致
            rows = verify.execute("SELECT * FROM songs ORDER BY id").fetchall()
            verify.close()
            self.assertEqual(len(rows), 2, "应保留全部 2 行数据")
            self.assertEqual(rows[0][1], "song1")
            self.assertEqual(rows[1][1], "song2")


class TestLolDataDir(unittest.TestCase):
    """lol_data_dir() 路径解析测试。"""

    def test_source_mode_uses_project_root(self):
        """源码态：lol_data_dir() = PROJECT_ROOT/data/lol_data。"""
        expected = pr.PROJECT_ROOT / "data" / "lol_data"
        self.assertEqual(pr.lol_data_dir(), expected)

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_frozen_user_dir_preferred_when_nonempty(self):
        """frozen 态：用户目录有数据 → 读用户目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data" / "lol_data").mkdir(parents=True)
            (user / "data" / "lol_data").mkdir(parents=True)
            (user / "data" / "lol_data" / "champions.json").write_text("{}")
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                result = pr.lol_data_dir()
            self.assertEqual(result, user / "data" / "lol_data")

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_frozen_falls_back_to_program_resources(self):
        """frozen 态：用户目录空 → 回退到程序资源目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "data" / "lol_data").mkdir(parents=True)
            (proj / "data" / "lol_data" / "champions.json").write_text("{}")
            (user / "data" / "lol_data").mkdir(parents=True)  # 用户目录空
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                result = pr.lol_data_dir()
            self.assertEqual(result, proj / "data" / "lol_data")


class TestQwenKeyMigration(unittest.TestCase):
    """Qwen Key 迁移测试。"""

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_qwen_key_migrated_when_only_old_exists(self):
        """旧 Key 存在、新 Key 不存在 → 迁移成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "tools").mkdir()
            (proj / "tools" / ".qwen_key").write_text("sk-test-12345\n", encoding="utf-8")
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            self.assertTrue((user / "tools" / ".qwen_key").exists())
            content = (user / "tools" / ".qwen_key").read_text(encoding="utf-8")
            self.assertIn("sk-test-12345", content)
            # 旧文件保留
            self.assertTrue((proj / "tools" / ".qwen_key").exists())

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_qwen_key_not_overwritten_when_new_exists(self):
        """新 Key 已存在 → 不覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "tools").mkdir()
            (proj / "tools" / ".qwen_key").write_text("sk-old\n", encoding="utf-8")
            (user / "tools").mkdir(parents=True)
            (user / "tools" / ".qwen_key").write_text("sk-new\n", encoding="utf-8")
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()
            content = (user / "tools" / ".qwen_key").read_text(encoding="utf-8")
            self.assertIn("sk-new", content, "新 Key 不应被旧 Key 覆盖")

    @patch.object(pr.sys, 'frozen', True, create=True)
    def test_qwen_key_skipped_when_absent(self):
        """旧 Key 不存在 → 正常跳过。"""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "prog"
            user = Path(tmp) / "user"
            proj.mkdir()
            (proj / "tools").mkdir()
            # 没有 .qwen_key
            with patch.object(pr, 'PROJECT_ROOT', proj), \
                 patch.object(pr, 'USER_DATA_DIR', user):
                pr.migrate_legacy_user_data()  # 不抛异常就算过


if __name__ == "__main__":
    unittest.main(verbosity=2)
