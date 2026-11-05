"""Detect the host OS and import only the selected desktop backend."""
import platform

from backend_common import BackendError


PLATFORMS = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}
BACKENDS = ("auto", "macos", "windows", "linux", "mock")


def resolve_backend(requested="auto", system=None):
    system = platform.system() if system is None else system
    if requested not in BACKENDS:
        raise BackendError("invalid_backend", f"Unknown backend: {requested}")
    if requested == "mock":
        return "mock"
    native = PLATFORMS.get(system)
    if native is None:
        raise BackendError("unsupported_platform", f"No native desktop backend for OS {system!r}",
                           {"supported": list(PLATFORMS), "mock_available": True})
    if requested != "auto" and requested != native:
        raise BackendError("backend_os_mismatch", f"Backend {requested!r} cannot run on {system}",
                           {"detected_os": system, "expected_backend": native})
    return native


def create_backend(requested="auto"):
    name = resolve_backend(requested)
    if name == "mock":
        from mock_backend import MockBackend
        backend = MockBackend()
    elif name == "macos":
        from macos_backend import MacOSBackend
        backend = MacOSBackend()
    else:
        from process_backend import ProcessBackend
        backend = ProcessBackend(name)
    backend.backend_name = name
    return backend
