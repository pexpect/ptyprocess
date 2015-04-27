import os
import time
import select
import unittest
from ptyprocess import PtyProcess, PtyProcessUnicode

class PtyTestCase(unittest.TestCase):
    def setUp(self):
        self.cmd = u'echo $ENV_KEY; exit 0\n'
        self.env = os.environ.copy()
        self.env_key = u'ENV_KEY'
        self.env_value = u'env_value'
        self.env[self.env_key] = self.env_value

    def _canread(self, fd, timeout=1):
        return fd in select.select([fd], [], [], timeout)[0]

    def _spawn_sh(self, ptyp, cmd, outp):
        # given,
        p = ptyp.spawn(['sh'], env=self.env)
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
        self._spawn_sh(PtyProcess, self.cmd.encode('ascii'), outp)

    def test_spawn_sh_unicode(self):
        outp = u''
        self._spawn_sh(PtyProcessUnicode, self.cmd, outp)

    def test_quick_spawn(self):
        """Spawn a very short-lived process."""
        # so far only reproducable on Solaris 11, spawning a process
        # that exits very quickly raised an exception at 'inst.setwinsize',
        # because the pty file descriptor was quickly lost after exec().
        PtyProcess.spawn(['true'])

    def _interactive_repl(self, echo):
        """Test Call and response in proc.readline(), echo OFF."""
        # given,
        bc = PtyProcessUnicode.spawn(['bc'], echo=echo)
	given_input = u'2+2+2+2+2+2+2+2+2+2+2+2+2+2+2+2+2+2+2+2\n'
        expected_output = u'40'

        # gnu-bc will display a long FSF banner on startup,
        # whereas bsd-bc (on FreeBSD, Solaris) display no
        # banner at all.  To ensure we've read up to our
        # current prompt, read until the response of '2^16' is found.
        time.sleep(1)

        bc.write(u'2^16\n')
        outp = u''
        while self._canread(bc.fd) and not u'65536' in outp:
            outp += bc.read()
        assert '65536' in outp

        # ensure terminal echo reflects our requested settings
        if echo == False:
            bc.setecho(echo)
            assert bc.waitnoecho(timeout=3) == True
            assert bc.getecho() == False

        # exercise,
        bc.write(given_input)

        # TODO: We're seeing our input on output on FreeBSD with
        # echo=False, and when echo=True, .getecho() returns False.
        # This might be another case of 'setecho not supported on
        # this platform'??

        if echo:
           # validate input echoed to output.  This is where
           # the '_echo' TestCase differs from the previous
           # '_noecho' varient.
           assert bc.readline().strip() == given_input.strip()

        assert bc.readline().strip() == expected_output

        # exercise sending EOF
        bc.sendeof()

        # validate EOF on read
        while True:
            try:
                bc.readline()
            except EOFError:
                break

        # validate exit status,
        assert bc.wait() == 0

    def test_interactive_repl_unicode_noecho(self):
        self._interactive_repl(echo=False)

    def test_interactive_repl_unicode_echo(self):
        self._interactive_repl(echo=True)
