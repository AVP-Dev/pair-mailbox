"""Linux seccomp filter for the critic's read-only workspace boundary."""
from __future__ import annotations

import errno
import os
import platform
import struct

_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_ALU_AND_K = 0x54
_BPF_RET_K = 0x06
_SECCOMP_ARCH_OFFSET = 4
_SECCOMP_NR_OFFSET = 0
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000


def hardlink_filter_program(machine: str | None = None) -> bytes:
    """Build cBPF that denies hardlink syscalls and io_uring escape routes."""
    machine = (machine or platform.machine()).lower()
    if machine in ("x86_64", "amd64"):
        arch, denied = 0xC000003E, (86, 265, 425, 426, 427)
    elif machine in ("aarch64", "arm64"):
        arch, denied = 0xC00000B7, (37, 425, 426, 427)
    else:
        raise ValueError(f"Linux critic sandbox has no syscall map for {machine!r}")

    instructions = [
        (_BPF_LD_W_ABS, 0, 0, _SECCOMP_ARCH_OFFSET),
        (_BPF_JMP_JEQ_K, 1, 0, arch),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, _SECCOMP_NR_OFFSET),
    ]
    if arch == 0xC000003E:
        instructions.append((_BPF_ALU_AND_K, 0, 0, 0x3FFFFFFF))  # mask x32 ABI bit
    for syscall_number in denied:
        instructions.extend((
            (_BPF_JMP_JEQ_K, 0, 1, syscall_number),
            (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM),
        ))
    instructions.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    return b"".join(struct.pack("=HBBI", *item) for item in instructions)


def hardlink_filter_fd() -> int:
    """Return an inheritable memfd for bwrap --seccomp; fail closed if absent."""
    if platform.system() != "Linux" or not hasattr(os, "memfd_create"):
        raise ValueError("critic launch refused: Linux seccomp filter fd is unavailable")
    try:
        fd = os.memfd_create("pair-critic-seccomp", flags=0)
    except OSError as exc:
        raise ValueError(f"critic launch refused: cannot create seccomp filter fd: {exc}") from exc
    try:
        data = hardlink_filter_program()
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("short write while creating Linux seccomp filter")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        return fd
    except (OSError, ValueError):
        os.close(fd)
        raise
