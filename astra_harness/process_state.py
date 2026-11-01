"""保守确认原执行进程退出，避免尚存活的超时线程与后续写入重叠。"""

import os
import re


def process_has_exited(identity):
    # 老检查点缺失身份时没有足够证据，不能靠换一个 Application 放行。
    if not isinstance(identity, str):
        return False
    match = re.fullmatch(r"([1-9][0-9]{0,9})-[0-9a-f]{32}", identity)
    if match is None:
        return False
    pid = int(match[1])
    if pid == os.getpid() or pid > 0xFFFFFFFF:
        return False
    try:
        return _pid_has_exited(pid)
    except (OSError, OverflowError, ValueError):
        # 无查询权限、平台错误都不是进程已经退出的证明。
        return False


def _pid_has_exited(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    # Windows 的 os.kill(pid, 0) 不提供 Unix 式探活语义，必须使用只读查询。
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID 已不存在。
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value != 259  # STILL_ACTIVE；退出码恰好相同也保守不放行。
    finally:
        kernel.CloseHandle(handle)
