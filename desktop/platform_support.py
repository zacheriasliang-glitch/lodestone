"""Small stdlib-only host boundary used before importing a verified runtime.

Locks are kernel leases on every host. Windows uses LockFileEx, denies deletion
of open lock files, pins directory ancestors, and commits replacements with
MoveFileExW(WRITE_THROUGH). POSIX commits fsync both file and parent directory.
"""
from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import uuid


def host_platform():
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ValueError("Unsupported desktop operating system: " + sys.platform)


def default_locations(platform_name=None, *, home=None, environ=None):
    platform_name = platform_name or host_platform()
    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    if platform_name == "macos":
        return home / "Library/Application Support/Lodestone Preview", home / "Applications/Lodestone Preview.app"
    if platform_name == "windows":
        local = Path(environ.get("LOCALAPPDATA") or home / "AppData/Local")
        roaming = Path(environ.get("APPDATA") or home / "AppData/Roaming")
        if not local.is_absolute() or not roaming.is_absolute():
            raise ValueError("Windows application data locations must be absolute")
        return local / "Lodestone Preview", roaming / "Microsoft/Windows/Start Menu/Programs/Lodestone Preview.cmd"
    if platform_name == "linux":
        data = Path(environ.get("XDG_DATA_HOME") or home / ".local/share")
        # The XDG specification says to ignore a relative environment value.
        if not data.is_absolute():
            data = home / ".local/share"
        return data / "lodestone-preview", data / "applications/lodestone-preview.desktop"
    raise ValueError("Unsupported desktop platform: " + str(platform_name))


