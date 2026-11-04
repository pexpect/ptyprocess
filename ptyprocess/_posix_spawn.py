"""Start a process in a new pseudo-terminal without forking this process.

fork() makes a child process with only one thread: the thread that called
fork(). The child also has a copy of all the memory of this process, including
the thread-local objects of the other threads. Before the child calls `exec`,
Python frees these objects, so their finalizers run in the child. Python also
calls the functions registered with os.register_at_fork(). These finalizers
and functions come from extensions and libraries. A finalizer or a function
can wait forever for a lock, because the thread that held the lock does not
exist in the child.

Instead of fork(), spawn() uses os.posix_spawn() to start a small helper
interpreter. The helper sets up the terminal and then calls `exec`, so no code
from this process runs in the child. The code of the helper is in
_posix_spawn_helper.py.
"""
import contextlib
import errno
import fcntl
import functools
import logging
import os
import re
import selectors
import signal
import sys
import termios
import threading
import time

log = logging.getLogger(__name__)

# How many seconds spawn() waits for the helper to call `exec`. spawn() blocks
# the thread that calls it, and a chdir() into a stalled network mount can
# block the helper with no time limit.
EXEC_TIMEOUT = 30

# When this environment variable is 1, PtyProcess.spawn() forks, as in
# ptyprocess 0.7.0.
FORK_VARIABLE = 'PTYPROCESS_USE_FORK'


def available():
    """Return True if spawn() can start a process without forking this
    process. On Solaris, PtyProcess.spawn() keeps its own fork path.

    Return False when the environment variable PTYPROCESS_USE_FORK is 1.
    Otherwise, the first call starts the helper once to test it. This test
    takes 15 to 30 ms on Linux, and at most EXEC_TIMEOUT seconds.
    """
    return (
        os.environ.get(FORK_VARIABLE) != '1'
        and hasattr(os, 'posix_spawn')
        and hasattr(termios, 'TIOCSCTTY')
        and not sys.platform.startswith(('solaris', 'sunos'))
        and not getattr(sys, 'frozen', False)
        and bool(sys.executable)
        and _helper_starts(sys.executable)
    )


@functools.lru_cache(maxsize=None)
def _helper_starts(executable):
    """Return True if the helper starts a command when `executable` runs the
    helper.

    This function starts `executable -I -S -c ""` in a new terminal, in the
    same way as spawn() starts a command. For example, a relocated interpreter
    that needs PYTHONHOME does not start, because the -I option makes the
    interpreter ignore PYTHONHOME. A program that is not Python, for example an
    application that Nuitka compiled, does not run the helper.
    """
    master, tty = os.openpty()
    try:
        pid = spawn(master, tty, [executable, '-I', '-S', '-c', ''],
                    executable=executable)
    except OSError as error:
        log.warning('PtyProcess.spawn() forks, because %s did not run the '
                    'helper: %s', executable, error)
        return False
    finally:
        os.close(master)
        os.close(tty)
    os.waitpid(pid, 0)
    return True


@functools.lru_cache(maxsize=None)
def _helper_source():
    """Return the code of the helper, from _posix_spawn_helper.py.

    spawn() gives this code to `python -I -S -c`, because the helper cannot
    import ptyprocess: with -S, sys.path does not have site-packages.
    """
    # pkgutil also reads the file from a zip file. This function imports
    # pkgutil, because an import at the top of the module adds about 3 ms to
    # the import of ptyprocess.
    import pkgutil
    data = pkgutil.get_data(__package__, '_posix_spawn_helper.py')
    if data is None:
        raise FileNotFoundError('The loader of %s cannot read '
                                '_posix_spawn_helper.py' % __package__)
    return data.decode('utf-8')


def _encode_environment(env):
    """Return `env` in the form that the helper reads from the environment
    pipe. The keys and values of `env` can be str or bytes.

    Raise the same errors as os.execve() for a key or a value that os.execve()
    does not accept, so that the caller of spawn() gets these errors instead of
    the helper.
    """
    fields = []
    for key, value in env.items():
        key, value = os.fsencode(key), os.fsencode(value)
        if b'\0' in key or b'\0' in value:
            raise ValueError('embedded null byte')
        if not key or b'=' in key[1:]:
            raise ValueError('illegal environment variable name')
        fields += [key, value]
    return b''.join(field + b'\0' for field in fields)


def _wait_for_exec(error_read, env_write, environment):
    """Write `environment` to the helper, and return what the helper writes to
    the error pipe.

    This function closes `env_write` after the last byte, because the helper
    reads the environment pipe until EOF. It returns None when the helper does
    not call `exec` within EXEC_TIMEOUT seconds.
    """
    deadline = time.monotonic() + EXEC_TIMEOUT
    unsent = memoryview(environment)
    report = b''
    with os.fdopen(env_write, 'wb', buffering=0) as pipe, \
            selectors.DefaultSelector() as selector:
        os.set_blocking(env_write, False)
        selector.register(error_read, selectors.EVENT_READ)
        selector.register(pipe, selectors.EVENT_WRITE)
        while True:
            ready = selector.select(deadline - time.monotonic())
            if not ready:
                return None
            for key, _ in ready:
                if key.fileobj is not pipe:
                    data = os.read(error_read, 4096)
                    if not data:
                        return report
                    report += data
                    continue
                unsent = unsent[pipe.write(unsent) or 0:]
                if not unsent:
                    selector.unregister(pipe)
                    pipe.close()


def _above(fd, number):
    """Return `fd` if it is above `number`. Otherwise, move `fd` above
    `number` and return the new number."""
    if fd > number:
        return fd
    copy = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, number + 1)
    os.close(fd)
    return copy


