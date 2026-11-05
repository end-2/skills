"""Native POSIX file/locking checks and mocked Windows routing/locking checks.

Mocked Windows tests do not establish native Windows ACL/API compatibility.
"""
import errno
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import platform_support as support


class StatePathTests(unittest.TestCase):
    def test_macos_preserves_existing_state_directory(self):
        home = Path.cwd() / "fixture-home"
        with patch.object(support.sys, "platform", "darwin"), patch.object(support.Path, "home", return_value=home):
            self.assertEqual(support.default_token_path(), home / ".local/state/computer-use-server/token")

    def test_linux_uses_absolute_xdg_state_home_or_existing_default(self):
        home, custom = Path.cwd() / "fixture-home", Path.cwd() / "custom-state"
        with patch.object(support.sys, "platform", "linux"), patch.object(support.Path, "home", return_value=home):
            for value, expected in (
                ("", home / ".local/state/computer-use-server"),
                (str(custom), custom / "computer-use-server"),
                ("relative/state", home / ".local/state/computer-use-server"),
            ):
                with self.subTest(value=value), patch.dict(os.environ, {"XDG_STATE_HOME": value}):
                    self.assertEqual(support.default_state_dir(), Path(expected))

    def test_windows_uses_local_appdata_and_home_fallback(self):
        home, local = Path.cwd() / "fixture-home", Path.cwd() / "fixture-local"
        with patch.object(support.sys, "platform", "win32"), patch.object(support.Path, "home", return_value=home):
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertEqual(support.default_state_dir(), local / "computer-use-server")
            with patch.dict(os.environ, {"LOCALAPPDATA": ""}):
                self.assertEqual(support.default_state_dir(), home / "AppData/Local/computer-use-server")


@unittest.skipIf(sys.platform == "win32", "Native POSIX permissions and flock tests")
class PosixPrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private"

    def test_creation_is_private_and_reopen_does_not_truncate(self):
        with support.private_file(self.path, create=True) as stream:
            stream.write("secret")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with support.private_file(self.path, create=True) as stream:
            self.assertEqual(stream.read(), "secret")
        with support.private_file(self.path, "a+") as stream:
            stream.write("-suffix")
        with support.private_file(self.path, "r") as stream:
            self.assertEqual(stream.read(), "secret-suffix")

    def test_permissive_file_is_rejected_without_changing_or_truncating_it(self):
        self.path.write_text("existing")
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            with support.private_file(self.path, create=True):
                self.fail("Opened a world-readable file")
        self.assertEqual(self.path.read_text(), "existing")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_symlink_and_nonregular_target_are_rejected(self):
        target = self.path.with_name("target")
        with support.private_file(target, create=True) as stream:
            stream.write("unchanged")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            with support.private_file(self.path, create=True):
                self.fail("Opened a symlink")
        self.assertEqual(target.read_text(), "unchanged")
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(ValueError):
            with support.private_file(self.path, "r"):
                self.fail("Opened a FIFO")

    def test_foreign_owner_is_rejected(self):
        with support.private_file(self.path, create=True):
            pass
        with patch.object(support.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                with support.private_file(self.path):
                    self.fail("Opened another user's private file")

    def test_process_lock_contention_and_release_preserve_file_and_cursor(self):
        child = """
import sys
sys.path.insert(0, sys.argv[1])
from platform_support import private_file, lock_file
try:
    with private_file(sys.argv[2]) as stream, lock_file(stream, blocking=False):
        pass
except BlockingIOError:
    sys.exit(17)
"""
        def try_lock():
            return subprocess.run(
                [sys.executable, "-c", child, str(SCRIPTS), str(self.path)],
                capture_output=True, text=True, timeout=5, check=False,
            )

        with support.private_file(self.path, create=True) as stream:
            stream.write("original")
            stream.flush()
            stream.seek(3)
            with support.lock_file(stream):
                self.assertEqual(stream.tell(), 3)
                blocked = try_lock()
                self.assertEqual(blocked.returncode, 17, blocked.stderr)
            self.assertEqual(stream.tell(), 3)
            available = try_lock()
            self.assertEqual(available.returncode, 0, available.stderr)
        self.assertEqual(self.path.read_text(), "original")

    def test_lock_releases_when_body_raises(self):
        with support.private_file(self.path, create=True) as first:
            with self.assertRaisesRegex(RuntimeError, "intentional"):
                with support.lock_file(first):
                    raise RuntimeError("intentional")
            with support.private_file(self.path) as second, support.lock_file(second, blocking=False):
                pass


class MockedWindowsTests(unittest.TestCase):
    def test_private_file_routes_to_native_acl_opener_without_posix_mode_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture"
            path.write_text("existing")
            path.chmod(0o644)
            fd = os.open(path, os.O_RDONLY)
            with patch.object(support, "_is_windows", return_value=True), \
                    patch.object(support, "_windows_open_private_file", return_value=fd) as native, \
                    patch.object(support, "_posix_open_private_file") as posix:
                with support.private_file(path, "r") as stream:
                    self.assertEqual(stream.read(), "existing")
                native.assert_called_once_with(path.absolute(), "r", False)
                posix.assert_not_called()

    def test_windows_lock_retries_contention_and_preserves_cursor_without_writes(self):
        stream = io.StringIO("original")
        stream.fileno = lambda: 42
        stream.seek(3)
        calls = []

        def locking(fd, operation, count):
            calls.append((fd, operation, count, stream.tell()))
            if len(calls) == 1:
                raise OSError(errno.EACCES, "locked")

        native = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with patch.object(support, "_is_windows", return_value=True), \
                patch.dict(sys.modules, {"msvcrt": native}), patch.object(support.time, "sleep") as sleep:
            with support.lock_file(stream):
                self.assertEqual(stream.tell(), 3)
                stream.seek(5)
            self.assertEqual(stream.tell(), 5)
            sleep.assert_called_once_with(0.05)
        self.assertEqual(calls, [(42, 2, 1, 0), (42, 2, 1, 0), (42, 0, 1, 0)])
        self.assertEqual(stream.getvalue(), "original")

    def test_windows_nonblocking_contention_raises_blocking_error_without_retry(self):
        stream = io.StringIO("")
        stream.fileno = lambda: 42
        native = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock(side_effect=OSError(errno.EACCES, "locked")))
        with patch.object(support, "_is_windows", return_value=True), \
                patch.dict(sys.modules, {"msvcrt": native}), patch.object(support.time, "sleep") as sleep:
            with self.assertRaises(BlockingIOError):
                with support.lock_file(stream, blocking=False):
                    self.fail("Acquired contended lock")
            sleep.assert_not_called()
        self.assertEqual(stream.tell(), 0)
        self.assertEqual(stream.getvalue(), "")
        native.locking.assert_called_once_with(42, 2, 1)

    def test_windows_invalid_descriptor_does_not_retry(self):
        stream = io.StringIO("")
        stream.fileno = lambda: 42
        native = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock(side_effect=OSError(errno.EBADF, "bad descriptor")))
        with patch.object(support, "_is_windows", return_value=True), \
                patch.dict(sys.modules, {"msvcrt": native}), patch.object(support.time, "sleep") as sleep:
            with self.assertRaises(OSError) as caught:
                with support.lock_file(stream):
                    self.fail("Acquired invalid descriptor")
            self.assertEqual(caught.exception.errno, errno.EBADF)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
