"""Tests for starting a process without forking this process."""
import errno
import os
import pty
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

from ptyprocess import PtyProcess, PtyProcessUnicode, _posix_spawn

requires_helper = unittest.skipUnless(
    _posix_spawn.available(),
    '_posix_spawn.available() is False, so spawn() still forks')

# The scripts below import ptyprocess from the root of this repository.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This program prints "ready", then sleeps for 30 s or until a signal
# stops it.
SLEEPER = ['sh', '-c', 'echo ready; exec sleep 30']

# This program prints the file descriptors above 2 that it has open.
LIST_OPEN_DESCRIPTORS = """\
import os
for fd in range(3, 1024):
    try:
        os.fstat(fd)
    except OSError:
        continue
    print(fd)
"""

# This code runs in the helper before the code of _posix_spawn_helper.py. The
# helper then sees the file descriptor directories of FreeBSD without fdescfs:
# there is no /proc/self/fd, and /dev/fd lists only 0, 1 and 2.
FREEBSD_WITHOUT_FDESCFS = """\
import os, sys
sys.platform = "freebsd14"
real_listdir = os.listdir
def listdir(path):
    if path == "/proc/self/fd":
        raise FileNotFoundError(path)
    return ["0", "1", "2"] if path == "/dev/fd" else real_listdir(path)
os.listdir = listdir
"""

# This script runs in a new interpreter with file descriptors 0 to 3 closed,
# so the terminal and the error pipe of the first process get numbers that
# the child replaces. The script writes the file name in the error of the
# first process and the output of a second process to result.txt.
WITHOUT_STANDARD_DESCRIPTORS = """\
import os
os.closerange(0, 4)
from ptyprocess import PtyProcessUnicode
output = ""
try:
    PtyProcessUnicode.spawn(["sh"], cwd="missing")
except FileNotFoundError as error:
    output += error.filename + " "
process = PtyProcessUnicode.spawn(["sh", "-c", "echo hello"])
try:
    while True:
        output += process.read(65536)
except EOFError:
    pass
with open("result.txt", "w") as f:
    f.write(output)
"""

# This script runs in a new interpreter. It opens result.txt as file
# descriptor 3, and passes file descriptor 3 to a shell that writes "hello"
# to the file.
PASS_DESCRIPTOR_3 = """\
import os
from ptyprocess import PtyProcess
fd = os.open("result.txt", os.O_WRONLY | os.O_CREAT)
if fd != 3:
    os.dup2(fd, 3)
    os.close(fd)
os.set_inheritable(3, True)
process = PtyProcess.spawn(["sh", "-c", "echo hello >&3"], pass_fds=(3,))
process.wait()
"""

# This script runs in a new interpreter with SIGPIPE at its default action.
# The helper is the program in the first argument, and that program exits
# before it reads the environment. The script writes the error from spawn()
# to result.txt.
SIGPIPE_AT_DEFAULT = """\
import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
sys.executable = sys.argv[1]
from ptyprocess import PtyProcess, _posix_spawn
# The program fails the check in available(), so the script skips it.
_posix_spawn.available = lambda: True
env = dict(os.environ, LARGE="x" * 100000)
try:
    PtyProcess.spawn(["true"], env=env)
except OSError as error:
    with open("result.txt", "w") as f:
        f.write(str(error))
"""

# This script runs in a new interpreter in its own session. The interpreter
# handles SIGINT, as a server does, and sends SIGINT to its own process group
# right after posix_spawn() returns. The script writes the output of the
# command to result.txt.
CTRL_C_IN_THE_CALLER = """\
import os, signal
from ptyprocess import PtyProcessUnicode, _posix_spawn
signal.signal(signal.SIGINT, lambda *args: None)
# The script runs the check in available() now, so that the check does not
# get the SIGINT.
assert _posix_spawn.available()
real_posix_spawn = os.posix_spawn
def posix_spawn(*args, **kwargs):
    pid = real_posix_spawn(*args, **kwargs)
    os.killpg(os.getpgrp(), signal.SIGINT)
    return pid
os.posix_spawn = posix_spawn
process = PtyProcessUnicode.spawn(["sh", "-c", "echo started"])
output = ""
try:
    while True:
        output += process.read(65536)
except EOFError:
    pass
with open("result.txt", "w") as f:
    f.write(output)
"""

