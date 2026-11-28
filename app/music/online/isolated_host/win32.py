"""Win32 AppContainer launch and teardown primitives for the opt-in plugin host.

Based on the independently verified B23 launcher. Access is limited to the
private workspace created by this module and explicit AppContainer SIDs.
"""
from __future__ import annotations

import ctypes
import math
import os
import re
import secrets
import stat
import subprocess
import time
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

# ---- constants ----
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
DUPLICATE_SAME_ACCESS = 0x00000002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_PARENT_PROCESS = 0x00020000
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
TOKEN_QUERY = 0x0008
TOKEN_IS_APP_CONTAINER = 29
TOKEN_APP_CONTAINER_SID = 31
TOKEN_CAPABILITIES = 30
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS_HR = 0x800700B7
TOKEN_USER = 1
ERROR_ALREADY_EXISTS = 183
DRIVE_FIXED = 3
FS_PERSISTENT_ACLS = 0x00000008
_PROFILE_NAME = re.compile(r"SL25[0-9a-f]{32}\Z")
_APPCONTAINER_SID = re.compile(r"S-1-15-2-(?:[1-9][0-9]{0,9}|0)(?:-(?:[1-9][0-9]{0,9}|0)){6}\Z")
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p),
                ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


# ---- function signatures ----
userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                               ctypes.POINTER(SID_AND_ATTRIBUTES), wintypes.DWORD,
                                               ctypes.POINTER(ctypes.c_void_p)]
userenv.CreateAppContainerProfile.restype = ctypes.c_long
userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
userenv.DeleteAppContainerProfile.restype = ctypes.c_long
advapi32.FreeSid.argtypes = [ctypes.c_void_p]
advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD]
kernel32.CreatePipe.restype = wintypes.BOOL
kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
kernel32.SetHandleInformation.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.DuplicateHandle.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]
kernel32.DuplicateHandle.restype = wintypes.BOOL
kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsProcessInJob.restype = wintypes.BOOL
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
kernel32.CancelSynchronousIo.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                     wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                     ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                        ctypes.POINTER(ctypes.c_size_t)]
kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.IsTokenRestricted.argtypes = [wintypes.HANDLE]
advapi32.IsTokenRestricted.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
kernel32.GetWindowsDirectoryW.restype = wintypes.UINT
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryInformationJobObject.restype = wintypes.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)
]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel32.GetDriveTypeW.restype = wintypes.UINT
kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]
kernel32.GetVolumeInformationW.restype = wintypes.BOOL
kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES)]
kernel32.CreateDirectoryW.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL
userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
userenv.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
userenv.GetAppContainerFolderPath.restype = ctypes.c_long
ole32 = ctypes.WinDLL("ole32")
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None


def hr(code: int) -> str:
    return f"0x{code & 0xffffffff:08x}"


def sid_to_string(sid: ctypes.c_void_p) -> str | None:
    if not sid or not sid.value:
        return None
    out = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        return None
    try:
        return out.value
    finally:
        kernel32.LocalFree(out)


@dataclass
class Profile:
    name: str
    sid: ctypes.c_void_p
    sid_string: str | None
    create_hr: str
    already_existed: bool

    def close(self):
        if self.sid and self.sid.value:
            advapi32.FreeSid(self.sid)
            self.sid = ctypes.c_void_p()


def _profile_name(name: str) -> None:
    if not _PROFILE_NAME.fullmatch(name):
        raise ValueError("expected a generated B25 plugin profile name")


def ensure_profile(name: str, display: str = "Starlight plugin") -> Profile:
    _profile_name(name)
    psid = ctypes.c_void_p()
    rc = userenv.CreateAppContainerProfile(name, display, "Starlight managed plugin", None, 0, ctypes.byref(psid))
    # Never adopt an existing profile. It may carry different filesystem grants.
    return Profile(name, psid, sid_to_string(psid) if rc == 0 else None, hr(rc),
                   (rc & 0xffffffff) == ERROR_ALREADY_EXISTS_HR)


def delete_profile(name: str) -> str:
    _profile_name(name)
    return hr(userenv.DeleteAppContainerProfile(name))


