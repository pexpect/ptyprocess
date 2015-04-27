import time
import unittest
from ptyprocess import PtyProcess

class PtyEchoTestCase(unittest.TestCase):

    def _read_until_eof(self, proc):
        """Read away all output on ``proc`` until EOF."""
        while True:
            try:
                proc.read()
            except EOFError:
                return

    def test_waitnoecho_forever(self):
        """Ensure waitnoecho() with no timeout will return when echo=False."""
        cat = PtyProcess.spawn(['cat'], echo=False)
        assert cat.waitnoecho() == True
        assert cat.echo == False
        assert cat.getecho() == False
        cat.sendeof()
        self._read_until_eof(cat)
        assert cat.wait() == 0

    def test_waitnoecho_timeout(self):
        """Ensure waitnoecho() with timeout will return when using stty to unset echo."""
        att_sh = PtyProcess.spawn(['sh'], echo=True)
        # make a prompt we can expect,
        time.sleep(1)
        assert att_sh.getecho() == True
        att_sh.write(b'export PS1="IN: "\n')

        # we must exhaust all awaiting input.  The terminal attributes made by
        # setecho() are not understood by getecho() until all awaiting
        # output is read, irregardless of the TCSANOW attribute we use, it is
        # not reflected until this is done.
        #
        # Furthermore, with stdout line-buffered, we can expect .read() to
        # return full lines.
        inp = b''
        while not inp == b'IN: ':
             inp = att_sh.read().strip(b'\r\n')
             print(inp)

        # we use stty(1) to set echo. By doing so, we can be assured that
        # waitnoecho() will return True even after a short duration (and after
        # all of our output has been read.)
        att_sh.write(b'stty echo\n')
        while not inp == b'IN: ':
             inp = att_sh.read().strip('\r\n')
             print(inp)
        assert att_sh.waitnoecho(timeout=3) == True
        assert att_sh.getecho() == False

        att_sh.write(b'exit 0\n')
        self._read_until_eof(att_sh)
        assert att_sh.wait() == 0
