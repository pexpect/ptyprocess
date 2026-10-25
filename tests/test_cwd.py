import errno
import os
import subprocess
import sys
import tempfile
import unittest

from ptyprocess import PtyProcess


class CwdTestCase(unittest.TestCase):
    def _spawn_with_invalid_cwd(self, cwd):
        # Isolate the call: an unreported child-side exception must not let the
        # forked child continue running the test suite.
        code = """
from ptyprocess import PtyProcess
import sys
try:
    child = PtyProcess.spawn([sys.executable, '-c', 'pass'], cwd=sys.argv[1])
except OSError as err:
    print('error:{}'.format(err.errno))
else:
    child.close(force=True)
    print('spawned')
"""
        return subprocess.check_output(
            [sys.executable, '-c', code, cwd],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            timeout=10,
        )

    def test_missing_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self._spawn_with_invalid_cwd(
                os.path.join(directory, 'missing')
            )
        self.assertEqual(output, 'error:{}\n'.format(errno.ENOENT).encode())

    def test_cwd_is_not_a_directory(self):
        with tempfile.NamedTemporaryFile() as file:
            output = self._spawn_with_invalid_cwd(file.name)
        self.assertEqual(output, 'error:{}\n'.format(errno.ENOTDIR).encode())

    def test_valid_cwd_before_preexec(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            def preexec():
                with open('preexec-marker', 'w') as file:
                    file.write('ready')

            child = PtyProcess.spawn(
                [sys.executable, '-c', 'import os; print(os.getcwd())'],
                cwd=directory,
                preexec_fn=preexec,
            )
            try:
                output = b''
                while True:
                    try:
                        output += child.read()
                    except EOFError:
                        break
                self.assertEqual(child.wait(), 0)
            finally:
                child.close(force=True)

            self.assertEqual(
                output.strip(), os.fsencode(os.path.realpath(directory))
            )
            with open(os.path.join(directory, 'preexec-marker')) as file:
                self.assertEqual(file.read(), 'ready')
        self.assertEqual(os.getcwd(), original_cwd)
