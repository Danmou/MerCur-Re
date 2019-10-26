# util.py: Miscellaneous useful functions
#
# (C) 2019, Daniel Mouritzen

import ctypes
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from ctypes.util import find_library
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Optional, Sequence, TypeVar, Union, cast

from loguru import logger

libc = ctypes.cdll.LoadLibrary(cast(str, find_library('c')))


def get_config_dir() -> str:
    return str(Path(__file__).parent.parent / 'configs')


@contextmanager
def capture_output(name: str = 'output', level: str = 'TRACE') -> Generator[None, None, None]:
    """
    Context manager that captures all output while it's open (even from C libraries) and logs it.
    Based on https://stackoverflow.com/a/22434262/7759017
    """
    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    with os.fdopen(os.dup(stdout_fd), 'w') as copied_out, \
            os.fdopen(os.dup(stderr_fd), 'w') as copied_err, \
            tempfile.NamedTemporaryFile('w+') as temp_out:
        libc.fflush(None)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(temp_out.fileno(), stdout_fd)
        os.dup2(temp_out.fileno(), stderr_fd)
        try:
            yield
        finally:
            libc.fflush(None)
            sys.stdout.flush()
            os.dup2(copied_out.fileno(), stdout_fd)
            os.dup2(copied_err.fileno(), stderr_fd)
            temp_out.seek(0)
            record = {'name': name, 'function': '', 'line': ''}
            for line in temp_out.readlines():
                logger.patch(lambda r: r.update(record)).log(level, line.rstrip())


class Timer:
    def __enter__(self) -> 'Timer':
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.end = time.perf_counter()
        self.interval = self.end - self.start


T = TypeVar('T')


def measure_time(log_fn: Callable[[str], None] = logger.debug) -> Callable[[T], T]:
    def wrapper(fn: T) -> T:
        def timed(*args: Any, **kwargs: Any) -> Any:
            with Timer() as t:
                result = fn(*args, **kwargs)
            log_fn(f'Call to {fn.__name__} finished in {t.interval:.3g}s')
            return result
        return cast(T, timed)
    return wrapper
