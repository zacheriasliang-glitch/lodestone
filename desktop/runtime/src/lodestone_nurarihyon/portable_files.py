"""Local file custody with native POSIX and Windows implementations.

Windows uses real kernel handles: every ancestor is opened without reparse
traversal and held without delete sharing. Regular reads deny write/delete
sharing. Publication flushes a sibling temporary and uses a
same-volume MoveFileExW(MOVEFILE_WRITE_THROUGH), without a copy fallback.
There is deliberately no fake Windows directory fsync: callers must publish
through durable_write or explicitly handle UnsupportedDurability.

API references (Microsoft Learn):
 /windows/win32/api/fileapi/nf-fileapi-createfilew
 /windows/win32/api/winbase/nf-winbase-movefileexw
 /windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
Python os.fsync uses _commit on Windows (FlushFileBuffers).
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading
import uuid


WINDOWS = os.name == "nt"
_DIRECTORIES = threading.local()


class UnsupportedDurability(OSError):
    """The requested operation has no implemented native durability contract."""


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def is_alias(info):
    # Junctions, mount points and other reparse tags are aliases too.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def absolute_path(path):
    supplied = Path(path)
    if ".." in supplied.parts:
        raise ValueError("Parent traversal is not admitted")
    path = Path(os.path.abspath(supplied))
    if WINDOWS:
        # Local drive paths only. UNC/device namespaces and ADS need different
        # custody guarantees; silently treating them as local files is unsafe.
        if len(path.drive) != 2 or path.drive[1] != ":" or not path.root:
            raise ValueError("A local absolute drive path is required")
        reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(n) for n in range(1, 10)),
                    *("LPT" + str(n) for n in range(1, 10))}
        for part in path.parts[1:]:
            if (":" in part or part.endswith((".", " ")) or
                    part.split(".")[0].upper() in reserved):
                raise ValueError("Windows device, stream or ambiguous path is not admitted")
    return path


if WINDOWS:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID = ctypes.c_void_p(-1).value

    class _Information(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Information)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel.GetFileType.restype = wintypes.DWORD
    _kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _kernel.MoveFileExW.restype = wintypes.BOOL

    def _win_path(path):
        return "\\\\?\\" + str(path)

    def _win_error(path):
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = str(path)
        return error

    def _win_open(path, *, directory=False, flags=os.O_RDONLY, lock=False):
        if directory:
            # Attribute-only access is exempt from share-access accounting.
            # LIST_DIRECTORY | TRAVERSE | READ_ATTRIBUTES pins the directory
            # against rename; child writes remain possible with SHARE_READ|WRITE.
            access, sharing, disposition = 0xA1, 3, 3
        else:
            access = 0x80000000 if flags & os.O_RDWR == 0 and flags & os.O_WRONLY == 0 else 0xC0000000
            sharing = 3 if lock else 1  # lock holders must allow other lock handles
            disposition = 1 if flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
        attributes = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT/BACKUP_SEMANTICS
        handle = _kernel.CreateFileW(_win_path(path), access, sharing, None,
                                     disposition, attributes, None)
        if handle == _INVALID:
            raise _win_error(path)
        try:
            info = _Information()
            if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise _win_error(path)
            if info.attributes & 0x400:
                raise ValueError("Symlink or reparse point is not admitted")
            if bool(info.attributes & 0x10) != directory or _kernel.GetFileType(handle) != 1:
                raise ValueError("Expected a regular " + ("directory" if directory else "file"))
            return handle
        except BaseException:
            _kernel.CloseHandle(handle)
            raise

    def _win_move(source, target, *, replace):
        if not _kernel.MoveFileExW(_win_path(source), _win_path(target), 8 | (1 if replace else 0)):
            raise _win_error(target)


@contextmanager
def _directory(path, *, create=False, cache=False, reuse=True):
    """Pin components; explicit bounded guarded_directory scopes may be reused.

    Lock contexts are independent: ExitStack leases can outlive the context
    they were opened within. Only lexical guarded_directory scopes lend their
    descriptors, and their outer owner still checks and closes every handle.
    """
    path = absolute_path(path)
    held = getattr(_DIRECTORIES, "held", None)
    if held is None:
        held = _DIRECTORIES.held = []
    # Compare complete lexical components without constructing every ancestor
    # Path on each read. Windows component comparison remains case-insensitive.
    path_parts = path.parts
    comparison = tuple(os.path.normcase(part) for part in path_parts) if WINDOWS else path_parts
    parent = None
    if reuse:
        for entry in held:
            if (comparison[:len(entry[2])] == entry[2] and
                    (parent is None or len(entry[2]) > len(parent[2]))):
                parent = entry
    opened, relations = [], []
    registered = None
    try:
        current = parent[0] if parent else Path(path.anchor)
        parts = path_parts[len(parent[2]) if parent else 1:]
        if WINDOWS:
            if parent is None:
                opened.append(_win_open(current, directory=True))
            for part in parts:
                current = current / part
                if create:
                    try:
                        current.mkdir()
                    except FileExistsError:
                        pass
                opened.append(_win_open(current, directory=True))
            descriptor = None
        else:
            if parent is not None:
                descriptor = parent[1]
            else:
                descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                opened.append(descriptor)
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except OSError as exc:
                    # A directory symlink may report ENOTDIR rather than ELOOP.
                    info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                    if is_alias(info):
                        raise ValueError("Symlink directory is not admitted") from exc
                    raise
                opened.append(child)
                relations.append((descriptor, part, os.fstat(child)))
                descriptor = child
        result = (path, descriptor)
        if cache:
            registered = (path, descriptor, comparison)
            held.append(registered)
        yield result
        if WINDOWS:
            for handle in opened:
                info = _Information()
                if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                    raise _win_error(path)
                if info.attributes & 0x400 or not info.attributes & 0x10:
                    raise ValueError("Guarded directory became a reparse point")
        for ancestor, part, before in relations:
            after = os.stat(part, dir_fd=ancestor, follow_symlinks=False)
            if is_alias(after) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("Directory identity changed during operation")
    finally:
        if registered is not None:
            assert held[-1] is registered
            held.pop()
        for descriptor in reversed(opened):
            if WINDOWS:
                _kernel.CloseHandle(descriptor)
            else:
                os.close(descriptor)


@contextmanager
def guarded_directory(path):
    """Reject aliases and retain ancestor custody throughout a bounded operation."""
    with _directory(path, cache=True) as (path, _):
        yield path


def ensure_directory(path):
    """Create directories through pinned ancestors without following aliases."""
    with _directory(path, create=True) as (path, _):
        return path


def _open_in(directory, name, flags, mode=0o600, *, lock=False):
    path, descriptor = directory
    if WINDOWS:
        handle = _win_open(path / name, flags=flags, lock=lock)
        try:
            return msvcrt.open_osfhandle(handle, (flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)) | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
    return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=descriptor)


def _stat_in(directory, name):
    path, descriptor = directory
    return (path / name).lstat() if WINDOWS else os.stat(name, dir_fd=descriptor, follow_symlinks=False)


def _same_file(directory, name, fd, *, unchanged):
    opened = os.fstat(fd)
    named = _stat_in(directory, name)
    if is_alias(named) or not stat.S_ISREG(named.st_mode):
        raise ValueError("Expected a regular, non-symlink file")
    if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or
            unchanged is not None and (identity(opened) != unchanged or identity(named) != unchanged)):
        raise ValueError("File identity changed during operation")


@contextmanager
def open_regular(path, flags=os.O_RDONLY, mode=0o600, *, lock=False):
    """Yield an owned fd; detect replacement and read-time content changes.

    lock=True permits write sharing solely for an OS-held interprocess lock.
    This API never truncates existing paths. Use durable_write for publication.
    """
    if flags & (os.O_TRUNC | os.O_APPEND):
        raise ValueError("Destructive open is not admitted; use durable_write")
    path = absolute_path(path)
    with _directory(path.parent, reuse=not lock) as directory:
        try:
            before = _stat_in(directory, path.name)
        except FileNotFoundError:
            before = None
        if before is not None and (is_alias(before) or not stat.S_ISREG(before.st_mode)):
            raise ValueError("Expected a regular, non-symlink file")
        fd = _open_in(directory, path.name, flags, mode, lock=lock)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Expected a regular file")
            if before is not None and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("File identity changed while opening")
            unchanged = identity(info) if flags & (os.O_WRONLY | os.O_RDWR) == 0 else None
            _same_file(directory, path.name, fd, unchanged=unchanged)
            yield fd
            _same_file(directory, path.name, fd, unchanged=unchanged)
        finally:
            os.close(fd)


def read_bytes(path, limit=262144):
    with open_regular(path) as fd:
        blocks, remaining = [], limit + 1
        while remaining:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        data = b"".join(blocks)
    if len(data) > limit:
        raise ValueError("File exceeds byte bound")
    return data


def sync_directory(path):
    if WINDOWS:
        raise UnsupportedDurability("Windows directory fsync is unsupported; use durable_write publication")
    with _directory(path) as (_, fd):
        os.fsync(fd)


def durable_write(path, body, *, replace=False, mode=0o600):
    """Publish complete flushed bytes, atomically; preserve existing by default."""
    if type(body) is not bytes:
        raise TypeError("File body must be bytes")
    path = absolute_path(path)
    with _directory(path.parent) as directory:
        try:
            target_info = _stat_in(directory, path.name)
        except FileNotFoundError:
            target_info = None
        if target_info is not None:
            if is_alias(target_info) or not stat.S_ISREG(target_info.st_mode):
                raise ValueError("Publication target is not a regular, non-symlink file")
            if not replace:
                raise FileExistsError(str(path))
        temporary = ".pending-" + uuid.uuid4().hex
        fd = _open_in(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            try:
                view = memoryview(body)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("File write made no progress")
                    view = view[written:]
                os.fsync(fd)
                _same_file(directory, temporary, fd, unchanged=None)
                before = os.fstat(fd)
                published_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            finally:
                os.close(fd)
            if WINDOWS:
                _win_move(path.parent / temporary, path, replace=replace)
            elif replace:
                os.replace(temporary, path.name, src_dir_fd=directory[1], dst_dir_fd=directory[1])
                os.fsync(directory[1])
            else:
                os.link(temporary, path.name, src_dir_fd=directory[1], dst_dir_fd=directory[1], follow_symlinks=False)
                os.fsync(directory[1])
            after = _stat_in(directory, path.name)
            # Windows finalizes last-write timestamps on close. Use the stable
            # object identity and exact bytes there, never a pre-close timestamp.
            observed = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if is_alias(after) or (published_identity[:3] != observed[:3] if WINDOWS else
                                   published_identity != observed):
                raise ValueError("Published file identity changed")
            if WINDOWS and read_bytes(path, len(body)) != body:
                raise ValueError("Published file bytes changed")
        finally:
            try:
                if WINDOWS:
                    (path.parent / temporary).unlink()
                else:
                    os.unlink(temporary, dir_fd=directory[1])
                    os.fsync(directory[1])
            except FileNotFoundError:
                pass
