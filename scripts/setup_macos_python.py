"""Pinned current Python security build, independent of the runner's macOS floor."""

import argparse
import hashlib
import os
from pathlib import Path
import tarfile
import urllib.request

SOURCES = {
    "x86_64": (
        "x86_64",
        "c2e4b51e535ead2e526b8974909a1b1e00fbc2ebe2281087b9b8b14dad0cad5d",
    ),
    "arm64": (
        "aarch64",
        "3afb0567b2a2b30b3a7341bd143cb4e8a69f3d8e95d4641218eaa45f0fabbd07",
    ),
}
parser = argparse.ArgumentParser()
parser.add_argument("--arch", choices=SOURCES, required=True)
parser.add_argument("--directory", type=Path, required=True)
args = parser.parse_args()
machine, digest = SOURCES[args.arch]
url = f"https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.11.18%2B20261110-{machine}-apple-darwin-install_only_stripped.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(data).hexdigest() == digest, "Python archive checksum mismatch"
args.directory.mkdir(parents=True, exist_ok=True)
archive = args.directory / "python.tar.gz"
archive.write_bytes(data)
with tarfile.open(archive) as tar:
    for item in tar.getmembers():
        assert not item.name.startswith("/") and ".." not in Path(item.name).parts
        if item.issym():
            assert (
                not item.linkname.startswith("/")
                and ".." not in Path(item.linkname).parts
            )
    tar.extractall(args.directory)
python = args.directory / "python/bin/python3"
print(python)
if os.environ.get("GITHUB_PATH"):
    with open(os.environ["GITHUB_PATH"], "a") as output:
        output.write(str(python.parent) + "\n")
