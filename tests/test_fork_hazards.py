"""Starting a process must not run code that belongs to other threads.

fork() makes a child process with only one thread: the thread that called
fork(). The child also has a copy of all the memory of this process, including
the thread-local objects of the other threads. Before the command starts,
Python frees these objects, so their finalizers run in the child. A finalizer
can then wait forever for a lock, or change files that this process still
uses.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

# The root of this repository, which the scripts import ptyprocess from
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_script(script, cwd):
    """Run `script` in a new Python process and return its output."""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        filter(None, [ROOT, env.get('PYTHONPATH')]))
    argv = [sys.executable, '-c', textwrap.dedent(script)]
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


class ForkHazardsTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cwd = directory.name

    def test_lock_held_by_another_thread_does_not_hang(self):
        script = r"""
        import logging
        import os
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        from ptyprocess import PtyProcess

        # A child that hangs does not call `exec`, so kill it after 15 s to
        # end the test.
        forked = []


        def recording(fork):
            def wrapper():
                result = fork()
                pid = result[0] if isinstance(result, tuple) else result
                if pid:
                    forked.append(pid)
                return result
            return wrapper


        os.fork = recording(os.fork)
        os.forkpty = recording(os.forkpty)


        def watchdog():
            time.sleep(15)
            print("timed out", flush=True)
            for pid in forked:
                os.kill(pid, 9)


        threading.Thread(target=watchdog, daemon=True).start()

        logging.basicConfig(filename="app.log", level=logging.INFO)
        log = logging.getLogger("library")


        class Session:
            def __del__(self):
                log.info("session closed")


        local = threading.local()


        def handle_request():
            local.session = Session()


        def log_progress():
            while True:
                log.info("working")


        executor = ThreadPoolExecutor()
        executor.submit(handle_request).result()
        threading.Thread(target=log_progress, daemon=True).start()
        time.sleep(1)

        PtyProcess.spawn(["true"])
        print("started", flush=True)
        os._exit(0)
        """
        self.assertEqual(run_script(script, self.cwd), 'started\n')

    def test_objects_of_other_threads_are_not_finalized(self):
        script = r"""
        import os
        import tempfile
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from ptyprocess import PtyProcess

        local = threading.local()


        def export_row(i):
            if not hasattr(local, "workdir"):
                local.workdir = tempfile.TemporaryDirectory(dir=".")
                local.out = open("export.csv", "w")
            local.out.write(f"row {i}\n")
            return local.workdir.name


        def finish():
            local.out.close()


        executor = ThreadPoolExecutor(max_workers=1)
        for i in range(3):
            workdir = executor.submit(export_row, i).result()

        PtyProcess.spawn(["true"])
        executor.submit(finish).result()
        print(os.path.exists(workdir))
        with open("export.csv") as f:
            print(f.read().splitlines(), flush=True)
        os._exit(0)
        """
        self.assertEqual(run_script(script, self.cwd),
                         "True\n['row 0', 'row 1', 'row 2']\n")
