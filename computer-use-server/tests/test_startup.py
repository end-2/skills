from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from mock_backend import MockBackend
from platform_support import private_file, lock_file
import server


class StartupTests(unittest.TestCase):
    def test_default_startup_detects_os_and_creates_only_that_backend(self):
        for os_name, backend_name in (("Windows", "windows"), ("Darwin", "macos"), ("Linux", "linux")):
            with self.subTest(os_name=os_name), tempfile.TemporaryDirectory() as directory:
                backend = MockBackend()
                with patch("sys.argv", ["server.py", "--token-file", str(Path(directory) / "token")]), \
                        patch("backend_factory.platform.system", return_value=os_name), \
                        patch("server.default_state_dir", return_value=Path(directory)), \
                        patch("server.create_backend", return_value=backend) as create, \
                        patch("server.LocalHTTPServer") as http, contextlib.redirect_stdout(io.StringIO()) as output:
                    http.return_value.__enter__.return_value.server_port = 12345
                    server.main()
                create.assert_called_once_with(backend_name)
                self.assertIn(f"os={os_name} backend={backend_name}", output.getvalue())

    def test_native_startup_refuses_second_broker_even_with_different_token_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with private_file(root / "desktop.lock", "a+", create=True) as stream, lock_file(stream), \
                    patch("sys.argv", ["server.py", "--token-file", str(root / "different-token")]), \
                    patch("server.default_state_dir", return_value=root), \
                    patch("server.create_backend") as create, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    server.main()
            self.assertEqual(caught.exception.code, 1)
            create.assert_not_called()

    def test_failed_backend_startup_releases_singleton_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sys.argv", ["server.py", "--token-file", str(root / "token")]), \
                    patch("server.default_state_dir", return_value=root), \
                    patch("server.create_backend", side_effect=BackendError("backend_unavailable", "Missing dependency")), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit):
                    server.main()
            self.assertIn("backend_unavailable", output.getvalue())
            with private_file(root / "desktop.lock", "a+") as stream, lock_file(stream, blocking=False):
                pass

    def test_concurrent_token_creation_keeps_one_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            with ThreadPoolExecutor(max_workers=8) as pool:
                tokens = list(pool.map(lambda _: server.load_or_create_token(path), range(8)))
            self.assertEqual(len(set(tokens)), 1)
            self.assertGreaterEqual(len(tokens[0]), 32)
            with private_file(path, "r") as stream:
                self.assertEqual(stream.read().strip(), tokens[0])


if __name__ == "__main__":
    unittest.main()
