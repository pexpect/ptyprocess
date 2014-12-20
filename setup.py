#!/usr/bin/python3

from distutils.core import setup

with open('README.rst') as f:
    readme = f.read()

setup(name='ptyprocess',
      version='0.4',
      description="Run a subprocess in a pseudo terminal",
      long_description=readme,
      author='Thomas Kluyver',
      author_email="thomas@kluyver.me.uk",
      url="https://github.com/pexpect/ptyprocess",
      packages=['ptyprocess'],
      classifiers = [
        'Development Status :: 5 - Production/Stable',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: ISC License (ISCL)',
        'Operating System :: POSIX',
        'Operating System :: MacOS :: MacOS X',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Topic :: Terminals',
    ],
)