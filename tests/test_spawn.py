import os
import time
import unittest
from ptyprocess import PtyProcess

class PtyTestCase(unittest.TestCase):
    def test_spawn_sh(self):
        env = os.environ.copy()
        env['FOO'] = 'rebar'
        p = PtyProcess.spawn(['sh'], env=env)
        p.read_checking_eof(10)
        p.write(b'echo $FOO\n')
        time.sleep(0.1)
        response = p.read_checking_eof(100)
        assert b'rebar' in response
        
        p.sendeof()
        p.read_checking_eof(10)
        
        with self.assertRaises(EOFError):
            p.read_checking_eof(10)