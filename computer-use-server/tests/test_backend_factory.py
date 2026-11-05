import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from backend_common import BackendError
from backend_factory import create_backend, resolve_backend


class BackendFactoryTests(unittest.TestCase):
    def test_auto_selects_each_native_os(self):
        for system, expected in (("Darwin", "macos"), ("Windows", "windows"), ("Linux", "linux")):
            with self.subTest(system=system), patch("backend_factory.platform.system", return_value=system):
                self.assertEqual(resolve_backend(), expected)
                self.assertEqual(resolve_backend(expected), expected)

    def test_wrong_os_and_unknown_os_fail_without_mock_fallback(self):
        for requested, system, expected in (("windows", "Darwin", "backend_os_mismatch"),
                                            ("linux", "Windows", "backend_os_mismatch"),
                                            ("auto", "FreeBSD", "unsupported_platform"),
                                            ("typo", "Linux", "invalid_backend")):
            with self.subTest(requested=requested, system=system):
                with self.assertRaises(BackendError) as caught:
                    resolve_backend(requested, system)
                self.assertEqual(caught.exception.code, expected)
        self.assertEqual(resolve_backend("mock", "FreeBSD"), "mock")

    def test_native_workers_load_only_selected_backend(self):
        for system, name in (("Windows", "windows"), ("Linux", "linux")):
            with self.subTest(system=system), patch("backend_factory.platform.system", return_value=system), \
                    patch("process_backend.ProcessBackend") as constructor:
                backend = create_backend()
                constructor.assert_called_once_with(name)
                self.assertEqual(backend.backend_name, name)

    def test_mock_does_not_import_native_dependencies(self):
        env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
        result = subprocess.run([sys.executable, "-c", "import sys; from backend_factory import create_backend; "
                                 "b=create_backend('mock'); assert b.snapshot()['platform']=='mock'; "
                                 "assert not any(m in sys.modules for m in "
                                 "('macos_backend','windows_backend','linux_backend','uiautomation','pyatspi','fcntl'))"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
