"""One Hermes process per profile: serialize memory/skill writers across chats."""
from contextlib import contextmanager
import os
from pathlib import Path
import time


@contextmanager
def profile_lock(home, timeout, waiting=lambda: None):
    path = Path(home) / '.agentbridge.lock'
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        started, announced = time.monotonic(), False
        while True:
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('Waiting for another Hermes chat to finish timed out. Try again shortly.')
                if not announced:
                    waiting()
                    announced = True
                time.sleep(.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