def profile_sid(name):
    _profile_name(name)
    sid = ctypes.c_void_p()
    try:
        code = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        value = sid_to_string(sid) if code == 0 else None
        if not value:
            raise OSError(f"could not derive profile SID: {hr(code)}")
        return value
    finally:
        if sid:
            advapi32.FreeSid(sid)


def profile_exists(sid):
    folder = ctypes.c_void_p()
    try:
        code = userenv.GetAppContainerFolderPath(sid, ctypes.byref(folder))
        if code == 0:
            return True
        if hr(code) == "0x80070002":  # ERROR_FILE_NOT_FOUND
            return False
        raise OSError(f"could not check profile: {hr(code)}")
    finally:
        if folder:
            ole32.CoTaskMemFree(folder)


def _creation_time(handle):
    creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                    ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def current_process_creation():
    return _creation_time(kernel32.GetCurrentProcess())


def owner_gone(pid, creation):
    handle = kernel32.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # PID absent is conclusive. Access denied and other errors are not.
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
    try:
        return _creation_time(handle) != creation or kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


@contextmanager
def workspace_claim(name):
    handle = kernel32.CreateMutexW(None, False, "Local\\Starlight-isolated-reclaim-" + name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        acquired = kernel32.WaitForSingleObject(handle, 0) in (WAIT_OBJECT_0, 128)  # WAIT_ABANDONED
        try:
            yield acquired
        finally:
            if acquired:
                kernel32.ReleaseMutex(handle)
    finally:
        kernel32.CloseHandle(handle)


def _windows_directory() -> Path:
    buf = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetWindowsDirectoryW(buf, len(buf))
    if not 0 < length < len(buf):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buf.value)


