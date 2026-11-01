"""Private process ownership helpers; the launcher never reads workspace code.

Windows starts this trusted file with the base interpreter and -I -S. It waits
for the host's payload; the host assigns its process handle to a kill-on-close
Job *before* releasing that payload. Ordinary CreateProcess descendants inherit
the job. This is cleanup containment, not an account/network/filesystem sandbox.
"""

import json
import os
import subprocess
import sys
import time

LAUNCH_FAILURE = 253
MAX_PAYLOAD = 16384


class WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("user", "kernel", "period_user", "period_kernel")] + [
                        (name, wintypes.DWORD) for name in ("page_faults", "total", "active", "terminated")]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        self._api, self._ctypes, self._accounting = api, ctypes, Accounting
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("job_unavailable")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            api.CloseHandle(self.handle)
            self.handle = None
            raise OSError("job_unavailable")

    def assign(self, process):
        # Popen owns the actual handle; reopening a numerical pid adds a reuse race.
        if not self._api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("job_assignment_failed")

    def active(self):
        info = self._accounting()
        if not self._api.QueryInformationJobObject(self.handle, 1, self._ctypes.byref(info),
                                                   self._ctypes.sizeof(info), None):
            raise OSError("job_query_failed")
        return info.active

    def close(self, timeout=1.0):
        if self.handle is None:
            return True
        confirmed = False
        try:
            self._api.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.active() == 0:
                    confirmed = True
                    break
                time.sleep(0.01)
        except OSError:
            pass
        finally:
            self._api.CloseHandle(self.handle)  # Last handle closure is the fallback kill.
            self.handle = None
        return confirmed


def _launch():
    # Nothing supplied by a task is interpreted until the host releases this
    # bounded message after assigning the trusted launcher to its Job.
    try:
        raw = sys.stdin.buffer.readline(MAX_PAYLOAD + 1)
        if not raw.endswith(b"\n") or len(raw) > MAX_PAYLOAD:
            return LAUNCH_FAILURE
        payload = json.loads(raw)
        argv = payload["argv"]
        if (not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv)
                or not os.path.isabs(argv[0]) or set(payload) != {"argv", "cwd"}):
            return LAUNCH_FAILURE
        child = subprocess.Popen(argv, executable=argv[0], cwd=payload["cwd"], shell=False,
                                 stdin=subprocess.DEVNULL, close_fds=True, env=dict(os.environ))
        return child.wait()
    except BaseException:
        # No exception text, environment, command, or private path is printed.
        return LAUNCH_FAILURE


if __name__ == "__main__":
    sys.exit(_launch())
