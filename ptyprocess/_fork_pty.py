"""
Provides an alternative PTY forking mechanism.

This implementation serves as a substitute for Python's standard `pty.fork()`
functionality, especially on platforms where `os.login_tty()` is unavailable
in the Python build (e.g., some AIX configurations) or where the standard
implementation is problematic (e.g., historically on Solaris).
"""
import os
import errno

from pty import (STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO, CHILD)
from .util import PtyProcessError

def fork_pty():
    '''This implements a substitute for the functionality of pty.fork(),
    aiming for greater portability, especially on systems where Python's
    os.login_tty() is unavailable or pty.fork() is problematic.

    It is designed to work on:
    - Solaris systems (addressing historical issues with Python's pty.fork()).
    - AIX systems where os.login_tty() might not be compiled into Python.
    - Other Unix-like systems that might lack a functional os.login_tty().

    The core logic for establishing a new session and making the pseudo-terminal
    the controlling terminal is handled herein, similar to the operations
    typically performed by login_tty().

    Historical Context (Original Solaris solution by Geoff Marshall, 10.06.05):
    The method was initially implemented to resolve issues with Python's
    pty.fork() on Solaris, particularly for applications like ssh. It was
    inspired by a patch to Python's posixmodule.c authored by Noah Spurrier:
        http://mail.python.org/pipermail/python-dev/2003-May/035281.html
    This approach has been generalized to cover other platforms or scenarios
    where os.login_tty() is not available.
    '''

    parent_fd, child_fd = os.openpty()
    if parent_fd < 0 or child_fd < 0:
        raise OSError("os.openpty() failed")

    pid = os.fork()
    if pid == CHILD:
        # Child.
        os.close(parent_fd)
        pty_make_controlling_tty(child_fd)

        os.dup2(child_fd, STDIN_FILENO)
        os.dup2(child_fd, STDOUT_FILENO)
        os.dup2(child_fd, STDERR_FILENO)

    else:
        # Parent.
        os.close(child_fd)

    return pid, parent_fd

def pty_make_controlling_tty(tty_fd):
    '''This makes the pseudo-terminal the controlling tty. This should be
    more portable than the pty.fork() function. Specifically, this should
    work on Solaris. '''

    child_name = os.ttyname(tty_fd)

    # Disconnect from controlling tty, if any.  Raises OSError of ENXIO
    # if there was no controlling tty to begin with, such as when
    # executed by a cron(1) job.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
    except OSError as err:
        if err.errno != errno.ENXIO:
            raise

    os.setsid()

    # Verify we are disconnected from controlling tty by attempting to open
    # it again.  We expect that OSError of ENXIO should always be raised.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
        raise PtyProcessError("OSError of errno.ENXIO should be raised.")
    except OSError as err:
        if err.errno != errno.ENXIO:
            raise

    # Verify we can open child pty.
    fd = os.open(child_name, os.O_RDWR)
    os.close(fd)

    # Verify we now have a controlling tty.
    fd = os.open("/dev/tty", os.O_WRONLY)
    os.close(fd)
