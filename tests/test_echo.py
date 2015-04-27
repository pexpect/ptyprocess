import time
import unittest
from ptyprocess import PtyProcess

class PtyEchoTestCase(unittest.TestCase):
    def test_waitnoecho_forever(self):
        """Ensure waitnoecho() with no timeout will return when echo=False."""
        cat = PtyProcess.spawn(['cat'], echo=False)
        assert cat.waitnoecho() == True

    def test_waitnoecho_timeout(self):
        """Ensure waitnoecho() with timeout will return when using stty to unset echo."""
        att_sh = PtyProcess.spawn(['sh'], echo=True)
        time.sleep(1)
        att_sh.write('stty echo\n')
        assert att_sh.waitnoecho(timeout=3) == True