# This script runs in a new interpreter that ignores SIGINT, as a job that a
# shell starts with `&` does. The script starts a shell that sends SIGINT to
# itself, first with the helper and then with the fork, and writes the output
# of both shells to result.txt.
SIGINT_IGNORED = """\
import signal
signal.signal(signal.SIGINT, signal.SIG_IGN)
from ptyprocess import PtyProcessUnicode, _posix_spawn
def output_of(argv):
    process = PtyProcessUnicode.spawn(argv)
    output = ""
    try:
        while True:
            output += process.read(65536)
    except EOFError:
        pass
    return output
argv = ["sh", "-c", "kill -INT $$; echo survived"]
output = output_of(argv)
_posix_spawn.available = lambda: False
output += output_of(argv)
with open("result.txt", "w") as f:
    f.write(output)
"""

# This script runs in a new interpreter with SIGPIPE and SIGXFSZ at their
# default action, as some command-line programs set them. The script starts
# shells that send these signals to themselves, first with the helper and then
# with the fork, and writes the output of the shells to result.txt.
SIGNALS_AT_DEFAULT = """\
import signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
from ptyprocess import PtyProcessUnicode, _posix_spawn
def output_of(argv):
    process = PtyProcessUnicode.spawn(argv)
    output = ""
    try:
        while True:
            output += process.read(65536)
    except EOFError:
        pass
    return output
output = ""
for fork in [False, True]:
    if fork:
        _posix_spawn.available = lambda: False
    for name in ["PIPE", "XFSZ"]:
        script = "ulimit -c 0; kill -%s $$; echo survived" % name
        output += output_of(["sh", "-c", script])
with open("result.txt", "w") as f:
    f.write(output)
"""


def read_until_eof(process):
    """Return everything that the process prints until it exits."""
    chunks = []
    while True:
        try:
            chunks.append(process.read(65536))
        except EOFError:
            return ''.join(chunks)


def output_of(argv, **kwargs):
    """Run `argv` in a new terminal and return everything that it printed."""
    process = PtyProcessUnicode.spawn(argv, **kwargs)
    output = read_until_eof(process)
    process.close()
    return output


def open_descriptors():
    """Return the file descriptors above 2 that are open in a program in a new
    terminal.

    While the terminal starts, this process has an inheritable file descriptor
    open.
    """
    read_end, write_end = os.pipe()
    os.set_inheritable(write_end, True)
    try:
        return output_of([sys.executable, '-c', LIST_OPEN_DESCRIPTORS]).split()
    finally:
        os.close(read_end)
        os.close(write_end)


def stop_with_ctrl_c(process):
    """Press Ctrl-C when the program in the terminal is ready, and return its
    signal."""
    output = ''
    while 'ready' not in output:
        output += process.read(65536)
    process.write('\x03')
    read_until_eof(process)
    process.wait()
    status = process.signalstatus
    process.close()
    return status


def survives(signal_name, **kwargs):
    """Return True if a shell in a new terminal survives a signal that it sends
    to itself."""
    # The default action of SIGXFSZ writes a core file, so turn core files off.
    script = 'ulimit -c 0; kill -%s $$; echo survived' % signal_name
    return 'survived' in output_of(['sh', '-c', script], **kwargs)


def open_fds():
    """Return the numbers of the file descriptors that are open in this
    process."""
    return set(os.listdir('/dev/fd'))


