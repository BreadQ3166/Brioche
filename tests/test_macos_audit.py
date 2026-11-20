from pathlib import Path
import struct
import tempfile
import unittest

from scripts.check_macos import audit, inspect_slice, slices


def macho(major=12, cpu=0x1000007, dependency=None):
    commands = struct.pack("<IIIIII", 0x32, 24, 1, major << 16, 15 << 16, 0)
    count = 1
    if dependency:
        name = dependency.encode() + b"\0"
        size = (24 + len(name) + 7) // 8 * 8
        commands += (
            struct.pack("<IIIIII", 0xC, size, 24, 0, 0, 0)
            + name
            + b"\0" * (size - 24 - len(name))
        )
        count += 1
    return (
        struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 3, 2, count, len(commands), 0, 0)
        + commands
    )


class MacOSAuditTests(unittest.TestCase):
    def check(self, data, arch="x86_64"):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "application"
            p.write_bytes(data)
            return audit(Path(folder), arch)

    def test_accepts_macos12_and_rejects_higher_or_wrong_arch(self):
        self.assertTrue(self.check(macho())["pass"])
        self.assertFalse(self.check(macho(13))["pass"])
        self.assertFalse(self.check(macho(cpu=0x100000C))["pass"])

    def test_unbundled_homebrew_dependency_is_rejected(self):
        self.assertFalse(
            self.check(macho(dependency="/usr/local/opt/openssl/lib/libssl.3.dylib"))[
                "pass"
            ]
        )
        self.assertTrue(
            self.check(macho(dependency="@loader_path/libssl.3.dylib"))["pass"]
        )

    def test_fat_binary_checks_the_requested_slice(self):
        first, second = macho(11), macho(13, cpu=0x100000C)
        header = struct.pack(">II", 0xCAFEBABE, 2)
        header += struct.pack(">IIIII", 0x1000007, 3, 48, len(first), 0)
        header += struct.pack(">IIIII", 0x100000C, 0, 48 + len(first), len(second), 0)
        data = header + first + second
        self.assertTrue(self.check(data, "x86_64")["pass"])
        self.assertFalse(self.check(data, "arm64")["pass"])
