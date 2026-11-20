"""Inspect every bundled Mach-O slice. Never rewrite a binary's minimum OS."""

import argparse
import json
from pathlib import Path
import struct

CPU = {"x86_64": 0x1000007, "arm64": 0x100000C}


def version(value):
    return (value >> 16, (value >> 8) & 255, value & 255)


def slices(data):
    magic = data[:4]
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        count = struct.unpack_from(">I", data, 4)[0]
        if not 1 <= count <= 16:
            raise ValueError("Invalid fat Mach-O")
        offset = 8
        for _ in range(count):
            if magic == b"\xca\xfe\xba\xbf":
                cpu, _, start, size, _, _ = struct.unpack_from(">IIQQII", data, offset)
                offset += 32
            else:
                cpu, _, start, size, _ = struct.unpack_from(">IIIII", data, offset)
                offset += 20
            if start + size > len(data):
                raise ValueError("Truncated fat Mach-O")
            yield cpu, data[start : start + size]
    elif magic in (
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
    ):
        endian = "<" if magic[0] in (0xCF, 0xCE) else ">"
        yield struct.unpack_from(endian + "I", data, 4)[0], data


def inspect_slice(data):
    endian = "<" if data[0] in (0xCF, 0xCE) else ">"
    is64 = data[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
    commands, command_bytes = struct.unpack_from(endian + "II", data, 16)
    offset = 32 if is64 else 28
    if commands > 8192 or offset + command_bytes > len(data):
        raise ValueError("Invalid Mach-O commands")
    minimum, dependencies = None, []
    for _ in range(commands):
        cmd, size = struct.unpack_from(endian + "II", data, offset)
        if size < 8 or offset + size > len(data):
            raise ValueError("Truncated Mach-O command")
        if cmd == 0x32:
            platform, minos = struct.unpack_from(endian + "II", data, offset + 8)
            if platform != 1:
                raise ValueError("Binary targets a non-macOS platform")
            minimum = version(minos)
        elif cmd == 0x24:
            minimum = version(struct.unpack_from(endian + "I", data, offset + 8)[0])
        elif cmd in (0xC, 0x80000018, 0x8000001F, 0x80000023):
            name = struct.unpack_from(endian + "I", data, offset + 8)[0]
            if not 0 < name < size:
                raise ValueError("Invalid dependency name")
            dependencies.append(
                data[offset + name : offset + size].split(b"\0")[0].decode("utf-8")
            )
        offset += size
    if minimum is None:
        raise ValueError("No minimum macOS version recorded")
    return minimum, dependencies


def audit(root, architecture, maximum=(12, 0, 0)):
    records, failures = [], []
    paths = (
        [root]
        if root.is_file()
        else sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    )
    for path in paths:
        with path.open("rb") as file:
            magic = file.read(4)
        if magic not in (
            b"\xca\xfe\xba\xbe",
            b"\xca\xfe\xba\xbf",
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xce",
        ):
            continue
        label = path.name if root.is_file() else str(path.relative_to(root))
        try:
            matches = [
                content
                for cpu, content in slices(path.read_bytes())
                if cpu == CPU[architecture]
            ]
            if not matches:
                raise ValueError("Required architecture missing")
            for content in matches:
                minimum, dependencies = inspect_slice(content)
                if minimum > maximum:
                    raise ValueError("Requires macOS " + ".".join(map(str, minimum)))
                bad = [
                    name
                    for name in dependencies
                    if name.startswith("/")
                    and not name.startswith(("/usr/lib/", "/System/Library/"))
                ]
                if bad:
                    raise ValueError("Unbundled absolute dependency: " + bad[0])
                records.append({"file": label, "minimum": ".".join(map(str, minimum))})
        except (ValueError, struct.error) as exc:
            failures.append({"file": label, "error": str(exc)})
    return {
        "pass": bool(records) and not failures,
        "architecture": architecture,
        "maximum": "12.0",
        "scope": "binary deployment targets, architecture and dependency paths; not an OS12 runtime or permission acceptance test",
        "binaries": records,
        "failures": failures,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--arch", choices=CPU, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.path, args.arch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "pass": result["pass"],
                "binaries": len(result["binaries"]),
                "failures": result["failures"],
            }
        )
    )
    raise SystemExit(0 if result["pass"] else 1)
