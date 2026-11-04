#!/usr/bin/env python3
"""Run a command on a pseudo-terminal, feed it keystrokes, report its exit code.

usage: pty_run.py KEYS_FILE -- CMD [ARG...]
KEYS_FILE lines (python-escaped, e.g. 'hello\\r', '\\x03'); one line is sent per step,
after the child has gone quiet for a moment. Child output goes to stdout; the child's
exit code becomes ours (128+N for a signal death, as a shell would report it).
"""
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time


def main():
    keys_file = sys.argv[1]
    cmd = sys.argv[sys.argv.index("--") + 1 :]
    with open(keys_file, encoding="utf-8") as fh:
        keys = [line.rstrip("\n").encode().decode("unicode_escape").encode("latin-1") for line in fh if line.strip("\n")]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    out = b""
    sent = 0
    last = time.time()
    deadline = time.time() + 60
    status = None
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.15)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if data:
                out += data
                last = time.time()
                continue
        done, st = os.waitpid(pid, os.WNOHANG)
        if done:
            status = st
            break
        if sent < len(keys) and time.time() - last > 0.4:
            os.write(fd, keys[sent])
            sent += 1
            last = time.time()
    if status is None:
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        sys.stderr.write("pty_run: timeout\n")
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    code = os.waitstatus_to_exitcode(status)
    sys.exit(code if code >= 0 else 128 - code)


if __name__ == "__main__":
    main()