def _close(*fds):
    """Close each file descriptor in `fds` that is not -1."""
    for fd in fds:
        if fd != -1:
            os.close(fd)


def _kill(pid):
    """Kill the helper, or the command that the helper started, and wait for
    it in a thread.

    A killed helper that waits in chdir() exits only when chdir() returns, so
    the thread that calls this function does not wait.
    """
    os.kill(pid, signal.SIGKILL)
    threading.Thread(target=os.waitpid, args=(pid, 0), daemon=True).start()


def _last_line(master):
    """Return the last line that the helper printed to the terminal, without
    colors.

    Call this function only after the helper has exited.
    """
    output = b''
    os.set_blocking(master, False)
    # The read raises an OSError when no output is left: EAGAIN, or EIO on
    # Linux when no process has the terminal open.
    with contextlib.suppress(OSError):
        while True:
            chunk = os.read(master, 65536)
            if not chunk:
                break
            output += chunk
    text = re.sub(r'\x1b\[[0-9;]*m', '', output.decode(errors='replace'))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ''


def _exit_code(status):
    """Return the exit code in `status` from os.waitpid(), or minus the number
    of the signal that ended the process."""
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def spawn(master, tty, argv, cwd=None, env=None, pass_fds=(),
          executable=None):
    """Start `argv` in a new session, with `tty` as its controlling terminal,
    and return the process ID. `master` is the other end of the terminal, and
    `executable` is the interpreter of the helper, sys.executable by default.

    The result is the same as with pty.fork(): the child is the leader of a new
    session, `tty` is the controlling terminal of the child, and file
    descriptors 0, 1 and 2 of the child are `tty`. `argv[0]` must be the path
    of the command. The child also keeps the file descriptors in `pass_fds`
    that do not have the close-on-exec flag. The child gets `env` as its
    environment, or the environment of this process when `env` is None.

    When an OSError stops the helper before `exec`, for example because the
    directory `cwd` does not exist, this function raises that error. When the
    helper exits before `exec` for another reason, for example because a
    signal ends it, this function raises OSError with the exit code and the
    last line that the helper printed. This function reads that line from
    `master`. When the helper does not call `exec` within EXEC_TIMEOUT
    seconds, this function kills the helper and raises TimeoutError. An
    exception while this function waits, for example KeyboardInterrupt, also
    kills the helper.
    """
    if executable is None:
        executable = sys.executable
    environment = _encode_environment(os.environ if env is None else env)
    pass_fds = sorted(set(pass_fds) - {0, 1, 2})
    # In the child, the error pipe and the environment pipe get the two lowest
    # numbers above 2 that are not in `pass_fds`.
    error_fd, env_fd = [fd for fd in range(3, len(pass_fds) + 5)
                        if fd not in pass_fds][:2]
    defaults = [name for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ')
                if hasattr(signal, name) and
                signal.getsignal(getattr(signal, name)) != signal.SIG_IGN]
    # _posix_spawn_helper.py explains the arguments, the file descriptors, the
    # environment and the blocked SIGINT.
    helper_argv = [
        executable, '-I', '-S', '-X', 'utf8', '-c', _helper_source(),
        str(error_fd), str(env_fd), ','.join(map(str, pass_fds)), cwd or '',
        ','.join(defaults),
    ] + list(argv)

    tty_copy = error_read = error_write = env_read = env_write = -1
    try:
        # In the child, posix_spawn() copies `tty_copy`, `error_write` and
        # `env_read` to file descriptors 0, 1, 2, `error_fd` and `env_fd`. The
        # source of a copy must not be the target of an earlier copy. It also
        # must not be its own target, because older C libraries then keep the
        # close-on-exec flag. So the sources get numbers above `env_fd`.
        tty_copy = fcntl.fcntl(tty, fcntl.F_DUPFD_CLOEXEC, env_fd + 1)
        error_read, error_write = os.pipe()
        error_write = _above(error_write, env_fd)
        env_read, env_write = os.pipe()
        env_read = _above(env_read, env_fd)
        pid = os.posix_spawn(
            executable,
            helper_argv,
            os.environ,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, tty_copy, 0),
                (os.POSIX_SPAWN_DUP2, tty_copy, 1),
                (os.POSIX_SPAWN_DUP2, tty_copy, 2),
                (os.POSIX_SPAWN_DUP2, error_write, error_fd),
                (os.POSIX_SPAWN_DUP2, env_read, env_fd),
            ],
            setsigmask={signal.SIGINT},
        )
    except BaseException:
        _close(error_read, env_read, env_write)
        raise
    finally:
        _close(tty_copy, error_write)

    # This process keeps its copy of the read end of the environment pipe
    # until the wait ends. The pipe then always has a reader, so a write to it
    # cannot raise SIGPIPE, which would end this process when SIGPIPE has its
    # default action.
    try:
        report = _wait_for_exec(error_read, env_write, environment)
    except BaseException:
        _kill(pid)
        raise
    finally:
        os.close(error_read)
        os.close(env_read)
    if report is None:
        _kill(pid)
        raise TimeoutError(errno.ETIMEDOUT, 'The command did not start '
                           'within %d s' % EXEC_TIMEOUT)
    error = report[1:] if report.startswith(b'\0') else report
    if error:
        os.waitpid(pid, 0)
        code, _, filename = error.partition(b':')
        raise OSError(int(code), os.strerror(int(code)),
                      os.fsdecode(filename) or None)
    if not report:
        # The helper exited before `exec`, and no OSError stopped it.
        line = _last_line(master)
        _, status = os.waitpid(pid, 0)
        message = 'The helper exited with code %d before %s started' % (
            _exit_code(status), os.fsdecode(argv[0]))
        if line:
            message += ': ' + line
        raise OSError(message)
    return pid
