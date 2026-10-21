"""修4：数据库损坏 → 启动崩溃 修复测试。

损坏 db → _recover_corrupt：主 db + -wal + -shm 全部重命名保留 + 新建空库。
"""
import pytest

pytestmark = pytest.mark.unit

import os

from app.music.database import MusicDatabase


def _corrupt(path):
    with open(path, "wb") as f:
        f.write(b"this is not a sqlite database file, definitely corrupt")


def _corrupt_backups(tmp_path, prefix):
    return [n for n in os.listdir(str(tmp_path)) if n.startswith(prefix)]


class TestDbRecover:
    def test_corrupt_db_recovers(self, tmp_path):
        """损坏 db → 构造不抛、新库可查、主文件已备份。"""
        db_path = str(tmp_path / "music.db")
        _corrupt(db_path)
        db = MusicDatabase(db_path)  # 不应抛异常
        cur = db._conn.execute("SELECT count(*) FROM music")
        assert cur.fetchone()[0] == 0
        assert _corrupt_backups(tmp_path, "music.db.corrupt.")
        db.close()

    def test_healthy_db_no_backup(self, tmp_path):
        """正常 db → 不产生 corrupt 备份。"""
        db_path = str(tmp_path / "ok.db")
        db = MusicDatabase(db_path)
        assert _corrupt_backups(tmp_path, "ok.db.corrupt.") == []
        db.close()

    def test_corrupt_with_wal_shm_all_isolated(self, tmp_path):
        """主 db + -wal + -shm 全损坏 → 恢复成功：主文件备份 + 新库可用。

        注：恢复后新库以 WAL 模式运行，会创建自己的 wal.db-wal/-shm，
        故此处只断言主文件备份 + 新库可查询；三件套 rename 兜底逻辑由
        test_recover_corrupt_renames_wal_shm 单独覆盖。
        """
        db_path = str(tmp_path / "wal.db")
        _corrupt(db_path)
        with open(db_path + "-wal", "wb") as f:
            f.write(b"junk-wal")
        with open(db_path + "-shm", "wb") as f:
            f.write(b"junk-shm")
        db = MusicDatabase(db_path)
        assert _corrupt_backups(tmp_path, "wal.db.corrupt.")
        cur = db._conn.execute("SELECT count(*) FROM music")
        assert cur.fetchone()[0] == 0
        db.close()

    def test_recover_corrupt_renames_wal_shm(self, tmp_path):
        """_recover_corrupt 循环：主 db + -wal + -shm 逐一 rename（跨平台兜底）。

        绕过 __init__（不触发 sqlite3 connect/close 的伴生文件清理），
        直接验证循环的 rename 逻辑本身。
        """
        db_path = str(tmp_path / "wal.db")
        _corrupt(db_path)
        with open(db_path + "-wal", "wb") as f:
            f.write(b"junk-wal")
        with open(db_path + "-shm", "wb") as f:
            f.write(b"junk-shm")
        db = MusicDatabase.__new__(MusicDatabase)
        db.db_path = db_path
        db._recover_corrupt()
        names = os.listdir(str(tmp_path))
        assert _corrupt_backups(tmp_path, "wal.db.corrupt.")
        assert _corrupt_backups(tmp_path, "wal.db-wal.corrupt.")
        assert _corrupt_backups(tmp_path, "wal.db-shm.corrupt.")
        # 原位置三件套均被移走
        assert "wal.db" not in names
        assert "wal.db-wal" not in names
        assert "wal.db-shm" not in names

    def test_recovered_db_crud_works(self, tmp_path):
        """恢复后的新库 CRUD 可用（插入 + 查询）。"""
        db_path = str(tmp_path / "crud.db")
        _corrupt(db_path)
        db = MusicDatabase(db_path)
        db._conn.execute(
            "INSERT INTO music (path, filename, title) VALUES (?, ?, ?)",
            ("/x/a.flac", "a.flac", "A"),
        )
        db._conn.commit()
        cur = db._conn.execute("SELECT title FROM music WHERE path=?", ("/x/a.flac",))
        assert cur.fetchone()["title"] == "A"
        db.close()
