"""State paths, private files, and process locks without third-party packages.

Windows files require an ACL-capable filesystem and a protected, current-user-only
DACL. Existing permissive ACLs are rejected, never silently rewritten. This does
not protect against administrators or compromised processes running as this user.
Only the final path component is protected against symlink/reparse traversal;
callers should use the per-user state directory or another trusted parent.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import sys
import time


def _is_windows():
    return sys.platform == "win32"


def default_state_dir():
    if _is_windows():
        base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local"
    elif sys.platform.startswith("linux") and os.environ.get("XDG_STATE_HOME"):
        candidate = Path(os.environ["XDG_STATE_HOME"])
        base = candidate if candidate.is_absolute() else Path.home() / ".local" / "state"
    else:
        base = Path.home() / ".local" / "state"
    return base / "computer-use-server"


def default_token_path():
    return default_state_dir() / "token"


def _posix_open_private_file(path, mode, create):
    flags = os.O_RDONLY if mode == "r" else os.O_RDWR
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    if mode == "a+":
        flags |= os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"Private file must be a regular file owned by this user with mode 600: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _windows_open_private_file(path, mode, create):
    # Native APIs supply the DACL at creation and validate the opened object, not
    # a path checked before opening. Do not substitute Windows chmod/stat bits.
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt

    kernel = C.WinDLL("kernel32", use_last_error=True)
    security = C.WinDLL("advapi32", use_last_error=True)
    pointer = C.c_void_p
    pointer_out = C.POINTER(pointer)

    def bind(library, name, result, *arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
        return function

    def checked(result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    close_handle = bind(kernel, "CloseHandle", W.BOOL, W.HANDLE)
    local_free = bind(kernel, "LocalFree", pointer, pointer)
    current_process = bind(kernel, "GetCurrentProcess", W.HANDLE)
    open_token = bind(security, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
    get_token = bind(security, "GetTokenInformation", W.BOOL, W.HANDLE, W.DWORD, pointer, W.DWORD, C.POINTER(W.DWORD))
    sid_string = bind(security, "ConvertSidToStringSidW", W.BOOL, pointer, pointer_out)
    parse_sd = bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", W.BOOL,
                    W.LPCWSTR, W.DWORD, pointer_out, C.POINTER(W.DWORD))
    get_security = bind(security, "GetSecurityInfo", W.DWORD, W.HANDLE, W.DWORD, W.DWORD,
                        pointer_out, pointer_out, pointer_out, pointer_out, pointer_out)
    get_control = bind(security, "GetSecurityDescriptorControl", W.BOOL, pointer, C.POINTER(W.WORD), C.POINTER(W.DWORD))
    equal_sid = bind(security, "EqualSid", W.BOOL, pointer, pointer)
    get_ace = bind(security, "GetAce", W.BOOL, pointer, W.DWORD, pointer_out)

    class SecurityAttributes(C.Structure):
        _fields_ = [("length", W.DWORD), ("descriptor", pointer), ("inherit", W.BOOL)]

    class FileAttributes(C.Structure):
        _fields_ = [("attributes", W.DWORD), ("tag", W.DWORD)]

    class ACL(C.Structure):
        _fields_ = [("revision", W.BYTE), ("reserved", W.BYTE), ("size", W.WORD),
                    ("count", W.WORD), ("reserved2", W.WORD)]

    class AllowedACE(C.Structure):
        _fields_ = [("type", W.BYTE), ("flags", W.BYTE), ("size", W.WORD),
                    ("mask", W.DWORD), ("sid_start", W.DWORD)]

    create_file = bind(kernel, "CreateFileW", W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD,
                       C.POINTER(SecurityAttributes), W.DWORD, W.DWORD, W.HANDLE)
    file_type = bind(kernel, "GetFileType", W.DWORD, W.HANDLE)
    file_info = bind(kernel, "GetFileInformationByHandleEx", W.BOOL, W.HANDLE, W.DWORD, pointer, W.DWORD)
    token, sd, descriptor, sid_text = W.HANDLE(), pointer(), pointer(), pointer()
    handle = None
    try:
        checked(open_token(current_process(), 0x0008, C.byref(token)))  # TOKEN_QUERY
        needed = W.DWORD()
        get_token(token, 1, None, 0, C.byref(needed))  # TokenUser
        if not needed.value:
            raise C.WinError(C.get_last_error())
        token_user = C.create_string_buffer(needed.value)
        checked(get_token(token, 1, token_user, needed.value, C.byref(needed)))
        user_sid = C.cast(token_user, C.POINTER(pointer)).contents
        checked(sid_string(user_sid, C.byref(sid_text)))
        user = C.wstring_at(sid_text)
        # Explicit owner + protected DACL + only this user's full-control ACE.
        checked(parse_sd(f"O:{user}D:P(A;;FA;;;{user})", 1, C.byref(sd), None))
        attributes = SecurityAttributes(C.sizeof(SecurityAttributes), sd, False)
        access = 0x80000000 | (0x40000000 if mode != "r" else 0)
        handle = create_file(str(path), access, 0x3, C.byref(attributes),
                             4 if create else 3, 0x00200000, None)
        if handle == pointer(-1).value:
            handle = None
            raise C.WinError(C.get_last_error())
        info = FileAttributes()
        checked(file_info(handle, 9, C.byref(info), C.sizeof(info)))
        if file_type(handle) != 1 or info.attributes & (0x10 | 0x400):
            raise ValueError(f"Private file must be regular and cannot be a reparse point: {path}")
        owner, dacl = pointer(), pointer()
        result = get_security(handle, 1, 0x1 | 0x4, C.byref(owner), None,
                              C.byref(dacl), None, C.byref(descriptor))
        if result:
            raise C.WinError(result)
        control, revision = W.WORD(), W.DWORD()
        checked(get_control(descriptor, C.byref(control), C.byref(revision)))
        if not owner or not equal_sid(owner, user_sid) or not dacl or not control.value & 0x1000:
            raise ValueError(f"Private file needs a protected current-user-only DACL: {path}")
        acl = C.cast(dacl, C.POINTER(ACL)).contents
        if acl.count != 1:
            raise ValueError(f"Private file DACL must grant access only to the current user: {path}")
        ace_pointer = pointer()
        checked(get_ace(dacl, 0, C.byref(ace_pointer)))
        ace = C.cast(ace_pointer, C.POINTER(AllowedACE)).contents
        ace_sid = pointer(ace_pointer.value + AllowedACE.sid_start.offset)
        if ace.type != 0 or ace.flags != 0 or ace.mask != 0x001F01FF or not equal_sid(ace_sid, user_sid):
            raise ValueError(f"Private file DACL must grant only the current user full control: {path}")
        # open_osfhandle accepts fewer flags than os.open. Handle access above
        # enforces read/write permissions; set binary translation separately.
        flags = os.O_NOINHERIT | (os.O_APPEND if mode == "a+" else 0)
        fd = msvcrt.open_osfhandle(handle, flags)
        handle = None  # The CRT file descriptor now owns this handle.
        try:
            msvcrt.setmode(fd, os.O_BINARY)
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally:
        if handle is not None:
            close_handle(handle)
        if token:
            close_handle(token)
        for allocated in (sd, descriptor, sid_text):
            if allocated:
                local_free(allocated)


@contextmanager
def private_file(path, mode="r+", create=False):
    """Open UTF-8 text securely; creation never truncates an existing file.

    Supported modes: r, r+, a+. create=True creates missing parents and file.
    The caller must use lock_file while reading/updating shared file content.
    """
    if mode not in {"r", "r+", "a+"}:
        raise ValueError("private_file supports only r, r+, and a+ modes")
    path = Path(path).expanduser().absolute()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    opener = _windows_open_private_file if _is_windows() else _posix_open_private_file
    fd = opener(path, mode, create)
    try:
        stream = os.fdopen(fd, mode, encoding="utf-8", newline="")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


@contextmanager
def lock_file(stream, blocking=True):
    """Hold an OS process lock until exit; never write/truncate a lock marker.

    Windows locks byte zero (including beyond EOF) and restores the cursor after
    lock/unlock. Nonblocking contention raises BlockingIOError on both systems.
    """
    if not _is_windows():
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield stream
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt

    def change_lock(operation):
        position = stream.tell()
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), operation, 1)
        finally:
            stream.seek(position)

    while True:
        try:
            change_lock(msvcrt.LK_NBLCK)
            break
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, "File is already locked") from error
            time.sleep(0.05)
    try:
        yield stream
    finally:
        change_lock(msvcrt.LK_UNLCK)
