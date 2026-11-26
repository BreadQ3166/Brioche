"""Build (and optionally test) the native Rust `brioche-core` v2.0 engine into runtime/core/."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--test", action="store_true", help="Run Rust workspace tests before staging release binary")
args = parser.parse_args()

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
if sys.platform == "darwin":
    env.setdefault("MACOSX_DEPLOYMENT_TARGET", "12.0")
    existing_flags = env.get("RUSTFLAGS", "").strip()
    min_flag = "-C link-arg=-mmacosx-version-min=12.0"
    if min_flag not in existing_flags:
        env["RUSTFLAGS"] = f"{existing_flags} {min_flag}".strip()

if args.test:
    subprocess.run(
        [cargo, "test", "--workspace"],
        cwd=core_dir,
        env=env,
        check=True,
    )

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
