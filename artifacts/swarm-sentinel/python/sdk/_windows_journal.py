"""Native private journal I/O on local NTFS. Imported only on Windows.

Keep handles non-inheritable and validate existing files before reading them.
chmod(0600), os.replace(), and directory fsync are not Windows equivalents of
the POSIX journal's security and durability primitives.
"""
import ctypes
from ctypes import wintypes as w
from contextlib import contextmanager
import msvcrt
import os
from pathlib import Path
from uuid import uuid4


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class FileInformation(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME),
                ("accessed", w.FILETIME), ("written", w.FILETIME),
                ("volume", w.DWORD), ("size_high", w.DWORD), ("size_low", w.DWORD),
                ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD)]


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments
    return function


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
security = ctypes.WinDLL("advapi32", use_last_error=True)
P = w.LPVOID
PP = ctypes.POINTER(P)
close_handle = _bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)
local_free = _bind(kernel, "LocalFree", P, P)
create_file = _bind(kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                    ctypes.POINTER(SecurityAttributes), w.DWORD, w.DWORD, w.HANDLE)
file_information = _bind(kernel, "GetFileInformationByHandle", w.BOOL, w.HANDLE,
                         ctypes.POINTER(FileInformation))
file_attributes = _bind(kernel, "GetFileAttributesW", w.DWORD, w.LPCWSTR)
volume_path = _bind(kernel, "GetVolumePathNameW", w.BOOL, w.LPCWSTR, w.LPWSTR, w.DWORD)
drive_type = _bind(kernel, "GetDriveTypeW", w.UINT, w.LPCWSTR)
volume_information = _bind(kernel, "GetVolumeInformationW", w.BOOL, w.LPCWSTR,
                           w.LPWSTR, w.DWORD, P, P, P, w.LPWSTR, w.DWORD)
move_file = _bind(kernel, "MoveFileExW", w.BOOL, w.LPCWSTR, w.LPCWSTR, w.DWORD)
flush_buffers = _bind(kernel, "FlushFileBuffers", w.BOOL, w.HANDLE)
current_process = _bind(kernel, "GetCurrentProcess", w.HANDLE)
open_token = _bind(security, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD,
                   ctypes.POINTER(w.HANDLE))
token_information = _bind(security, "GetTokenInformation", w.BOOL, w.HANDLE,
                          w.DWORD, P, w.DWORD, ctypes.POINTER(w.DWORD))
sid_string = _bind(security, "ConvertSidToStringSidW", w.BOOL, P,
                   ctypes.POINTER(w.LPWSTR))
parse_descriptor = _bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                         w.BOOL, w.LPCWSTR, w.DWORD, PP, P)
descriptor_string = _bind(security, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                          w.BOOL, P, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), P)
get_security = _bind(security, "GetSecurityInfo", w.DWORD, w.HANDLE, w.DWORD,
                     w.DWORD, PP, PP, PP, PP, PP)


def _check(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def _user_sid():
    token = w.HANDLE()
    _check(open_token(current_process(), 0x0008, ctypes.byref(token)))  # TOKEN_QUERY
    try:
        size = w.DWORD()
        token_information(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        _check(token_information(token, 1, buffer, size, ctypes.byref(size)))
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; its first field is a pointer.
        sid = ctypes.cast(buffer, PP)[0]
        text = w.LPWSTR()
        _check(sid_string(sid, ctypes.byref(text)))
        try:
            return text.value
        finally:
            local_free(ctypes.cast(text, P))
    finally:
        close_handle(token)


def _descriptor_text(descriptor):
    text = w.LPWSTR()
    _check(descriptor_string(descriptor, 1, 0x5, ctypes.byref(text), None))
    try:
        return text.value
    finally:
        local_free(ctypes.cast(text, P))


class WindowsJournalIO:
    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        # Reject streams, device paths, and ambiguous Win32 names.
        if (str(self.path).startswith("\\\\")
                or any(":" in part or part.endswith((" ", "."))
                       for part in self.path.parts[1:])):
            raise OSError("Journal requires an ordinary local NTFS path")
        parent = self.path.parent
        for directory in (parent, *parent.parents):
            attributes = file_attributes(str(directory))
            if attributes == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                raise OSError("Journal directories must not be reparse points")
        volume = ctypes.create_unicode_buffer(32768)
        _check(volume_path(str(parent), volume, len(volume)))
        filesystem = ctypes.create_unicode_buffer(32)
        _check(volume_information(volume.value, None, 0, None, None, None,
                                  filesystem, len(filesystem)))
        if drive_type(volume.value) != 3 or filesystem.value != "NTFS":
            raise OSError("Execution journals require a fixed local NTFS volume on Windows")
        sid = _user_sid()
        self.sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
        # Normalize aliases (e.g. SYSTEM) just as GetSecurityInfo does.
        with self._descriptor() as descriptor:
            self.expected_security = _descriptor_text(descriptor)

    @contextmanager
    def _descriptor(self):
        descriptor = P()
        _check(parse_descriptor(self.sddl, 1, ctypes.byref(descriptor), None))
        try:
            yield descriptor
        finally:
            local_free(descriptor)

    def _validate(self, handle):
        info = FileInformation()
        _check(file_information(handle, ctypes.byref(info)))
        if info.attributes & (0x400 | 0x10) or info.links != 1:
            raise OSError("Journal files must be regular files, not links or reparse points")
        descriptor = P()
        error = get_security(handle, 1, 0x5, None, None, None, None,
                             ctypes.byref(descriptor))  # SE_FILE_OBJECT, OWNER | DACL
        if error:
            raise ctypes.WinError(error)
        try:
            if _descriptor_text(descriptor) != self.expected_security:
                raise OSError("Journal requires a protected, current-user-only owner and DACL")
        finally:
            local_free(descriptor)

    def open_fd(self, path, *, create=False, exclusive=False, lock=False, read=False):
        with self._descriptor() as descriptor:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            access = 0x80000000 if read else 0xC0000000  # GENERIC_READ / READ | WRITE
            access |= 0x00020000  # READ_CONTROL for ACL validation
            disposition = 1 if exclusive else (4 if create else 3)  # CREATE_NEW/OPEN_ALWAYS/OPEN_EXISTING
            # A zero-share lock handle excludes other opens AND deletion/renaming.
            # Reparse-point opens are inspected, never followed.
            handle = create_file(str(Path(os.path.abspath(path))), access, 0 if lock else 1,
                                 ctypes.byref(attributes), disposition,
                                 0x00200000 | (0 if read else 0x80000000), None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._validate(handle)
            fd = msvcrt.open_osfhandle(handle, (os.O_RDONLY if read else os.O_RDWR)
                                      | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            close_handle(handle)
            raise
        return fd  # CRT descriptor now owns the handle.

    def temporary(self):
        path = self.path.with_name(f".{self.path.name}.{uuid4().hex}")
        return self.open_fd(path, exclusive=True), str(path)

    def flush(self, fd):
        _check(flush_buffers(msvcrt.get_osfhandle(fd)))

    def replace(self, temporary):
        # Same-directory, same-volume rename; never allow a copy/delete fallback.
        # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH.
        _check(move_file(str(temporary), str(self.path), 0x1 | 0x8))