def venv_python(path):
    return Path(path) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def check_path(path):
    """Reject symlinks, junctions, device paths and alternate data streams."""
    path = Path(path).absolute()
    if os.name == "nt":
        if str(path).startswith(("\\\\?\\", "\\\\.\\")):
            raise ValueError("Device namespace paths are unsupported")
        reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                    *("LPT" + str(i) for i in range(1, 10))}
        for part in path.parts[1:]:
            if ":" in part or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved:
                raise ValueError("Unsupported Windows managed path: " + str(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise ValueError("Managed path is a symbolic link or reparse point: " + str(item))
    return path


if os.name == "nt":
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    class _FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL
    _kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _kernel.MoveFileExW.restype = wintypes.BOOL

    def _open_windows(path, *, directory=False, read_only=False):
        # LIST_DIRECTORY participates in Windows share accounting; attribute-only
        # access does not. Omit DELETE sharing to pin names, while allowing child
        # creation/writes. Reparse attributes are checked on the native handle.
        handle = _kernel.CreateFileW(str(path), 0xA1 if directory else (0x80000000 if read_only else 0xC0000000),
                    1 if read_only else 3, None, 3 if directory or read_only else 4,
                    0x02200000 if directory else 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileInfo()
            if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
                raise ValueError("Managed path has unexpected type or is a reparse point: " + str(path))
            return handle
        except BaseException:
            _kernel.CloseHandle(handle)
            raise


@contextmanager
def guarded_directory(path, *, create=False):
    """Pin Windows ancestors before writes; refuse redirecting managed paths."""
    path = check_path(path)
    handles = []
    try:
        for item in reversed((path, *path.parents)):
            if create:
                item.mkdir(exist_ok=True, mode=0o700)
            if os.name == "nt":
                handles.append(_open_windows(item, directory=True))
            elif not item.is_dir():
                raise ValueError("Managed parent is not a directory: " + str(item))
        check_path(path)
        yield path
        if os.name == "nt":
            for handle in handles:
                info = _FileInfo()
                if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info.attributes & 0x400 or not info.attributes & 0x10:
                    raise ValueError("Managed directory became a reparse point")
        check_path(path)
    finally:
        for handle in reversed(handles):
            _kernel.CloseHandle(handle)


@contextmanager
def file_lock(path, *, shared=False):
    """Nonblocking kernel lock. Raise BlockingIOError only for lock contention."""
    path = check_path(path)
    with guarded_directory(path.parent, create=True):
        if os.name == "nt":
            handle = _open_windows(path)
            overlapped = _Overlapped()
            acquired = False
            try:
                if not _kernel.LockFileEx(handle, 1 | (0 if shared else 2), 0, 1, 0, ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    if error in (32, 33, 158):
                        raise BlockingIOError(errno.EAGAIN, "File lock is held", str(path))
                    raise ctypes.WinError(error)
                acquired = True
                yield handle
            finally:
                if acquired:
                    _kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
                _kernel.CloseHandle(handle)
        else:
            import fcntl
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                opened = os.fstat(fd)
                current = path.lstat()
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("Managed lock is not the expected regular file")
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                yield fd
            finally:
                os.close(fd)


def _sync_parent(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def open_regular(path):
    """Stream a non-aliased file with native custody and identity checks.

    Windows denies write/delete sharing for the entire read. POSIX checks the
    descriptor and name before/after streaming; O_NONBLOCK prevents FIFOs from
    blocking before their type can be rejected. No file contents are cached.
    """
    path = check_path(path)
    with guarded_directory(path.parent):
        if os.name == "nt":
            import msvcrt
            handle = _open_windows(path, read_only=True)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _kernel.CloseHandle(handle)
                raise
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            def identity(info):
                return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
            if not stat.S_ISREG(before.st_mode) or _is_link(before):
                raise ValueError("Managed path is not a regular file: " + str(path))
            named_before = path.lstat()
            # CPython on Windows gives fstat ctime the ChangeTime meaning, but
            # path stat ctime still means creation time (python/cpython#157671).
            # Compare common fields across APIs and retain both full baselines.
            comparable = 4 if os.name == "nt" else 5
            if identity(before)[:comparable] != identity(named_before)[:comparable]:
                raise ValueError("Managed file identity changed while opening: " + str(path))
            yield stream
            check_path(path)
            if (identity(before) != identity(os.fstat(stream.fileno())) or
                    identity(named_before) != identity(path.lstat())):
                raise ValueError("Managed file identity changed while reading: " + str(path))


def read_file(path, maximum=None):
    with open_regular(path) as stream:
        return stream.read() if maximum is None else stream.read(maximum + 1)


def atomic_write(path, data, mode=0o600, *, replace=True):
    path = check_path(path)
    with guarded_directory(path.parent, create=True):
        if path.exists() and not path.is_file():
            raise ValueError("Managed file is not a regular file: " + str(path))
        fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, mode)
            check_path(path)
            if os.name == "nt":
                if not _kernel.MoveFileExW(tmp, str(path), (0x1 if replace else 0) | 0x8):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                if replace:
                    os.replace(tmp, path)
                else:
                    os.link(tmp, path, follow_symlinks=False)
                _sync_parent(path.parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def remove_file(path):
    path = check_path(path)
    if not path.parent.exists():
        return
    with guarded_directory(path.parent):
        if path.exists() and not path.is_file():
            raise ValueError("Managed file is not a regular file: " + str(path))
        if os.name == "nt" and path.exists():
            # A write-through rename durably removes the original name before
            # best-effort cleanup; interrupted cleanup leaves only a tombstone.
            tombstone = path.with_name("." + path.name + ".removed-" + uuid.uuid4().hex)
            if not _kernel.MoveFileExW(str(path), str(tombstone), 0x8):
                raise ctypes.WinError(ctypes.get_last_error())
            tombstone.unlink()
        else:
            path.unlink(missing_ok=True)
            if os.name != "nt":
                _sync_parent(path.parent)


def durable_move(source, destination):
    """Publish a staged file/directory exclusively and flush the rename."""
    source, destination = check_path(source), check_path(destination)
    with guarded_directory(source.parent), guarded_directory(destination.parent):
        if os.name == "nt":
            if not _kernel.MoveFileExW(str(source), str(destination), 0x8):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            # Both supported POSIX hosts have an exclusive rename primitive.
            # A check followed by os.rename could replace a racing empty directory.
            libc = ctypes.CDLL(None, use_errno=True)
            if sys.platform == "darwin":
                rename = libc.renamex_np
                rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
                rename.restype = ctypes.c_int
                result = rename(os.fsencode(source), os.fsencode(destination), 0x4)
            elif sys.platform.startswith("linux"):
                rename = libc.renameat2
                rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                rename.restype = ctypes.c_int
                result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
            else:
                raise OSError("Exclusive durable rename unavailable on this host")
            if result:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))
            _sync_parent(destination.parent)
            if source.parent != destination.parent:
                _sync_parent(source.parent)