def _owned_parent(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":" or ".." in path.parts:
        raise ValueError("expected an absolute local workspace directory")
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("reparse points and non-directories are not allowed in the workspace")
    return path.resolve(strict=True)


def _user_sid() -> str:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buf, len(buf), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buf, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents
        sid = sid_to_string(ctypes.c_void_p(user.Sid))
        if not sid:
            raise RuntimeError("could not determine workspace owner SID")
        return sid
    finally:
        kernel32.CloseHandle(token)


def create_workspace(parent: Path, *, prefix: str = "sl25-") -> Path:
    """Create a new host-owned directory with no inherited permission grants."""
    parent = _owned_parent(parent)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", prefix):
        raise ValueError("invalid workspace prefix")
    if kernel32.GetDriveTypeW(parent.anchor) != DRIVE_FIXED:
        raise ValueError("plugin workspace must be on a local fixed volume")
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationW(parent.anchor, None, 0, None, None, ctypes.byref(flags), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not flags.value & FS_PERSISTENT_ACLS:
        raise ValueError("plugin workspace must be on a volume that supports persistent ACLs")
    descriptor = ctypes.c_void_p()
    try:
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{_user_sid()})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
        # Install the private DACL during creation, before the directory can
        # be observed with the parent's permission grants.
        for _ in range(3):
            path = parent / f"{prefix}{secrets.token_hex(16)}"
            if kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                return path
            error = ctypes.get_last_error()
            if error != ERROR_ALREADY_EXISTS:
                raise ctypes.WinError(error)
        raise FileExistsError("could not allocate a private plugin workspace")
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def _scoped_acl(path: Path, sid: str, *, workspace_root: Path) -> Path:
    if not _APPCONTAINER_SID.fullmatch(sid):
        raise ValueError("expected an AppContainer package SID")
    root = _owned_parent(workspace_root)
    child = _owned_parent(path)
    if child == root or not child.is_relative_to(root):
        raise ValueError("ACL change is outside the owned workspace")
    return child


def _icacls(path: Path, *args: str) -> int:
    windows = _windows_directory()
    tool = windows / "System32" / "icacls.exe"
    process = subprocess.run([str(tool), str(path), *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env={"SystemRoot": str(windows)}, creationflags=CREATE_NO_WINDOW,
                             timeout=4, check=False)
    return process.returncode


def grant_acl(path: Path, sid: str, perm: str, *, workspace_root: Path) -> dict:
    if perm not in ("RX", "M"):
        raise ValueError("expected explicit RX or M permissions")
    child = _scoped_acl(path, sid, workspace_root=workspace_root)
    return {"rc": _icacls(child, "/grant:r", f"*{sid}:(OI)(CI){perm}")}


def remove_acl(path: Path, sid: str, *, workspace_root: Path) -> bool:
    child = _scoped_acl(path, sid, workspace_root=workspace_root)
    return _icacls(child, "/remove:g", f"*{sid}") == 0


def make_env(cwd: Path, extra: dict[str, str] | None = None) -> ctypes.c_unicode_buffer:
    local = _owned_parent(cwd)
    temp = local / "Temp"
    temp.mkdir(exist_ok=True)
    _owned_parent(temp)
    windows = _windows_directory()
    env = {
        "SystemRoot": str(windows), "WINDIR": str(windows),
        "LOCALAPPDATA": str(local), "APPDATA": str(local),
        "TEMP": str(temp), "TMP": str(temp),
        "HOMEDRIVE": local.drive, "HOMEPATH": str(local)[len(local.drive):],
        "PATH": os.pathsep.join((str(windows / "System32"), str(windows))),
    }
    for key, value in (extra or {}).items():
        if not re.fullmatch(r"STARLIGHT_[A-Z0-9_]{1,72}", key) or not isinstance(value, str) or "\0" in value:
            raise ValueError("unexpected plugin environment entry")
        env[key] = value
    return ctypes.create_unicode_buffer("".join(f"{key}={env[key]}\0" for key in sorted(env, key=str.upper)) + "\0")


def token_info(hprocess) -> dict:
    htok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(hprocess, TOKEN_QUERY, ctypes.byref(htok)):
        return {"error": f"OpenProcessToken {ctypes.get_last_error()}"}
    try:
        val = wintypes.DWORD(0)
        ret = wintypes.DWORD(0)
        ok = advapi32.GetTokenInformation(htok, TOKEN_IS_APP_CONTAINER, ctypes.byref(val), 4, ctypes.byref(ret))
        is_ac = bool(val.value) if ok else None
        restricted = bool(advapi32.IsTokenRestricted(htok))
        # TokenAppContainerSid: buffer receives a PSID/TOKEN_APPCONTAINER_INFORMATION-like pointer.
        buf = ctypes.create_string_buffer(64)
        size = wintypes.DWORD(0)
        sid_str = None
        sid_err = None
        if advapi32.GetTokenInformation(htok, TOKEN_APP_CONTAINER_SID, buf, 64, ctypes.byref(size)):
            ptr = ctypes.c_void_p.from_buffer(buf).value
            if ptr:
                sid_str = sid_to_string(ctypes.c_void_p(ptr))
        else:
            sid_err = ctypes.get_last_error()
        count = None
        caps_size = wintypes.DWORD()
        advapi32.GetTokenInformation(htok, TOKEN_CAPABILITIES, None, 0, ctypes.byref(caps_size))
        if 4 <= caps_size.value <= 65536:
            caps = ctypes.create_string_buffer(caps_size.value)
            if advapi32.GetTokenInformation(htok, TOKEN_CAPABILITIES, caps, len(caps), ctypes.byref(caps_size)):
                count = wintypes.DWORD.from_buffer(caps).value
        return {"is_appcontainer": is_ac, "is_restricted": restricted,
                "appcontainer_sid": sid_str, "sid_error": sid_err, "capability_count": count}
    finally:
        kernel32.CloseHandle(htok)


@dataclass
class Job:
    active_processes: int = 1
    process_memory_mb: int = 512
    job_memory_mb: int = 1024
    handle: int | None = None
    assigned: bool = False
    error: str | None = None

    def create(self, *, holder=False, lifetime_only=False) -> bool:
        try:
            self.handle = kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
            if lifetime_only:
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if holder:
                # The inert holder cannot run. Its nominal child must enter
                # a separate Job with no breakaway permission.
                info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            info.BasicLimitInformation.ActiveProcessLimit = max(1, int(self.active_processes))
            info.ProcessMemoryLimit = int(self.process_memory_mb) * 1024 * 1024
            info.JobMemoryLimit = int(self.job_memory_mb) * 1024 * 1024
            if not kernel32.SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                   ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.close()
            return False

    def assign(self, hprocess) -> bool:
        if not self.handle:
            self.error = "job not created"
            return False
        if not kernel32.AssignProcessToJobObject(self.handle, hprocess):
            self.error = f"AssignProcessToJobObject {ctypes.get_last_error()}"
            return False
        self.assigned = True
        return True

    def terminate(self) -> bool:
        if self.handle and self.assigned:
            return bool(kernel32.TerminateJobObject(self.handle, 1))
        return False

    def active_count(self) -> int | None:
        if not self.handle:
            return None
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not kernel32.QueryInformationJobObject(
                self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            return None
        return accounting.ActiveProcesses

    def close(self):
        if not self.handle:
            return True
        if not kernel32.CloseHandle(self.handle):
            return False
        self.handle = None
        return True


@dataclass
class LaunchResult:
    process: PROCESS_INFORMATION | None
    parent_handles: dict
    job: Job | None
    profile: Profile
    token: dict = field(default_factory=dict)
    error: str | None = None
    steps: dict = field(default_factory=dict)
    holder: _HandleHolder | None = field(default=None, repr=False)

    def close_handles(self):
        pi = self.process
        if pi and pi.hProcess and kernel32.WaitForSingleObject(pi.hProcess, 0) != WAIT_OBJECT_0:
            return False
        for key, handle in tuple(self.parent_handles.items()):
            if not handle or kernel32.CloseHandle(handle):
                self.parent_handles.pop(key)
        if pi:
            for field_name in ("hThread", "hProcess"):
                handle = getattr(pi, field_name)
                if handle and kernel32.CloseHandle(handle):
                    setattr(pi, field_name, None)
            if not pi.hThread and not pi.hProcess:
                self.process = None
        if self.holder is not None and self.holder.close():
            self.holder = None
        return not self.parent_handles and self.process is None and self.holder is None


def _new_pipe():
    r, w = wintypes.HANDLE(), wintypes.HANDLE()
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, False)
    if not kernel32.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return r, w


@contextmanager
def _attributes(count):
    size = ctypes.c_size_t(0)
    if (kernel32.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
            or ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value):
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size.value)
    if not kernel32.InitializeProcThreadAttributeList(buffer, count, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield buffer
    finally:
        kernel32.DeleteProcThreadAttributeList(buffer)


def _attribute(buffer, kind, value):
    if not kernel32.UpdateProcThreadAttribute(
            buffer, 0, kind, ctypes.byref(value), ctypes.sizeof(value), None, None):
        raise ctypes.WinError(ctypes.get_last_error())


_SUSPENDED_FLAGS = (
    EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | CREATE_SUSPENDED
)


def _in_job(process, job):
    present = wintypes.BOOL()
    if not kernel32.IsProcessInJob(process, job, ctypes.byref(present)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(present.value)


class OwnedProcess:
    """Trusted media child created in an unnamed, non-inherited lifetime Job."""

    def __init__(self, args):
        self.args = args
        self._job = Job()
        self._process = PROCESS_INFORMATION()
        self.returncode = None
        if not self._job.create(lifetime_only=True):
            raise RuntimeError(f"media Job create failed: {self._job.error}")
        try:
            jobs = (wintypes.HANDLE * 1)(self._job.handle)
            with _attributes(1) as attributes:
                _attribute(attributes, PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs)
                startup = STARTUPINFOEXW()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
                if not kernel32.CreateProcessW(
                        str(Path(args[0]).resolve(strict=True)),
                        ctypes.create_unicode_buffer(subprocess.list2cmdline(args)),
                        None, None, False, _SUSPENDED_FLAGS, None, None,
                        ctypes.byref(startup), ctypes.byref(self._process)):
                    raise ctypes.WinError(ctypes.get_last_error())
            self.pid = self._process.dwProcessId
            self._job.assigned = _in_job(self._process.hProcess, self._job.handle)
            if not self._job.assigned:
                raise RuntimeError("media process is outside its Job")
            if kernel32.ResumeThread(self._process.hThread) != 1:
                raise RuntimeError("media process did not leave initial suspension")
        except BaseException:
            if self._process.hProcess:
                kernel32.TerminateProcess(self._process.hProcess, 1)
            self._close_handles()
            raise
        finally:
            if self._process.hThread and kernel32.CloseHandle(self._process.hThread):
                self._process.hThread = None

    def _close_handles(self):
        self._job.close()
        if self._process.hProcess and kernel32.CloseHandle(self._process.hProcess):
            self._process.hProcess = None

    def wait(self, timeout=None):
        if self.returncode is None:
            millis = 0xffffffff if timeout is None else min(0xfffffffe, max(0, math.ceil(timeout * 1000)))
            result = kernel32.WaitForSingleObject(self._process.hProcess, millis)
            if result == 258:  # WAIT_TIMEOUT
                raise subprocess.TimeoutExpired(self.args, timeout)
            code = wintypes.DWORD()
            if result != WAIT_OBJECT_0 or not kernel32.GetExitCodeProcess(self._process.hProcess, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.returncode = code.value
        return self.returncode

    def poll(self):
        try:
            return self.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self):
        if not self._job.terminate() and self.poll() is None:
            raise ctypes.WinError(ctypes.get_last_error())

    kill = terminate

    def __del__(self):
        self._close_handles()


class _HandleHolder:
    # This nominal parent remains suspended for its entire lifetime.

    def __init__(self):
        self.process = PROCESS_INFORMATION()
        self.job = Job()

    def start(self, node_path, cwd, env):
        if not self.job.create(holder=True):
            raise RuntimeError(f"handle holder Job create failed: {self.job.error}")
        jobs = (wintypes.HANDLE * 1)(self.job.handle)
        with _attributes(1) as buffer:
            _attribute(buffer, PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs)
            start_info = STARTUPINFOEXW()
            start_info.StartupInfo.cb = ctypes.sizeof(start_info)
            start_info.lpAttributeList = ctypes.cast(buffer, ctypes.c_void_p)
            # Use the identical pinned trusted image as the real launch. There
            # is no command to execute and this process never leaves suspension.
            if not kernel32.CreateProcessW(
                    str(node_path), None, None, None, False, _SUSPENDED_FLAGS,
                    ctypes.cast(env, ctypes.c_void_p), str(cwd), ctypes.byref(start_info),
                    ctypes.byref(self.process)):
                raise ctypes.WinError(ctypes.get_last_error())
        self.job.assigned = _in_job(self.process.hProcess, self.job.handle)
        if not self.job.assigned:
            raise RuntimeError("handle holder is outside its Job")

    def duplicate(self, handles):
        copies = []
        for handle in handles:
            copy = wintypes.HANDLE()
            if not kernel32.DuplicateHandle(
                    kernel32.GetCurrentProcess(), handle, self.process.hProcess, ctypes.byref(copy),
                    0, True, DUPLICATE_SAME_ACCESS):
                raise ctypes.WinError(ctypes.get_last_error())
            copies.append(copy.value)
        return (wintypes.HANDLE * len(copies))(*copies)

    def close(self):
        process = self.process
        if process.hProcess:
            if kernel32.WaitForSingleObject(process.hProcess, 0) != WAIT_OBJECT_0:
                if not self.job.terminate():
                    kernel32.TerminateProcess(process.hProcess, 1)
                if kernel32.WaitForSingleObject(process.hProcess, 1000) != WAIT_OBJECT_0:
                    return False
            if self.job.assigned:
                deadline = time.monotonic() + 0.1
                count = self.job.active_count()
                while count and time.monotonic() < deadline:
                    time.sleep(0.005)
                    count = self.job.active_count()
                if count != 0:
                    return False
        for name in ("hThread", "hProcess"):
            handle = getattr(process, name)
            if handle and kernel32.CloseHandle(handle):
                setattr(process, name, None)
        return not process.hProcess and not process.hThread and self.job.close()


def launch_appcontainer(*, node_path: Path, command_line: str, cwd: Path, profile: Profile,
                         env_extra: dict[str, str] | None = None, job: Job | None = None,
                         require_job: bool = True, before_resume=None) -> LaunchResult:
    """Create suspended AppContainer process inside its Job, verify token, then resume.

    Any failure after CreateProcess terminates the just-created process and closes
    handles; it never falls back to a normal-privilege Node process.
    """
    parent: dict[str, int] = {}
    child_ends = []
    pi = None
    holder = None
    try:
        if not require_job or job is None:
            raise RuntimeError("a Job Object is mandatory for plugin launch")
        if profile.create_hr != "0x00000000" or profile.already_existed or not profile.sid_string:
            raise RuntimeError("a fresh AppContainer profile is mandatory")
        _profile_name(profile.name)
        # stdin: parent writes w_in, child reads r_in
        r_in, w_in = _new_pipe()
        child_ends.append(r_in)
        parent["stdin"] = w_in
        r_out, w_out = _new_pipe()
        child_ends.append(w_out)
        parent["stdout"] = r_out
        r_err, w_err = _new_pipe()
        child_ends.append(w_err)
        parent["stderr"] = r_err

        holder = _HandleHolder()
        env = make_env(cwd, env_extra)
        holder.start(node_path, cwd, make_env(cwd))
        nominal_parent = wintypes.HANDLE(holder.process.hProcess)
        handles = holder.duplicate(child_ends)
        caps = SECURITY_CAPABILITIES(profile.sid, None, 0, 0)
        if not job.create():
            raise RuntimeError(f"job create failed: {job.error}")
        jobs = (wintypes.HANDLE * 1)(job.handle)
        with _attributes(4) as buffer:
            _attribute(buffer, PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs)
            _attribute(buffer, PROC_THREAD_ATTRIBUTE_PARENT_PROCESS, nominal_parent)
            _attribute(buffer, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles)
            _attribute(buffer, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, caps)
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(si)
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = handles
            si.lpAttributeList = ctypes.cast(buffer, ctypes.c_void_p)
            pi = PROCESS_INFORMATION()
            if not kernel32.CreateProcessW(
                    str(node_path), ctypes.create_unicode_buffer(command_line), None, None, True, _SUSPENDED_FLAGS,
                    ctypes.cast(env, ctypes.c_void_p), str(cwd), ctypes.byref(si), ctypes.byref(pi)):
                raise ctypes.WinError(ctypes.get_last_error())
        child_ends = [handle for handle in child_ends if not kernel32.CloseHandle(handle)]
        if child_ends:
            raise RuntimeError("could not release parent copies of child stdio handles")
        if _in_job(pi.hProcess, holder.job.handle):
            raise RuntimeError("plugin process is inside the handle holder Job")
        # Ending the suspended holder closes its duplicate stdio handles.
        # Neither the holder nor the plugin has run at this point.
        if not holder.close():
            raise RuntimeError("handle holder cleanup pending")
        holder = None

        tok = token_info(pi.hProcess)
        if tok.get("is_appcontainer") is not True:
            raise RuntimeError(f"token is not AppContainer: {tok}")
        if not profile.sid_string or tok.get("appcontainer_sid") != profile.sid_string:
            raise RuntimeError("AppContainer SID absent or mismatched")
        if tok.get("capability_count") != 0:
            raise RuntimeError("AppContainer capabilities absent from attestation or not empty")
        job.assigned = _in_job(pi.hProcess, job.handle)
        if not job.assigned:
            raise RuntimeError("plugin process is outside its Job")
        # Only after token + job checks: resume.
        if before_resume is not None:
            before_resume()
        if kernel32.ResumeThread(pi.hThread) != 1:
            raise RuntimeError("Process did not leave the single initial suspension")
        return LaunchResult(process=pi, parent_handles=parent, job=job, profile=profile, token=tok,
                            steps={"create_process": "suspended", "job_assigned": True,
                                   "holder_reaped": True, "resumed": True})
    except Exception as exc:  # noqa: BLE001
        process = None
        if pi and pi.hProcess:
            if not job or not job.terminate():
                kernel32.TerminateProcess(pi.hProcess, 1)
            if kernel32.WaitForSingleObject(pi.hProcess, 1000) == WAIT_OBJECT_0:
                if not kernel32.CloseHandle(pi.hThread):
                    process = pi
                else:
                    pi.hThread = None
                if kernel32.CloseHandle(pi.hProcess):
                    pi.hProcess = None
                else:
                    process = pi
            else:
                process = pi
        retained_handles = {}
        for name, handle in parent.items():
            if handle and not kernel32.CloseHandle(handle):
                retained_handles[name] = handle
        for i, handle in enumerate(child_ends):
            if not kernel32.CloseHandle(handle):
                retained_handles[f"child-{i}"] = handle
        if job is not None and (not job.assigned or process is None and job.active_count() == 0):
            job.close()
        if holder is not None and holder.close():
            holder = None
        return LaunchResult(process=process, parent_handles=retained_handles, job=job, profile=profile,
                            error=f"{type(exc).__name__}: {exc}", steps={"failed": True}, holder=holder)
