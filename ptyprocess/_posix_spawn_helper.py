"""Start a command in a new session, with the terminal as its controlling
terminal.

ptyprocess._posix_spawn starts the helper with os.posix_spawn(), as
`python -I -S -X utf8 -c CODE error_fd env_fd pass_fds cwd defaults command
[arg ...]`. CODE is the text of this file. File descriptors 0, 1 and 2 are the
terminal, `error_fd` is the write end of the error pipe, `env_fd` is the read
end of the environment pipe, `pass_fds` lists the other file descriptors that
the command keeps, and `defaults` lists the signals that the helper sets to
SIG_DFL. `cwd` is empty when the command starts in the directory of the
caller. With -X utf8, `cwd` and the arguments keep their bytes in every
locale. Without -X utf8, Python 3.14 and older can change or reject some of
these bytes in a legacy multibyte locale, for example ja_JP.eucJP.

Right before `exec`, the helper writes a NUL byte to the error pipe. When an
OSError stops the helper, the helper writes `errno:filename` and exits. So a
failed `exec` gives the NUL byte and then the error. EOF with no data means
that the helper exited for another reason, for example because a signal ended
it.

The helper runs with the environment of the caller, because
_posix_spawn.available() checked the interpreter with this environment. With
the environment of the command, the helper could miss a variable that the
interpreter needs (LD_LIBRARY_PATH), start differently even with -I
(PYTHONEXECUTABLE), or change LC_CTYPE before `exec` (PEP 538). The
environment of the command goes through the environment pipe, because other
users can read the arguments of a process.
"""

# Every import adds to the time that the command takes to start, so the helper
# imports only the modules that it needs.
import fcntl
import os
import signal
import sys
import termios


def main():
    """Set up the terminal and start the command, or write the error to the
    error pipe."""
    error_fd, env_fd, pass_fds, cwd, defaults, command, *args = sys.argv[1:]
    error_fd, env_fd = int(error_fd), int(env_fd)
    keep = {0, 1, 2, error_fd}
    keep.update(map(int, filter(None, pass_fds.split(','))))
    try:
        # The environment pipe has a key, a NUL byte, a value and a NUL byte
        # for each variable. split() gives an empty field after the last NUL
        # byte, and zip() drops this field.
        data = b''
        while True:
            chunk = os.read(env_fd, 65536)
            if not chunk:
                break
            data += chunk
        fields = data.split(b'\0')
        env = dict(zip(fields[::2], fields[1::2]))

        # Make the terminal the controlling terminal of a new session, as
        # pty.fork() does.
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        if cwd:
            os.chdir(cwd)

        # Until setsid(), the helper is in the process group of the caller,
        # and Ctrl-C or a kernel interrupt from jupyter_client sends SIGINT to
        # the whole group. So _posix_spawn starts the helper with SIGINT
        # blocked. SIG_IGN discards a pending SIGINT. The helper then restores
        # the previous action, so the command gets the SIGINT action of the
        # caller, as after fork(). os.posix_spawn(setsid=True) would avoid the
        # pending SIGINT, but the builds of Python that uv installs do not
        # support it.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGINT, previous)
        # Python ignores SIGPIPE, SIGXFZ and SIGXFSZ when it starts, and `exec`
        # keeps ignored signals ignored. So `defaults` names each of these
        # signals that the caller does not ignore, or all of them with
        # `restore_signals`. The command then gets the same actions as after
        # fork().
        for name in filter(None, defaults.split(',')):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, ())

        # Close the file descriptors that the command does not keep, including
        # the environment pipe. The helper finds the open file descriptors in
        # /proc/self/fd, or in /dev/fd on macOS, because os.closerange() can
        # take more than 100 ms: it calls close() for every number up to the
        # limit when close_range() is not available. On the BSDs, /dev/fd can
        # be a static directory that lists only 0, 1 and 2, so the helper does
        # not read /dev/fd there and uses os.closerange().
        try:
            fds = os.listdir('/dev/fd' if sys.platform == 'darwin'
                             else '/proc/self/fd')
        except OSError:
            max_fd = os.sysconf('SC_OPEN_MAX')
            bounds = sorted(keep) + [max_fd if 0 < max_fd < 1048576
                                     else 1048576]
            for low, high in zip(bounds, bounds[1:]):
                os.closerange(low + 1, high)
        else:
            for fd in map(int, fds):
                if fd not in keep:
                    # The list includes the file descriptor of the directory,
                    # and os.listdir() has already closed it.
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        # Because of FD_CLOEXEC, `exec` closes the error pipe. The caller then
        # reads the NUL byte and EOF.
        fcntl.fcntl(error_fd, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        os.write(error_fd, b'\0')
        os.execve(command, [command, *args], env)
    except OSError as e:
        os.write(error_fd,
                 b'%d:' % (e.errno or 0) + os.fsencode(e.filename or ''))
        os._exit(127)


if __name__ == '__main__':
    main()
