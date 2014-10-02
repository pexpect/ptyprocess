import errno
import fcntl
import io
import os
import pty
import re
import resource
import signal
import struct
import sys
import termios
import time
import tty

# Constants
from pty import (STDIN_FILENO, STDOUT_FILENO, CHILD)

from .util import which

_platform = sys.platform.lower()

# Solaris uses internal __fork_pty(). All others use pty.fork().
use_native_pty_fork = not (
    _platform.startswith('solaris') or
    _platform.startswith('sunos'))

if not use_native_pty_fork:
    from . import _fork_pty

PY3 = sys.version_info[0] >= 3

if PY3:
    def _byte(i):
        return bytes([i])
else:
    def _byte(i):
        return chr(i)
    
    FileNotFoundError = OSError

_EOF, _INTR = None, None

def _make_eof_intr():
    """Set constants _EOF and _INTR.
    
    This avoids doing potentially costly operations on module load.
    """
    global _EOF, _INTR
    if (_EOF is not None) and (_INTR is not None):
        pass

    # inherit EOF and INTR definitions from controlling process.
    try:
        from termios import VEOF, VINTR
        try:
            fd = sys.__stdin__.fileno()
        except ValueError:
            # ValueError: I/O operation on closed file
            fd = sys.__stdout__.fileno()
        intr = ord(termios.tcgetattr(fd)[6][VINTR])
        eof = ord(termios.tcgetattr(fd)[6][VEOF])
    except (ImportError, OSError, IOError, ValueError, termios.error):
        # unless the controlling process is also not a terminal,
        # such as cron(1), or when stdin and stdout are both closed.
        # Fall-back to using CEOF and CINTR. There
        try:
            from termios import CEOF, CINTR
            (intr, eof) = (CINTR, CEOF)
        except ImportError:
            #                         ^C, ^D
            (intr, eof) = (3, 4)
    
    _INTR = _byte(intr)
    _EOF = _byte(eof)


