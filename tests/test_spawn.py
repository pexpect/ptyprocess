import os
import time
import unittest
from ptyprocess import PtyProcess, PtyProcessUnicode

class PtyTestCase(unittest.TestCase):
    def test_spawn_sh(self):
        env = os.environ.copy()
        env['FOO'] = 'rebar'
        p = PtyProcess.spawn(['sh'], env=env)
        p.read()
        p.write(b'echo $FOO\n')
        time.sleep(0.1)
        response = p.read()
        assert b'rebar' in response
        
        p.sendeof()
        p.readline()
        
        with self.assertRaises(EOFError):
            p.read()

    def test_spawn_unicode_sh(self):
        env = os.environ.copy()
        env['FOO'] = 'rebar'
        p = PtyProcessUnicode.spawn(['sh'], env=env)
        p.read()
        p.write(u'echo $FOO\n')
        time.sleep(0.1)
        response = p.read()
        assert u'rebar' in response
        
        p.sendeof()
        p.readline()
        
        with self.assertRaises(EOFError):
            p.read()

    def test_quick_spawn(self):
        """Spawn a very short-lived process."""
        # so far only reproducable on Solaris 11, spawning a process
        # that exits very quickly raised an exception at 'inst.setwinsize',
        # because the pty filedes was quickly lost after exec().
        PtyProcess.spawn(['/bin/true'])
