"""Exclusive macOS/POSIX serial reader; pressure v2 explicitly enables bounded writes."""

import errno
import fcntl
import glob
import os
import select
import stat
import termios
import time


def list_serial_ports():
    """只列字符设备名称，不打开端口、不探测固件。"""
    result = []
    for name in sorted(glob.glob("/dev/cu.*")):
        try:
            if stat.S_ISCHR(os.stat(name).st_mode):
                result.append(name)
        except OSError:
            continue
    return result


class PosixSerialReader:
    def __init__(self, port, baudrate=9600, *, writable=False):
        if baudrate != 9600:
            raise ValueError("Arduino 温压协议均使用 9600 波特率")
        if not port.startswith("/dev/cu.") or not stat.S_ISCHR(os.stat(port).st_mode):
            raise ValueError("请选择 /dev/cu.* 串口字符设备")
        self.fd = None
        self.previous = None
        self.writable = writable
        fd = os.open(port, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # 提供同类接收程序互斥，TIOCEXCL 还阻止新的设备打开。
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            self.previous = termios.tcgetattr(fd)
            settings = termios.tcgetattr(fd)
            settings[0] = 0
            settings[1] = 0
            settings[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            settings[3] = 0
            settings[4] = settings[5] = termios.B9600
            settings[6][termios.VMIN] = 0
            settings[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, settings)
            self.fd = fd
        except Exception:
            os.close(fd)
            raise

    def read(self, timeout=0.1):
        if self.fd is None:
            raise OSError("串口已关闭")
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        try:
            data = os.read(self.fd, 8192)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            raise
        if not data:
            raise EOFError("串口断开或返回 EOF")
        return data

    def write(self, data, timeout=1.0):
        """Bounded command write on the same exclusively owned descriptor."""
        if not self.writable or self.fd is None:
            raise OSError("串口未启用命令发送或已关闭")
        if not isinstance(data, bytes) or len(data) > 128:
            raise ValueError("串口命令必须为不超过128字节的bytes")
        deadline = time.monotonic() + timeout
        remaining = memoryview(data)
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [self.fd], [], max(0, left))[1]:
                raise TimeoutError("串口命令发送超时")
            try:
                count = os.write(self.fd, remaining)
            except BlockingIOError:
                continue
            if count <= 0:
                raise OSError("串口命令发送中断")
            remaining = remaining[count:]
        return len(data)

    def close(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if self.previous is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, self.previous)
                except OSError:
                    # 已拔出的设备不能恢复设置，FD 仍必须关闭。
                    pass
        finally:
            os.close(fd)