class PtyProcess(object):
    '''This class represents a process running in a pseudoterminal.
    
    The main constructor is the :meth:`spawn` classmethod.
    '''
    string_type = bytes
    if PY3:
        allowed_string_types = (bytes, str)
        @staticmethod
        def _chr(c):
            return bytes([c])
        linesep = os.linesep.encode('ascii')
        crlf = '\r\n'.encode('ascii')

        @staticmethod
        def write_to_stdout(b):
            try:
                return sys.stdout.buffer.write(b)
            except AttributeError:
                # If stdout has been replaced, it may not have .buffer
                return sys.stdout.write(b.decode('ascii', 'replace'))
    else:
        allowed_string_types = (basestring,)  # analysis:ignore
        _chr = staticmethod(chr)
        linesep = os.linesep
        crlf = '\r\n'
        write_to_stdout = sys.stdout.write

    encoding = None
    
    argv = None
    env = None
    launch_dir = None

    def __init__(self, pid, fd):
        _make_eof_intr()  # Ensure _EOF and _INTR are calculated
        self.pid = pid
        self.fd = fd
        self.fileobj = self.fileobj_bytes = io.open(fd, 'r+b', buffering=0)

        self.terminated = False
        self.closed = False
        self.exitstatus = None
        self.signalstatus = None
        # status returned by os.waitpid
        self.status = None
        self.flag_eof = False
        # Delay used before sending data to child. Time in seconds.
        # Most Linux machines don't like this to be below 0.03 (30 ms).
        self.delaybeforesend = 0.05
        # Used by close() to give kernel time to update process status.
        # Time in seconds.
        self.delayafterclose = 0.1
        # Used by terminate() to give kernel time to update process status.
        # Time in seconds.
        self.delayafterterminate = 0.1
        # This flags if we are running on irix
        self.__irix_hack = _platform.startswith('irix')

    @classmethod
    def spawn(cls, argv, timeout=30, maxread=2000,
        searchwindowsize=None, logfile=None, cwd=None, env=None,
        ignore_sighup=True, echo=True):
        '''Start the given command in a child process in a pseudo terminal.
        
        This does all the fork/exec type of stuff for a pty, and returns an
        instance of PtyProcess.
        '''
        # Note that it is difficult for this method to fail.
        # You cannot detect if the child process cannot start.
        # So the only way you can tell if the child process started
        # or not is to try to read from the file descriptor. If you get
        # EOF immediately then it means that the child is already dead.
        # That may not necessarily be bad because you may have spawned a child
        # that performs some task; creates no stdout output; and then dies.

        # Shallow copy of argv so we can modify it
        argv = argv[:]
        command = argv[0]

        command_with_path = which(command)
        if command_with_path is None:
            raise FileNotFoundError('The command was not found or was not ' +
                                    'executable: %s.' % command)
        command = command_with_path
        argv[0] = command

        if use_native_pty_fork:
            pid, fd = pty.fork()
        else:
            # Use internal fork_pty, for Solaris
            pid, fd = _fork_pty.fork_pty()

        # Some platforms must call setwinsize() and setecho() from the
        # child process, and others from the master process. We do both,
        # allowing IOError for either.

        if pid == CHILD:
            # HACK: Make a PtyProcess instance in the child, so we can use our
            # implementation of setecho() and setwinsize()
            inst = cls(0, fd)
            # set default window size of 24 rows by 80 columns
            try:
                inst.setwinsize(24, 80)
            except IOError as err:
                if err.args[0] not in (errno.EINVAL, errno.ENOTTY):
                    raise

            # disable echo if spawn argument echo was unset
            if not echo:
                try:
                    inst.setecho(False)
                except (IOError, termios.error) as err:
                    if err.args[0] not in (errno.EINVAL, errno.ENOTTY):
                        raise

            # Do not allow child to inherit open file descriptors from parent.
            max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            os.closerange(3, max_fd)

            if cwd is not None:
                os.chdir(cwd)
            if env is None:
                os.execv(command, argv)
            else:
                os.execvpe(command, argv, env)

        # Parent
        inst = cls(pid, fd)
        
        # Set some informational attributes
        inst.argv = argv
        if env is not None:
            inst.env = env
        if cwd is not None:
            inst.launch_dir = cwd
        
        try:
            inst.setwinsize(24, 80)
        except IOError as err:
            if err.args[0] not in (errno.EINVAL, errno.ENOTTY):
                raise

        return inst

    def __repr__(self):
        clsname = type(self).__name__
        if self.argv is not None:
            args = [repr(self.argv)]
            if self.env is not None:
                args.append("env=%r" % self.env)
            if self.launch_dir is not None:
                args.append("cwd=%r" % self.launch_dir)
            
            return "{}.spawn({})".format(clsname, ", ".join(args))
        
        else:
            return "{}(pid={}, fd={})".format(clsname, self.pid, self.fd)

    @staticmethod
    def _coerce_send_string(s):
        if not isinstance(s, bytes):
            return s.encode('utf-8')
        return s

    @staticmethod
    def _coerce_read_string(s):
        return s

    def __del__(self):
        '''This makes sure that no system resources are left open. Python only
        garbage collects Python objects. OS file descriptors are not Python
        objects, so they must be handled explicitly. If the child file
        descriptor was opened outside of this class (passed to the constructor)
        then this does not close it. '''

        if not self.closed:
            # It is possible for __del__ methods to execute during the
            # teardown of the Python VM itself. Thus self.close() may
            # trigger an exception because os.close may be None.
            try:
                self.close()
            # which exception, shouldnt' we catch explicitly .. ?
            except:
                pass


    def fileno(self):
        '''This returns the file descriptor of the pty for the child.
        '''
        return self.child_fd

    def close(self, force=True):
        '''This closes the connection with the child application. Note that
        calling close() more than once is valid. This emulates standard Python
        behavior with files. Set force to True if you want to make sure that
        the child is terminated (SIGKILL is sent if the child ignores SIGHUP
        and SIGINT). '''

        if not self.closed:
            self.flush()
            os.close(self.child_fd)
            # Give kernel time to update process status.
            time.sleep(self.delayafterclose)
            if self.isalive():
                if not self.terminate(force):
                    raise ExceptionPexpect('Could not terminate the child.')
            self.child_fd = -1
            self.closed = True
            #self.pid = None

    def flush(self):
        '''This does nothing. It is here to support the interface for a
        File-like object. '''

        pass

    def isatty(self):
        '''This returns True if the file descriptor is open and connected to a
        tty(-like) device, else False.

        On SVR4-style platforms implementing streams, such as SunOS and HP-UX,
        the child pty may not appear as a terminal device.  This means
        methods such as setecho(), setwinsize(), getwinsize() may raise an
        IOError. '''

        return os.isatty(self.child_fd)

    def waitnoecho(self, timeout=-1):
        '''This waits until the terminal ECHO flag is set False. This returns
        True if the echo mode is off. This returns False if the ECHO flag was
        not set False before the timeout. This can be used to detect when the
        child is waiting for a password. Usually a child application will turn
        off echo mode when it is waiting for the user to enter a password. For
        example, instead of expecting the "password:" prompt you can wait for
        the child to set ECHO off::

            p = pexpect.spawn('ssh user@example.com')
            p.waitnoecho()
            p.sendline(mypassword)

        If timeout==-1 then this method will use the value in self.timeout.
        If timeout==None then this method to block until ECHO flag is False.
        '''

        if timeout == -1:
            timeout = self.timeout
        if timeout is not None:
            end_time = time.time() + timeout
        while True:
            if not self.getecho():
                return True
            if timeout < 0 and timeout is not None:
                return False
            if timeout is not None:
                timeout = end_time - time.time()
            time.sleep(0.1)

    def getecho(self):
        '''This returns the terminal echo mode. This returns True if echo is
        on or False if echo is off. Child applications that are expecting you
        to enter a password often set ECHO False. See waitnoecho().

        Not supported on platforms where ``isatty()`` returns False.  '''

        try:
            attr = termios.tcgetattr(self.child_fd)
        except termios.error as err:
            errmsg = 'getecho() may not be called on this platform'
            if err.args[0] == errno.EINVAL:
                raise IOError(err.args[0], '%s: %s.' % (err.args[1], errmsg))
            raise

        self.echo = bool(attr[3] & termios.ECHO)
        return self.echo

    def setecho(self, state):
        '''This sets the terminal echo mode on or off. Note that anything the
        child sent before the echo will be lost, so you should be sure that
        your input buffer is empty before you call setecho(). For example, the
        following will work as expected::

            p = pexpect.spawn('cat') # Echo is on by default.
            p.sendline('1234') # We expect see this twice from the child...
            p.expect(['1234']) # ... once from the tty echo...
            p.expect(['1234']) # ... and again from cat itself.
            p.setecho(False) # Turn off tty echo
            p.sendline('abcd') # We will set this only once (echoed by cat).
            p.sendline('wxyz') # We will set this only once (echoed by cat)
            p.expect(['abcd'])
            p.expect(['wxyz'])

        The following WILL NOT WORK because the lines sent before the setecho
        will be lost::

            p = pexpect.spawn('cat')
            p.sendline('1234')
            p.setecho(False) # Turn off tty echo
            p.sendline('abcd') # We will set this only once (echoed by cat).
            p.sendline('wxyz') # We will set this only once (echoed by cat)
            p.expect(['1234'])
            p.expect(['1234'])
            p.expect(['abcd'])
            p.expect(['wxyz'])


        Not supported on platforms where ``isatty()`` returns False.
        '''

        errmsg = 'setecho() may not be called on this platform'

        try:
            attr = termios.tcgetattr(self.child_fd)
        except termios.error as err:
            if err.args[0] == errno.EINVAL:
                raise IOError(err.args[0], '%s: %s.' % (err.args[1], errmsg))
            raise

        if state:
            attr[3] = attr[3] | termios.ECHO
        else:
            attr[3] = attr[3] & ~termios.ECHO

        try:
            # I tried TCSADRAIN and TCSAFLUSH, but these were inconsistent and
            # blocked on some platforms. TCSADRAIN would probably be ideal.
            termios.tcsetattr(self.child_fd, termios.TCSANOW, attr)
        except IOError as err:
            if err.args[0] == errno.EINVAL:
                raise IOError(err.args[0], '%s: %s.' % (err.args[1], errmsg))
            raise

        self.echo = state

    def read_nonblocking(self, size=1, timeout=-1):
        '''This reads at most size characters from the child application. It
        includes a timeout. If the read does not complete within the timeout
        period then a TIMEOUT exception is raised. If the end of file is read
        then an EOF exception will be raised. If a log file was set using
        setlog() then all data will also be written to the log file.

        If timeout is None then the read may block indefinitely.
        If timeout is -1 then the self.timeout value is used. If timeout is 0
        then the child is polled and if there is no data immediately ready
        then this will raise a TIMEOUT exception.

        The timeout refers only to the amount of time to read at least one
        character. This is not effected by the 'size' parameter, so if you call
        read_nonblocking(size=100, timeout=30) and only one character is
        available right away then one character will be returned immediately.
        It will not wait for 30 seconds for another 99 characters to come in.

        This is a wrapper around os.read(). It uses select.select() to
        implement the timeout. '''

        if self.closed:
            raise ValueError('I/O operation on closed file.')

        if timeout == -1:
            timeout = self.timeout

        # Note that some systems such as Solaris do not give an EOF when
        # the child dies. In fact, you can still try to read
        # from the child_fd -- it will block forever or until TIMEOUT.
        # For this case, I test isalive() before doing any reading.
        # If isalive() is false, then I pretend that this is the same as EOF.
        if not self.isalive():
            # timeout of 0 means "poll"
            r, w, e = self.__select([self.child_fd], [], [], 0)
            if not r:
                self.flag_eof = True
                raise EOF('End Of File (EOF). Braindead platform.')
        elif self.__irix_hack:
            # Irix takes a long time before it realizes a child was terminated.
            # FIXME So does this mean Irix systems are forced to always have
            # FIXME a 2 second delay when calling read_nonblocking? That sucks.
            r, w, e = self.__select([self.child_fd], [], [], 2)
            if not r and not self.isalive():
                self.flag_eof = True
                raise EOF('End Of File (EOF). Slow platform.')

        r, w, e = self.__select([self.child_fd], [], [], timeout)

        if not r:
            if not self.isalive():
                # Some platforms, such as Irix, will claim that their
                # processes are alive; timeout on the select; and
                # then finally admit that they are not alive.
                self.flag_eof = True
                raise EOF('End of File (EOF). Very slow platform.')
            else:
                raise TIMEOUT('Timeout exceeded.')

        if self.child_fd in r:
            try:
                s = os.read(self.child_fd, size)
            except OSError as err:
                if err.args[0] == errno.EIO:
                    # Linux-style EOF
                    self.flag_eof = True
                    raise EOF('End Of File (EOF). Exception style platform.')
                raise
            if s == b'':
                # BSD-style EOF
                self.flag_eof = True
                raise EOF('End Of File (EOF). Empty string style platform.')

            s = self._coerce_read_string(s)
            self._log(s, 'read')
            return s

        raise ExceptionPexpect('Reached an unexpected state.')  # pragma: no cover

    def read(self, size=-1):
        '''This reads at most "size" bytes from the file (less if the read hits
        EOF before obtaining size bytes). If the size argument is negative or
        omitted, read all data until EOF is reached. The bytes are returned as
        a string object. An empty string is returned when EOF is encountered
        immediately. '''

        if size == 0:
            return self.string_type()
        if size < 0:
            # delimiter default is EOF
            self.expect(self.delimiter)
            return self.before

        # I could have done this more directly by not using expect(), but
        # I deliberately decided to couple read() to expect() so that
        # I would catch any bugs early and ensure consistant behavior.
        # It's a little less efficient, but there is less for me to
        # worry about if I have to later modify read() or expect().
        # Note, it's OK if size==-1 in the regex. That just means it
        # will never match anything in which case we stop only on EOF.
        cre = re.compile(self._coerce_expect_string('.{%d}' % size), re.DOTALL)
        # delimiter default is EOF
        index = self.expect([cre, self.delimiter])
        if index == 0:
            ### FIXME self.before should be ''. Should I assert this?
            return self.after
        return self.before

    def readline(self, size=-1):
        '''This reads and returns one entire line. The newline at the end of
        line is returned as part of the string, unless the file ends without a
        newline. An empty string is returned if EOF is encountered immediately.
        This looks for a newline as a CR/LF pair (\\r\\n) even on UNIX because
        this is what the pseudotty device returns. So contrary to what you may
        expect you will receive newlines as \\r\\n.

        If the size argument is 0 then an empty string is returned. In all
        other cases the size argument is ignored, which is not standard
        behavior for a file-like object. '''

        if size == 0:
            return self.string_type()
        # delimiter default is EOF
        index = self.expect([self.crlf, self.delimiter])
        if index == 0:
            return self.before + self.crlf
        else:
            return self.before

    def __iter__(self):
        '''This is to support iterators over a file-like object.
        '''
        return iter(self.readline, self.string_type())

    def readlines(self, sizehint=-1):
        '''This reads until EOF using readline() and returns a list containing
        the lines thus read. The optional 'sizehint' argument is ignored.
        Remember, because this reads until EOF that means the child
        process should have closed its stdout. If you run this method on
        a child that is still running with its stdout open then this
        method will block until it timesout.'''

        lines = []
        while True:
            line = self.readline()
            if not line:
                break
            lines.append(line)
        return lines

    def write(self, s):
        '''Write data to the pseudoterminal.
        
        Returns the number of bytes/characters written.
        '''
        return self.fileobj.write(s)

    def sendcontrol(self, char):
        '''Helper method that wraps send() with mnemonic access for sending control
        character to the child (such as Ctrl-C or Ctrl-D).  For example, to send
        Ctrl-G (ASCII 7, bell, '\a')::

            child.sendcontrol('g')

        See also, sendintr() and sendeof().
        '''

        char = char.lower()
        a = ord(char)
        if a >= 97 and a <= 122:
            a = a - ord('a') + 1
            return self.fileobj_bytes.write(_byte(a))
        d = {'@': 0, '`': 0,
            '[': 27, '{': 27,
            '\\': 28, '|': 28,
            ']': 29, '}': 29,
            '^': 30, '~': 30,
            '_': 31,
            '?': 127}
        if char not in d:
            return 0
        
        return self.fileobj_bytes.write(_byte(d[char]))

    def sendeof(self):
        '''This sends an EOF to the child. This sends a character which causes
        the pending parent output buffer to be sent to the waiting child
        program without waiting for end-of-line. If it is the first character
        of the line, the read() in the user program returns 0, which signifies
        end-of-file. This means to work as expected a sendeof() has to be
        called at the beginning of a line. This method does not send a newline.
        It is the responsibility of the caller to ensure the eof is sent at the
        beginning of a line. '''

        self.fileobj_bytes.write(_EOF)

    def sendintr(self):
        '''This sends a SIGINT to the child. It does not require
        the SIGINT to be the first character on a line. '''

        self.fileobj_bytes.write(_EOF)

    def eof(self):
        '''This returns True if the EOF exception was ever raised.
        '''

        return self.flag_eof

    def terminate(self, force=False):
        '''This forces a child process to terminate. It starts nicely with
        SIGHUP and SIGINT. If "force" is True then moves onto SIGKILL. This
        returns True if the child was terminated. This returns False if the
        child could not be terminated. '''

        if not self.isalive():
            return True
        try:
            self.kill(signal.SIGHUP)
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            self.kill(signal.SIGCONT)
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            self.kill(signal.SIGINT)
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            if force:
                self.kill(signal.SIGKILL)
                time.sleep(self.delayafterterminate)
                if not self.isalive():
                    return True
                else:
                    return False
            return False
        except OSError:
            # I think there are kernel timing issues that sometimes cause
            # this to happen. I think isalive() reports True, but the
            # process is dead to the kernel.
            # Make one last attempt to see if the kernel is up to date.
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            else:
                return False

    def wait(self):
        '''This waits until the child exits. This is a blocking call. This will
        not read any data from the child, so this will block forever if the
        child has unread output and has terminated. In other words, the child
        may have printed output then called exit(), but, the child is
        technically still alive until its output is read by the parent. '''

        if self.isalive():
            pid, status = os.waitpid(self.pid, 0)
        else:
            raise ExceptionPexpect('Cannot wait for dead child process.')
        self.exitstatus = os.WEXITSTATUS(status)
        if os.WIFEXITED(status):
            self.status = status
            self.exitstatus = os.WEXITSTATUS(status)
            self.signalstatus = None
            self.terminated = True
        elif os.WIFSIGNALED(status):
            self.status = status
            self.exitstatus = None
            self.signalstatus = os.WTERMSIG(status)
            self.terminated = True
        elif os.WIFSTOPPED(status):  # pragma: no cover
            # You can't call wait() on a child process in the stopped state.
            raise ExceptionPexpect('Called wait() on a stopped child ' +
                    'process. This is not supported. Is some other ' +
                    'process attempting job control with our child pid?')
        return self.exitstatus

    def isalive(self):
        '''This tests if the child process is running or not. This is
        non-blocking. If the child was terminated then this will read the
        exitstatus or signalstatus of the child. This returns True if the child
        process appears to be running or False if not. It can take literally
        SECONDS for Solaris to return the right status. '''

        if self.terminated:
            return False

        if self.flag_eof:
            # This is for Linux, which requires the blocking form
            # of waitpid to get the status of a defunct process.
            # This is super-lame. The flag_eof would have been set
            # in read_nonblocking(), so this should be safe.
            waitpid_options = 0
        else:
            waitpid_options = os.WNOHANG

        try:
            pid, status = os.waitpid(self.pid, waitpid_options)
        except OSError:
            err = sys.exc_info()[1]
            # No child processes
            if err.errno == errno.ECHILD:
                raise ExceptionPexpect('isalive() encountered condition ' +
                        'where "terminated" is 0, but there was no child ' +
                        'process. Did someone else call waitpid() ' +
                        'on our process?')
            else:
                raise err

        # I have to do this twice for Solaris.
        # I can't even believe that I figured this out...
        # If waitpid() returns 0 it means that no child process
        # wishes to report, and the value of status is undefined.
        if pid == 0:
            try:
                ### os.WNOHANG) # Solaris!
                pid, status = os.waitpid(self.pid, waitpid_options)
            except OSError as e:  # pragma: no cover
                # This should never happen...
                if e.errno == errno.ECHILD:
                    raise ExceptionPexpect('isalive() encountered condition ' +
                            'that should never happen. There was no child ' +
                            'process. Did someone else call waitpid() ' +
                            'on our process?')
                else:
                    raise

            # If pid is still 0 after two calls to waitpid() then the process
            # really is alive. This seems to work on all platforms, except for
            # Irix which seems to require a blocking call on waitpid or select,
            # so I let read_nonblocking take care of this situation
            # (unfortunately, this requires waiting through the timeout).
            if pid == 0:
                return True

        if pid == 0:
            return True

        if os.WIFEXITED(status):
            self.status = status
            self.exitstatus = os.WEXITSTATUS(status)
            self.signalstatus = None
            self.terminated = True
        elif os.WIFSIGNALED(status):
            self.status = status
            self.exitstatus = None
            self.signalstatus = os.WTERMSIG(status)
            self.terminated = True
        elif os.WIFSTOPPED(status):
            raise ExceptionPexpect('isalive() encountered condition ' +
                    'where child process is stopped. This is not ' +
                    'supported. Is some other process attempting ' +
                    'job control with our child pid?')
        return False

    def kill(self, sig):
        '''This sends the given signal to the child application. In keeping
        with UNIX tradition it has a misleading name. It does not necessarily
        kill the child unless you send the right signal. '''

        # Same as os.kill, but the pid is given for you.
        if self.isalive():
            os.kill(self.pid, sig)

    def getwinsize(self):
        '''This returns the terminal window size of the child tty. The return
        value is a tuple of (rows, cols). '''

        TIOCGWINSZ = getattr(termios, 'TIOCGWINSZ', 1074295912)
        s = struct.pack('HHHH', 0, 0, 0, 0)
        x = fcntl.ioctl(self.fd, TIOCGWINSZ, s)
        return struct.unpack('HHHH', x)[0:2]

    def setwinsize(self, rows, cols):
        '''This sets the terminal window size of the child tty. This will cause
        a SIGWINCH signal to be sent to the child. This does not change the
        physical window size. It changes the size reported to TTY-aware
        applications like vi or curses -- applications that respond to the
        SIGWINCH signal. '''

        # Some very old platforms have a bug that causes the value for
        # termios.TIOCSWINSZ to be truncated. There was a hack here to work
        # around this, but it caused problems with newer platforms so has been
        # removed. For details see https://github.com/pexpect/pexpect/issues/39
        TIOCSWINSZ = getattr(termios, 'TIOCSWINSZ', -2146929561)
        # Note, assume ws_xpixel and ws_ypixel are zero.
        s = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(self.fd, TIOCSWINSZ, s)

    def interact(self, escape_character=chr(29),
            input_filter=None, output_filter=None):
        '''This gives control of the child process to the interactive user (the
        human at the keyboard). Keystrokes are sent to the child process, and
        the stdout and stderr output of the child process is printed. This
        simply echos the child stdout and child stderr to the real stdout and
        it echos the real stdin to the child stdin. When the user types the
        escape_character this method will stop. The default for
        escape_character is ^]. This should not be confused with ASCII 27 --
        the ESC character. ASCII 29 was chosen for historical merit because
        this is the character used by 'telnet' as the escape character. The
        escape_character will not be sent to the child process.

        You may pass in optional input and output filter functions. These
        functions should take a string and return a string. The output_filter
        will be passed all the output from the child process. The input_filter
        will be passed all the keyboard input from the user. The input_filter
        is run BEFORE the check for the escape_character.

        Note that if you change the window size of the parent the SIGWINCH
        signal will not be passed through to the child. If you want the child
        window size to change when the parent's window size changes then do
        something like the following example::

            import pexpect, struct, fcntl, termios, signal, sys
            def sigwinch_passthrough (sig, data):
                s = struct.pack("HHHH", 0, 0, 0, 0)
                a = struct.unpack('hhhh', fcntl.ioctl(sys.stdout.fileno(),
                    termios.TIOCGWINSZ , s))
                global p
                p.setwinsize(a[0],a[1])
            # Note this 'p' global and used in sigwinch_passthrough.
            p = pexpect.spawn('/bin/bash')
            signal.signal(signal.SIGWINCH, sigwinch_passthrough)
            p.interact()
        '''

        # Flush the buffer.
        self.write_to_stdout(self.buffer)
        sys.stdout.flush()
        self.buffer = self.string_type()
        mode = tty.tcgetattr(STDIN_FILENO)
        tty.setraw(STDIN_FILENO)
        if PY3:
            escape_character = escape_character.encode('latin-1')
        try:
            self.__interact_copy(escape_character, input_filter, output_filter)
        finally:
            tty.tcsetattr(STDIN_FILENO, tty.TCSAFLUSH, mode)

    def __interact_writen(self, fd, data):
        '''This is used by the interact() method.
        '''

        while data != b'' and self.isalive():
            n = os.write(fd, data)
            data = data[n:]

    def __interact_read(self, fd):
        '''This is used by the interact() method.
        '''

        return os.read(fd, 1000)

    def __interact_copy(self, escape_character=None,
            input_filter=None, output_filter=None):

        '''This is used by the interact() method.
        '''

        while self.isalive():
            r, w, e = self.__select([self.child_fd, STDIN_FILENO], [], [])
            if self.child_fd in r:
                try:
                    data = self.__interact_read(self.child_fd)
                except OSError as err:
                    if err.args[0] == errno.EIO:
                        # Linux-style EOF
                        break
                    raise
                if data == b'':
                    # BSD-style EOF
                    break
                if output_filter:
                    data = output_filter(data)
                if self.logfile is not None:
                    self.logfile.write(data)
                    self.logfile.flush()
                os.write(STDOUT_FILENO, data)
            if STDIN_FILENO in r:
                data = self.__interact_read(STDIN_FILENO)
                if input_filter:
                    data = input_filter(data)
                i = data.rfind(escape_character)
                if i != -1:
                    data = data[:i]
                    self.__interact_writen(self.child_fd, data)
                    break
                self.__interact_writen(self.child_fd, data)

    def __select(self, iwtd, owtd, ewtd, timeout=None):

        '''This is a wrapper around select.select() that ignores signals. If
        select.select raises a select.error exception and errno is an EINTR
        error then it is ignored. Mainly this is used to ignore sigwinch
        (terminal resize). '''

        # if select() is interrupted by a signal (errno==EINTR) then
        # we loop back and enter the select() again.
        if timeout is not None:
            end_time = time.time() + timeout
        while True:
            try:
                return select.select(iwtd, owtd, ewtd, timeout)
            except select.error:
                err = sys.exc_info()[1]
                if err.args[0] == errno.EINTR:
                    # if we loop back we have to subtract the
                    # amount of time we already waited.
                    if timeout is not None:
                        timeout = end_time - time.time()
                        if timeout < 0:
                            return([], [], [])
                else:
                    # something else caused the select.error, so
                    # this actually is an exception.
                    raise

class PtyProcessUnicode(PtyProcess):
    def __init__(self, pid, fd, encoding='utf-8', codec_errors='strict',
                 newline=None):
        super(PtyProcessUnicode, self).__init__(pid, fd)
        self.encoding = encoding
        self.fileobj = io.TextIOWrapper(self.fileobj_bytes, encoding=encoding,
                                        errors=codec_errors, newline=newline)