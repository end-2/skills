#!/usr/bin/env python3
"""Single-thread native adapter host. stdout is exclusively UTF-8 JSONL."""
import importlib
import json
import sys

from backend_common import BackendError


def main():
    # Use binary stdout; Windows console encodings must not corrupt UI text.
    output = sys.stdout.buffer
    # Third-party packages may print diagnostics; keep them off the JSON pipe.
    sys.stdout = sys.stderr

    def emit(value):
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
        output.flush()

    def error(exc):
        emit({"ok": False, "error": {"code": getattr(exc, "code", "backend_unavailable"),
               "message": str(exc), "details": getattr(exc, "details", None) or {}}})

    backend = None
    try:
        classes = {"windows": ("windows_backend", "WindowsBackend"), "linux": ("linux_backend", "LinuxBackend")}
        module_name, class_name = classes[sys.argv[1]]
        backend = getattr(importlib.import_module(module_name), class_name)()
        emit({"ok": True, "result": {"ready": True}})
        observed = False
        while True:
            line = sys.stdin.buffer.readline(131073)
            if not line:
                break
            if len(line) > 131072 or not line.endswith(b"\n"):
                error(BackendError("invalid_action", "Native request exceeds 128 KiB"))
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise BackendError("invalid_action", "Expected request object")
                op = request.get("op")
                if op == "snapshot":
                    result = backend.snapshot()
                    observed = True
                elif op == "act" and observed:
                    observed = False
                    result = backend.act(request.get("action"))
                else:
                    raise BackendError("element_unavailable", "Read a fresh snapshot before acting")
                emit({"ok": True, "result": result})
            except Exception as exc:
                observed = False
                error(exc)
    except Exception as exc:
        error(exc)
        return 1
    finally:
        if backend is not None:
            backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
