"""Build the native Rust `brioche-core` v2.0 streaming engine into runtime/core/."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
core_dir = root / "core"
exe_name = "brioche-core.exe" if sys.platform == "win32" else "brioche-core"
target_dir = root / "runtime" / "core"
target_dir.mkdir(parents=True, exist_ok=True)

cargo = shutil.which("cargo")
if not cargo:
    for candidate in (
        Path("/media/bread/Toast/.cargo/bin/cargo"),
        Path.home() / ".cargo" / "bin" / ("cargo.exe" if sys.platform == "win32" else "cargo"),
    ):
        if candidate.is_file():
            cargo = str(candidate)
            break
if not cargo:
    raise SystemExit("cargo not found; install Rust stable toolchain to build brioche-core")

env = os.environ.copy()
if Path("/media/bread/Toast/.rustup").is_dir() and "RUSTUP_HOME" not in env:
    env["RUSTUP_HOME"] = "/media/bread/Toast/.rustup"
if Path("/media/bread/Toast/.cargo").is_dir() and "CARGO_HOME" not in env:
    env["CARGO_HOME"] = "/media/bread/Toast/.cargo"

subprocess.run(
    [cargo, "build", "--release", "--bin", "brioche-core"],
    cwd=core_dir,
    env=env,
    check=True,
)
built = core_dir / "target" / "release" / exe_name
if not built.is_file():
    raise SystemExit(f"Expected built binary at {built}")
dst = target_dir / exe_name
shutil.copy2(built, dst)
if sys.platform != "win32":
    dst.chmod(0o755)
print(f"Built and staged: {dst}")
