"""Read large QQ images with their prefix-adjusted SQLite lock-page layout.

The encrypted QQ file has a 1024-byte prefix outside its SQLite pages. Its
reserved lock page is therefore one page earlier than stock SQLite's at 1 GiB.
Changing SQLite's pending byte is process-global, so these reads run in an owned
child. The desktop's SQLite library and other databases keep the default layout.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time


_PENDING_BYTE = 0x40000000
_QQ_PENDING_BYTE = _PENDING_BYTE - 1024
_READ_TIMEOUT = 300


def needs_qq_layout(path: Path) -> bool:
    """Recognize the supported decrypted QQ codec shape, without reading rows."""
    try:
        with path.open("rb") as stream:
            header = stream.read(100)
            size = os.fstat(stream.fileno()).st_size
    except OSError:
        return False  # The normal read reports the actual access failure.
    return (
        size > _QQ_PENDING_BYTE
        and len(header) == 100
        and header[:16] == b"SQLite format 3\0"
        and header[16:18] == b"\x10\x00"
        and header[20] in (48, 80)
    )


def _pending_byte_control():
    if os.name != "nt" or sqlite3.sqlite_version not in ("3.50.4", "3.54.1"):
        raise sqlite3.DatabaseError("Unsupported QQ SQLite layout runtime.")
    # sqlite3 has already loaded this module. Do not search for a replacement
    # DLL on PATH, or accidentally adjust a different SQLite library instance.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint
    handle = kernel.GetModuleHandleW("sqlite3.dll")
    name = ctypes.create_unicode_buffer(32768)
    length = kernel.GetModuleFileNameW(handle, name, len(name)) if handle else 0
    if not 0 < length < len(name):
        raise sqlite3.DatabaseError("QQ SQLite library unavailable.")
    library = ctypes.CDLL(name.value)
    library.sqlite3_libversion.restype = ctypes.c_char_p
    if library.sqlite3_libversion().decode("ascii") != sqlite3.sqlite_version:
        raise sqlite3.DatabaseError("QQ SQLite library mismatch.")
    control = library.sqlite3_test_control
    control.argtypes = [ctypes.c_int, ctypes.c_uint]
    control.restype = ctypes.c_int
    if control(11, 0) != _PENDING_BYTE:
        raise sqlite3.DatabaseError("Unexpected QQ SQLite lock layout.")
    return control


def _read_worker(channel, path: str, query: str, parameters: list) -> None:
    parent = multiprocessing.parent_process()
    if parent is not None:
        def exit_with_parent():
            parent.join()
            os._exit(1)  # Release read handles even after an abrupt desktop exit.
        threading.Thread(target=exit_with_parent, daemon=True).start()
    connection = None
    control = None
    try:
        control = _pending_byte_control()
        control(11, _QQ_PENDING_BYTE)
        if control(11, 0) != _QQ_PENDING_BYTE:
            raise sqlite3.DatabaseError("QQ SQLite layout unavailable.")
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        cursor = connection.execute(query, parameters)
        names = [column[0] for column in cursor.description or ()]
        rows = cursor.fetchall()
        channel.send(("ok", names, rows))
    except Exception as error:
        # Raw SQLite messages can contain private SQL identifiers or values.
        channel.send(("error", getattr(error, "sqlite_errorcode", 0), None))
    finally:
        if connection is not None:
            connection.close()
        if control is not None:
            control(11, _PENDING_BYTE)
        channel.close()


def read_rows(
    path: Path, query: str, parameters: list | None = None, *,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[list[str], list[tuple]]:
    """Run a read-only query in one bounded, private child and always reap it."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_read_worker, args=(sender, str(path), query, parameters or []), daemon=True)
    started = False
    try:
        if cancelled():
            raise sqlite3.DatabaseError("QQ SQLite read cancelled.")
        process.start()
        started = True
        sender.close()
        deadline = time.monotonic() + _READ_TIMEOUT
        while not receiver.poll(0.1):
            if cancelled():
                raise sqlite3.DatabaseError("QQ SQLite read cancelled.")
            if time.monotonic() >= deadline:
                raise sqlite3.DatabaseError("QQ SQLite read timed out.")
        try:
            status, names, rows = receiver.recv()
        except (EOFError, OSError):
            raise sqlite3.DatabaseError("QQ SQLite reader unavailable.") from None
        process.join(5)
        if process.is_alive() or process.exitcode != 0:
            raise sqlite3.DatabaseError("QQ SQLite reader failed.")
        if status != "ok":
            error = sqlite3.DatabaseError("QQ SQLite read failed.")
            error.sqlite_errorcode = names if type(names) is int else 0
            raise error
        return names, rows
    finally:
        receiver.close()
        sender.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(5)
            process.close()