def has_exited(pid):
    """Return True if the process `pid` is gone within 10 s.

    A process is gone when it has exited and its parent has waited for it.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def run_script(script, cwd, *args, **kwargs):
    """Run `script` with `args` in a new Python process that imports
    ptyprocess from this repository. `kwargs` go to subprocess.run()."""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        filter(None, [ROOT, env.get('PYTHONPATH')]))
    subprocess.run([sys.executable, '-c', script, *args], cwd=cwd, env=env,
                   timeout=60, check=True, **kwargs)


class PosixSpawnTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = directory.name

    def fake_python(self, script, name='python'):
        """Return the path of a shell script that runs `script`."""
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + script)
        os.chmod(path, 0o755)
        return path

    @requires_helper
    def test_does_not_fork(self):
        def fail():
            raise AssertionError('spawn() forked')

        with mock.patch.object(os, 'fork', fail), \
                mock.patch.object(os, 'forkpty', fail), \
                mock.patch.object(pty, 'fork', fail):
            self.assertIn('hello', output_of(['sh', '-c', 'echo hello']))

    def test_cwd(self):
        output = output_of(['sh', '-c', 'pwd -P'], cwd=self.tmp)
        self.assertEqual(output.split(), [os.path.realpath(self.tmp)])

    def test_env(self):
        env = dict(os.environ, GREETING='hello')
        output = output_of(['sh', '-c', 'echo $GREETING'], env=env)
        self.assertEqual(output.split(), ['hello'])

    def test_c_locale_in_env_reaches_the_command_unchanged(self):
        # When env selects the C locale, CPython sets LC_CTYPE in its own
        # environment (PEP 538), but the command must get the value from env.
        script = 'echo "${LC_CTYPE-unset}"'
        path = os.environ['PATH']
        for env, expected in [
            ({'PATH': path}, 'unset'),
            ({'PATH': path, 'LC_CTYPE': 'C'}, 'C'),
            ({b'PATH': os.fsencode(path), b'LC_CTYPE': b'C'}, 'C'),
        ]:
            with self.subTest(env=env):
                output = output_of(['sh', '-c', script], env=env)
                self.assertEqual(output.split(), [expected])

    def test_pyvenv_launcher_in_env_reaches_the_command_unchanged(self):
        # Since Python 3.11, CPython removes __PYVENV_LAUNCHER__ from its own
        # environment when it starts.
        env = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
        script = 'echo "${__PYVENV_LAUNCHER__-unset}"'
        output = output_of(['sh', '-c', script], env=env)
        self.assertEqual(output.strip(), sys.executable)

    def test_large_env(self):
        # The environment is larger than the buffer of a pipe.
        env = dict(os.environ)
        env.update(('LARGE_%d' % i, str(i) * 30000) for i in range(5))
        script = 'echo ${#LARGE_0} ${#LARGE_4}'
        output = output_of(['sh', '-c', script], env=env)
        self.assertEqual(output.split(), ['30000', '30000'])

    def test_session_leader_with_controlling_terminal(self):
        script = (
            "import os; os.close(os.open('/dev/tty', os.O_RDWR));"
            "print(os.getsid(0) == os.getpid(), os.getpgrp() == os.getpid())"
        )
        output = output_of([sys.executable, '-c', script])
        self.assertEqual(output.split(), ['True', 'True'])

    def test_ctrl_c_interrupts_the_program(self):
        process = PtyProcessUnicode.spawn(SLEEPER)
        self.assertEqual(stop_with_ctrl_c(process), signal.SIGINT)

    @requires_helper
    def test_blocked_signals_are_not_inherited(self):
        # The program must not inherit the signal mask of the thread that
        # starts it.
        processes = []

        def start():
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            processes.append(PtyProcessUnicode.spawn(SLEEPER))

        thread = threading.Thread(target=start)
        thread.start()
        thread.join()
        self.assertEqual(stop_with_ctrl_c(processes[0]), signal.SIGINT)

    @requires_helper
    def test_ctrl_c_in_the_caller_does_not_stop_the_helper(self):
        # The script runs in its own session, so that its SIGINT does not reach
        # the tests.
        run_script(CTRL_C_IN_THE_CALLER, self.tmp, start_new_session=True)
        with open(os.path.join(self.tmp, 'result.txt')) as f:
            self.assertEqual(f.read().split(), ['started'])

    def test_ignored_sigint_stays_ignored(self):
        run_script(SIGINT_IGNORED, self.tmp)
        with open(os.path.join(self.tmp, 'result.txt')) as f:
            self.assertEqual(f.read().split(), ['survived', 'survived'])

    def test_window_size(self):
        output = output_of(['stty', 'size'], dimensions=(30, 100))
        self.assertEqual(output.split(), ['30', '100'])

    def test_bytes_argv(self):
        # pexpect gives bytes when it has an encoding.
        sh = os.fsencode(shutil.which('sh'))
        output = output_of([sh, b'-c', b'echo hello'])
        self.assertEqual(output.split(), ['hello'])

    def assert_restores_signals(self):
        for name in ['PIPE', 'XFSZ']:
            with self.subTest(signal=name):
                self.assertTrue(survives(name))
                self.assertFalse(survives(name, restore_signals=True))

    def test_restore_signals(self):
        self.assert_restores_signals()

    def test_signal_actions_of_the_caller(self):
        # A shell that ignores the signal prints "survived".
        run_script(SIGNALS_AT_DEFAULT, self.tmp)
        with open(os.path.join(self.tmp, 'result.txt')) as f:
            self.assertEqual(f.read().split(), [])

    def test_restore_signals_after_fork(self):
        with mock.patch.object(_posix_spawn, 'available', return_value=False):
            self.assert_restores_signals()

    def test_file_descriptors_are_not_inherited(self):
        self.assertEqual(open_descriptors(), [])

    @unittest.skipUnless(
        sys.implementation.name == 'cpython'
        and sys.platform.startswith(('linux', 'darwin'))
        and hasattr(os, 'posix_spawn'),
        'This check runs only with CPython on Linux and macOS')
    def test_helper_is_available(self):
        # The tests of the helper skip when available() is False. So a helper
        # that does not start fails this test instead. The check runs again
        # here, so that the log of this test gives the reason.
        _posix_spawn._helper_starts.cache_clear()
        self.assertTrue(_posix_spawn._helper_starts(sys.executable))

    @requires_helper
    def test_pass_fds_without_a_list_of_open_descriptors(self):
        # On the BSDs, the helper closes file descriptors with os.closerange().
        path = os.path.join(self.tmp, 'result.txt')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        os.set_inheritable(fd, True)
        code = 'import os; os.write(%d, b"hello")' % fd
        helper = FREEBSD_WITHOUT_FDESCFS + _posix_spawn._helper_source()
        with mock.patch.object(_posix_spawn, '_helper_source', lambda: helper):
            output_of([sys.executable, '-c', code], pass_fds=(fd,))
        with open(path) as f:
            self.assertEqual(f.read(), 'hello')

    @requires_helper
    def test_static_dev_fd_is_not_trusted(self):
        helper = FREEBSD_WITHOUT_FDESCFS + _posix_spawn._helper_source()
        with mock.patch.object(_posix_spawn, '_helper_source', lambda: helper):
            self.assertEqual(open_descriptors(), [])

    @requires_helper
    def test_pass_descriptor_3(self):
        # In the child, the error pipe is not file descriptor 3 then.
        run_script(PASS_DESCRIPTOR_3, self.tmp)
        with open(os.path.join(self.tmp, 'result.txt')) as f:
            self.assertEqual(f.read(), 'hello\n')

    @requires_helper
    def test_without_standard_file_descriptors(self):
        run_script(WITHOUT_STANDARD_DESCRIPTORS, self.tmp)
        with open(os.path.join(self.tmp, 'result.txt')) as f:
            self.assertEqual(f.read().split(), ['missing', 'hello'])

    def test_invalid_executable(self):
        program = os.path.join(self.tmp, 'not-a-program')
        with open(program, 'w') as f:
            f.write('not a program')
        os.chmod(program, 0o755)
        with self.assertRaises(OSError) as caught:
            PtyProcess.spawn([program])
        self.assertEqual(caught.exception.errno, errno.ENOEXEC)

    @requires_helper
    def test_helper_that_exits_before_exec(self):
        # For example, the interpreter does not start, or a signal ends the
        # helper.
        for code, message in [
            ("raise RuntimeError('broken')",
             'code 1 before .*: RuntimeError: broken$'),
            ('import os; os.kill(os.getpid(), 9)', 'code -9 before'),
        ]:
            helper = code + '\n' + _posix_spawn._helper_source()
            with self.subTest(code=code), \
                    mock.patch.object(_posix_spawn, '_helper_source',
                                      lambda: helper):
                with self.assertRaisesRegex(OSError, message):
                    PtyProcess.spawn(['sh', '-c', 'echo started'])

    @requires_helper
    def test_invalid_env_raises_in_the_caller(self):
        # On the fork path, the child of the fork gets this error and runs the
        # rest of the tests.
        for env, message in [
            ({'A=B': '1'}, 'illegal environment variable name'),
            ({'A': '1\0'}, 'embedded null byte'),
        ]:
            with self.subTest(env=env):
                with self.assertRaisesRegex(ValueError, message):
                    PtyProcess.spawn(['sh'], env=env)

    @requires_helper
    def test_env_without_a_variable_that_python_needs(self):
        # This wrapper needs a variable from the environment of this process to
        # start Python. In the same way, an interpreter can need
        # LD_LIBRARY_PATH to find its shared libpython. `env` does not have the
        # variable.
        python = self.fake_python('exec "$PTYPROCESS_PYTHON" "$@"\n')
        env = dict(os.environ)
        with mock.patch.dict(os.environ, PTYPROCESS_PYTHON=sys.executable), \
                mock.patch.object(sys, 'executable', python):
            output = output_of(['sh', '-c', 'echo hello'], env=env)
        self.assertEqual(output.split(), ['hello'])

    @requires_helper
    def test_helper_exit_does_not_raise_sigpipe(self):
        # The helper exits before it reads the environment, which is larger
        # than the buffer of a pipe. The caller has SIGPIPE at its default
        # action, so a write to a pipe with no reader ends the caller.
        python = self.fake_python('exit 1\n')
        run_script(SIGPIPE_AT_DEFAULT, self.tmp, python)
        with open(os.path.join(self.tmp, 'result.txt')) as f:
            self.assertRegex(f.read(), '^The helper exited with code 1 before')

    @requires_helper
    def test_missing_cwd(self):
        missing = os.path.join(self.tmp, 'missing')
        with self.assertRaises(FileNotFoundError) as caught:
            PtyProcess.spawn(['sh'], cwd=missing)
        self.assertEqual(caught.exception.filename, missing)

    @requires_helper
    def test_waiting_for_the_helper_has_a_timeout(self):
        # This wrapper runs Python as a child, so it keeps the error pipe open
        # while the command runs. A helper that waits in chdir() for a stalled
        # network mount does the same.
        python = self.fake_python('%s "$@"\n' % shlex.quote(sys.executable))
        with mock.patch.object(sys, 'executable', python), \
                mock.patch.object(_posix_spawn, 'EXEC_TIMEOUT', 1):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                PtyProcess.spawn(['sleep', '5'])
        self.assertLess(time.monotonic() - start, 4)

    @requires_helper
    def test_failed_start_leaves_no_descriptor_and_no_helper(self):
        # For example, this process has too many open files when it opens the
        # error pipe, or when it waits for the helper.
        helpers = []
        real_posix_spawn = os.posix_spawn

        def posix_spawn(*args, **kwargs):
            helpers.append(real_posix_spawn(*args, **kwargs))
            return helpers[-1]

        def fail(*args):
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

        for module, name in [(os, 'pipe'), (selectors, 'DefaultSelector')]:
            del helpers[:]
            with self.subTest(call=name):
                before = open_fds()
                with mock.patch.object(os, 'posix_spawn', posix_spawn), \
                        mock.patch.object(module, name, fail):
                    with self.assertRaises(OSError) as caught:
                        PtyProcess.spawn(SLEEPER)
                self.assertEqual(caught.exception.errno, errno.EMFILE)
                self.assertEqual(open_fds(), before)
                self.assertTrue(all(map(has_exited, helpers)))

    def test_preexec_fn_forks(self):
        with mock.patch.object(_posix_spawn, 'spawn') as spawn:
            output = output_of(['sh', '-c', 'echo hello'],
                               preexec_fn=lambda: None)
        self.assertIn('hello', output)
        spawn.assert_not_called()

    @requires_helper
    def test_falls_back_to_fork_when_the_helper_does_not_start(self):
        # For example, a relocated interpreter that needs PYTHONHOME does not
        # start with -I. A program that is not Python, for example an
        # application that Nuitka compiled, can exit with 0 or not exit at all.
        for i, script in enumerate(['exit 1', 'exit 0', 'exec sleep 30']):
            # available() keeps its result for each path, so each script needs
            # its own path.
            python = self.fake_python(script + '\n', 'python%d' % i)
            with self.subTest(script=script), \
                    mock.patch.object(sys, 'executable', python), \
                    mock.patch.object(_posix_spawn, 'EXEC_TIMEOUT', 1), \
                    self.assertLogs('ptyprocess._posix_spawn', 'WARNING'), \
                    warnings.catch_warnings():
                warnings.filterwarnings(
                    'ignore', 'This process .* is multi-threaded',
                    DeprecationWarning)
                output = output_of(['sh', '-c', 'echo hello'])
                self.assertIn('hello', output)

    @requires_helper
    def test_environment_variable_selects_the_fork(self):
        with mock.patch.dict(os.environ, PTYPROCESS_USE_FORK='1'), \
                mock.patch.object(_posix_spawn, 'spawn') as spawn, \
                warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', 'This process .* is multi-threaded',
                DeprecationWarning)
            output = output_of(['sh', '-c', 'echo hello'])
        self.assertIn('hello', output)
        spawn.assert_not_called()
