"""Bound browser helpers to their worker, including failed starts and hard exits.

Call protect_worker() in a dedicated worker BEFORE importing tools or spawning
children, never in the desktop companion. On Windows the non-inherited job
handle lives until process exit; the OS then kills any surviving descendants.
This does not enumerate or terminate unrelated browser processes.
"""
import os
import sys

IDLE_TIMEOUT_MS = 300000
_job_handle = None


def browser_limits():
    # agent-browser's own daemon timer survives the Python cleanup thread.
    # Enforce after .env loading so a profile cannot accidentally disable it.
    os.environ['AGENT_BROWSER_IDLE_TIMEOUT_MS'] = str(IDLE_TIMEOUT_MS)


def protect_worker():
    global _job_handle
    if sys.platform != 'win32' or _job_handle is not None:
        return
    import ctypes
    from ctypes import wintypes as w

    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', w.DWORD), ('min_working_set', ctypes.c_size_t),
                    ('max_working_set', ctypes.c_size_t), ('active_processes', w.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', IoCounters),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.CreateJobObjectW(None, None)  # unnamed, non-inheritable
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
    try:
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    # Do NOT CloseHandle in finally/atexit: that would terminate this worker
    # before its cleanup/output completed. Windows closes it even on TerminateProcess.
    _job_handle = handle


def cleanup_browsers():
    """Polite shutdown first; the job remains a last resort until worker exit."""
    lifecycle = sys.modules.get('tools.browser_tool_lifecycle')
    if lifecycle is not None:
        try:
            lifecycle.cleanup_all_browsers()
        except Exception as error:
            print(f'Agent Bridge browser cleanup failed: {type(error).__name__}', file=sys.stderr)


if __name__ == '__main__':
    # A dedicated wrapper for one-off development probes. The caller must also
    # check that its exact test browser processes exited (see check_browser_cleanup).
    import runpy
    protect_worker()
    browser_limits()
    sys.argv = sys.argv[1:]
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    finally:
        cleanup_browsers()
