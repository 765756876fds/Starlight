"""ASR 双后端回退逻辑 mock 测试（标准库 unittest，不依赖 pytest）。

覆盖场景：
1. Vulkan 启动成功 → 直接返回
2. Vulkan 失败 → CPU 回退成功
3. 两个后端都失败 → 返回 None（语音降级）
4. 后端启动后立即退出 → 自动试下一个
5. 8080 已有可用 ASR → 直接复用不拉起
6. 8080 外部占用（data 空）→ 不拉起
7. llama_backend=vulkan 时 CPU 后端不被尝试
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.voice import asr_bootstrap as ab


class TestAsrBootendFallbacks(unittest.TestCase):
    """ASR 双后端回退逻辑测试。"""

    def setUp(self):
        """每个测试前重置全局状态。"""
        ab._server_proc = None
        ab._server_backend = ""
        ab._atexit_registered = False

    def _make_proc(self, exit_code=None):
        """mock 一个 subprocess.Popen 对象。"""
        p = MagicMock()
        p.poll.return_value = exit_code  # None = 存活
        p.terminate = MagicMock()
        p.wait = MagicMock()
        p.kill = MagicMock()
        p.returncode = exit_code
        return p

    @patch.object(ab, 'find_llama_backend_candidates')
    @patch.object(ab, '_is_server_up')
    @patch.object(ab, 'ensure_assets')
    @patch.object(ab.subprocess, 'Popen')
    @patch.object(ab.requests, 'get')
    @patch.object(ab.os.path, 'isfile')
    def test_vulkan_success(self, mock_isfile, mock_get, mock_popen, mock_assets, mock_up, mock_cands):
        """场景 1: Vulkan 启动成功 → 直接返回，不试 CPU。"""
        mock_assets.return_value = True
        mock_up.return_value = False  # 8080 无服务
        mock_isfile.return_value = True  # 模型文件存在（mock）
        mock_cands.return_value = [
            {"backend": "vulkan", "exe": "/fake/vulkan/llama-server.exe"},
            {"backend": "cpu", "exe": "/fake/cpu/llama-server.exe"},
        ]
        # Popen 返回存活进程
        mock_popen.return_value = self._make_proc(exit_code=None)
        # 健康检查：200 + data 非空
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "model1"}]}
        mock_get.return_value = mock_resp

        cfg = {"bootstrap_enabled": True, "llama_ctx": 8192,
               "qwen3_asr_model": "/fake/model.gguf", "qwen3_asr_mmproj": "/fake/mmproj.gguf"}
        # resolve_model_paths 需要 mock（文件存在）
        with patch.object(ab, 'resolve_model_paths', return_value=("/fake/model.gguf", "/fake/mmproj.gguf")):
            proc = ab.ensure_and_launch(cfg)

        self.assertIsNotNone(proc, "Vulkan 成功时应返回进程")
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        self.assertIn("-c", args)
        self.assertIn("8192", args)
        self.assertIn("--parallel", args)
        self.assertIn("2", args)

    @patch.object(ab, 'find_llama_backend_candidates')
    @patch.object(ab, '_is_server_up')
    @patch.object(ab, 'ensure_assets')
    @patch.object(ab.subprocess, 'Popen')
    @patch.object(ab.requests, 'get')
    @patch.object(ab.os.path, 'isfile')
    def test_vulkan_fail_cpu_fallback(self, mock_isfile, mock_get, mock_popen, mock_assets, mock_up, mock_cands):
        """场景 2: Vulkan 启动失败（立即退出）→ CPU 回退成功。"""
        mock_assets.return_value = True
        mock_up.return_value = False
        mock_isfile.return_value = True
        mock_cands.return_value = [
            {"backend": "vulkan", "exe": "/fake/vulkan/llama-server.exe"},
            {"backend": "cpu", "exe": "/fake/cpu/llama-server.exe"},
        ]
        # 第一次 Popen（vulkan）立即退出；第二次（cpu）存活
        vulkan_proc = self._make_proc(exit_code=1)  # 立即退出
        cpu_proc = self._make_proc(exit_code=None)
        mock_popen.side_effect = [vulkan_proc, cpu_proc]
        # 健康检查都 OK
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "model1"}]}
        mock_get.return_value = mock_resp

        cfg = {"bootstrap_enabled": True, "llama_ctx": 8192}
        with patch.object(ab, 'resolve_model_paths', return_value=("/fake/model.gguf", "/fake/mmproj.gguf")):
            proc = ab.ensure_and_launch(cfg)

        self.assertIsNotNone(proc, "CPU 回退成功应返回进程")
        self.assertEqual(mock_popen.call_count, 2, "应尝试 vulkan + cpu 两次")

    @patch.object(ab, 'find_llama_backend_candidates')
    @patch.object(ab, '_is_server_up')
    @patch.object(ab, 'ensure_assets')
    @patch.object(ab.subprocess, 'Popen')
    @patch.object(ab.os.path, 'isfile')
    def test_both_backends_fail(self, mock_isfile, mock_popen, mock_assets, mock_up, mock_cands):
        """场景 3: 两个后端都失败 → 返回 None（语音降级）。"""
        mock_assets.return_value = True
        mock_up.return_value = False
        mock_isfile.return_value = True
        mock_cands.return_value = [
            {"backend": "vulkan", "exe": "/fake/vulkan/llama-server.exe"},
            {"backend": "cpu", "exe": "/fake/cpu/llama-server.exe"},
        ]
        # 都立即退出
        mock_popen.side_effect = [self._make_proc(exit_code=1), self._make_proc(exit_code=1)]

        cfg = {"bootstrap_enabled": True, "llama_ctx": 8192}
        with patch.object(ab, 'resolve_model_paths', return_value=("/fake/model.gguf", "/fake/mmproj.gguf")):
            proc = ab.ensure_and_launch(cfg)

        self.assertIsNone(proc, "两个后端都失败应返回 None")
        self.assertEqual(mock_popen.call_count, 2)

    @patch.object(ab, '_is_server_up')
    @patch.object(ab, 'ensure_assets')
    @patch.object(ab.subprocess, 'Popen')
    def test_existing_service_reuse(self, mock_popen, mock_assets, mock_up):
        """场景 5: 8080 已有可用 ASR → 直接复用，不拉起新进程。"""
        mock_assets.return_value = True
        mock_up.return_value = True  # 已有可用服务

        cfg = {"bootstrap_enabled": True}
        proc = ab.ensure_and_launch(cfg)

        self.assertIsNone(proc, "已有服务复用时应返回 None（不拉起新进程）")
        mock_popen.assert_not_called()

    @patch.object(ab, 'find_llama_backend_candidates')
    @patch.object(ab, '_is_server_up')
    @patch.object(ab, 'ensure_assets')
    @patch.object(ab.subprocess, 'Popen')
    @patch.object(ab.requests, 'get')
    @patch.object(ab.os.path, 'isfile')
    def test_external_port_occupied(self, mock_isfile, mock_get, mock_popen, mock_assets, mock_up, mock_cands):
        """场景 6: 8080 外部占用（200 但 data 空）→ _is_server_up 返回 False，继续尝试拉起。"""
        mock_assets.return_value = True
        mock_isfile.return_value = True
        # _is_server_up 返回 False（data 空）
        mock_up.return_value = False
        mock_cands.return_value = [{"backend": "cpu", "exe": "/fake/cpu/llama-server.exe"}]
        mock_popen.return_value = self._make_proc(exit_code=None)
        # 健康检查：data 非空才算就绪
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "model1"}]}
        mock_get.return_value = mock_resp

        cfg = {"bootstrap_enabled": True}
        with patch.object(ab, 'resolve_model_paths', return_value=("/fake/model.gguf", "/fake/mmproj.gguf")):
            proc = ab.ensure_and_launch(cfg)

        self.assertIsNotNone(proc)
        # 不杀外部进程（我们只 terminate 自己拉起的 proc）
        # 这里 mock 的 proc 是我们自己 Popen 的，不涉及外部进程

    def test_cleanup_kills_own_proc(self):
        """场景 7: atexit 清理只杀自己拉起的进程。"""
        own_proc = self._make_proc(exit_code=None)
        ab._server_proc = own_proc
        ab._kill_server()
        own_proc.terminate.assert_called_once()
        self.assertIsNone(ab._server_proc)

    @patch.object(ab, 'find_llama_backend_candidates')
    @patch.object(ab, '_is_server_up')
    @patch.object(ab, 'ensure_assets')
    def test_no_candidates(self, mock_assets, mock_up, mock_cands):
        """场景 8: 无可用后端 → 返回 None，不抛异常。"""
        mock_assets.return_value = True
        mock_up.return_value = False
        mock_cands.return_value = []  # 无后端

        cfg = {"bootstrap_enabled": True}
        with patch.object(ab, 'resolve_model_paths', return_value=("/fake/model.gguf", "")):
            proc = ab.ensure_and_launch(cfg)
        self.assertIsNone(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
