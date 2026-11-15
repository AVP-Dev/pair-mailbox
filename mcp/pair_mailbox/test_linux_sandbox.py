"""Linux seccomp policy tests for the critic's read-only workspace."""
from __future__ import annotations

import struct
import unittest

import linux_sandbox


class TestHardlinkFilter(unittest.TestCase):
    def test_x86_64_filter_denies_link_linkat_and_io_uring(self) -> None:
        program = linux_sandbox.hardlink_filter_program("x86_64")
        instructions = [struct.unpack("=HBBI", program[i:i + 8])
                        for i in range(0, len(program), 8)]
        self.assertEqual(0, len(program) % 8)
        self.assertIn((linux_sandbox._BPF_JMP_JEQ_K, 0, 1, 86), instructions)
        self.assertIn((linux_sandbox._BPF_JMP_JEQ_K, 0, 1, 265), instructions)
        for syscall_number in (425, 426, 427):
            self.assertIn((linux_sandbox._BPF_JMP_JEQ_K, 0, 1, syscall_number), instructions)
        self.assertIn((linux_sandbox._BPF_RET_K, 0, 0,
                       linux_sandbox._SECCOMP_RET_ERRNO | 1), instructions)
        self.assertEqual((linux_sandbox._BPF_ALU_AND_K, 0, 0, 0x3FFFFFFF), instructions[4])

    def test_aarch64_filter_uses_its_linkat_number(self) -> None:
        program = linux_sandbox.hardlink_filter_program("aarch64")
        instructions = [struct.unpack("=HBBI", program[i:i + 8])
                        for i in range(0, len(program), 8)]
        self.assertIn((linux_sandbox._BPF_JMP_JEQ_K, 0, 1, 37), instructions)
        self.assertNotIn((linux_sandbox._BPF_JMP_JEQ_K, 0, 1, 86), instructions)

    def test_unsupported_architecture_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "no syscall map"):
            linux_sandbox.hardlink_filter_program("mips64")


if __name__ == "__main__":
    unittest.main(verbosity=2)
