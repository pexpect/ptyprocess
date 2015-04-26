import os
import time
import unittest
from ptyprocess import PtyProcess, PtyProcessUnicode
from ptyprocess.ptyprocess import which

class PtyTestCase(unittest.TestCase):
    def setUp(self):
        self.cmd = u'echo $ENV_KEY; exit 0\n'
        self.env = os.environ.copy()
        self.env_key = u'ENV_KEY'
        self.env_value = u'env_value'
        self.env[self.env_key] = self.env_value

    def _spawn_sh(self, cmd, outp):
        # given,
        p = PtyProcess.spawn(['sh'], env=self.env)
        p.write(cmd)

        # exercise,
        while True:
            try:
                outp += p.read()
            except EOFError:
                break

        # verify, read after EOF keeps throwing EOF
        with self.assertRaises(EOFError):
            p.read()
        with self.assertRaises(EOFError):
            p.readline()

        # verify, input is echo to output
        assert self.cmd.strip() in outp

        # result of echo $ENV_KEY in output
        assert self.env_value in outp

        # exit succesfully (exit 0)
        assert p.wait() == 0


    def test_spawn_sh(self):
        outp = b''
        self._spawn_sh(self.cmd.encode('ascii'), outp)

    def test_spawn_sh_unicode(self):
        outp = u''
        self._spawn_sh(self.cmd, outp)

    def test_quick_spawn(self):
        """Spawn a very short-lived process."""
        # so far only reproducable on Solaris 11, spawning a process
        # that exits very quickly raised an exception at 'inst.setwinsize',
        # because the pty file descriptor was quickly lost after exec().
        PtyProcess.spawn(['true'])
