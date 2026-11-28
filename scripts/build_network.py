"""Download and verify the bundled EasyTier userspace P2P mesh binaries (no root/TUN needed)."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

EASYTIER_VERSION = "v2.6.4"
RELEASE_BASE = f"https://github.com/EasyTier/EasyTier/releases/download/{EASYTIER_VERSION}"

TARGETS = {
    ("linux", "x86_64"): (f"easytier-linux-x86_64-{EASYTIER_VERSION}.zip", False),
    ("linux", "arm64"): (f"easytier-linux-aarch64-{EASYTIER_VERSION}.zip", False),
    ("windows", "x86_64"): (f"easytier-windows-x86_64-{EASYTIER_VERSION}.zip", True),
    ("windows", "arm64"): (f"easytier-windows-arm64-{EASYTIER_VERSION}.zip", True),
    ("darwin", "arm64"): (f"easytier-macos-aarch64-{EASYTIER_VERSION}.zip", False),
    ("darwin", "x86_64"): (f"easytier-macos-x86_64-{EASYTIER_VERSION}.zip", False),
    ("android", "arm64"): (f"Easytier-Magisk-{EASYTIER_VERSION}.zip", False),
    ("android", "x86_64"): (f"easytier-linux-x86_64-{EASYTIER_VERSION}.zip", False),
}


def detect_target():
    sys_name = sys.platform
    if sys_name.startswith("linux"):
        os_key = "linux"
    elif sys_name == "win32":
        os_key = "windows"
    elif sys_name == "darwin":
        os_key = "darwin"
    else:
        raise SystemExit(f"Unsupported OS: {sys_name}")
    mach = platform.machine().lower()
    if mach in ("x86_64", "amd64"):
        arch_key = "x86_64"
    elif mach in ("aarch64", "arm64"):
        arch_key = "arm64"
    else:
        raise SystemExit(f"Unsupported architecture: {mach}")
    return os_key, arch_key


def download_file(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "Brioche-Build/1.4"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, 256 * 1024)


def extract_binaries(archive: Path, out_dir: Path, is_windows: bool, rename_so: bool = False):
    ext = ".exe" if is_windows else ""
    wanted = {f"easytier-core{ext}": None, f"easytier-cli{ext}": None}
    extra_dlls = {}
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            base = Path(member).name
            if member.endswith("/"):
                continue
            if base in wanted:
                wanted[base] = member
            elif is_windows and base.lower().endswith(".dll"):
                extra_dlls[base] = member
        missing = [k for k, v in wanted.items() if v is None]
        if missing:
            raise SystemExit(f"Missing {missing} in {archive.name}")
        out_dir.mkdir(parents=True, exist_ok=True)
        for base, member in {**wanted, **extra_dlls}.items():
            if rename_so:
                dst_name = "libeasytier_core.so" if "core" in base else "libeasytier_cli.so"
            else:
                dst_name = base
            dst = out_dir / dst_name
            tmp = out_dir / (dst_name + ".tmp")
            with zf.open(member) as src, tmp.open("wb") as out:
                shutil.copyfileobj(src, out, 256 * 1024)
            if not is_windows:
                tmp.chmod(0o755)
            tmp.replace(dst)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--os", dest="target_os", default=None)
    parser.add_argument("--arch", dest="target_arch", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--android-so", action="store_true", help="Rename outputs to libeasytier_*.so for Android jniLibs")
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args()

    default_os, default_arch = detect_target()
    target_os = (args.target_os or default_os).lower()
    target_arch = (args.target_arch or default_arch).lower()
    key = (target_os, target_arch)
    if key not in TARGETS:
        raise SystemExit(f"Unsupported EasyTier target: {key}")

    asset_name, is_windows = TARGETS[key]
    out_dir = args.output_dir or (root / "runtime/network")
    cache_dir = args.cache_dir or Path(tempfile.gettempdir()) / "brioche-easytier-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / asset_name

    if not archive.is_file() or archive.stat().st_size < 1_000_000:
        url = f"{RELEASE_BASE}/{asset_name}"
        print(f"Downloading EasyTier {EASYTIER_VERSION}: {url}")
        tmp_zip = archive.with_suffix(".zip.part")
        download_file(url, tmp_zip)
        tmp_zip.replace(archive)

    extract_binaries(archive, out_dir, is_windows=is_windows, rename_so=args.android_so)

    if not args.android_so:
        # Verify native binary executes cleanly when building for host OS/arch
        if (target_os, target_arch) == (default_os, default_arch):
            ext = ".exe" if is_windows else ""
            ver = subprocess.check_output([str(out_dir / f"easytier-core{ext}"), "--version"], text=True).strip()
            print(f"Verified bundled EasyTier: {ver}")
        # Write dependency metadata & LGPL-3.0 notice
        (out_dir / "DEPENDENCIES.txt").write_text(
            f"EasyTier {EASYTIER_VERSION}\nSource: https://github.com/EasyTier/EasyTier\nLicense: LGPL-3.0\nAsset: {asset_name}\n",
            encoding="utf-8",
        )
        lic_dir = out_dir / "licenses" / f"EasyTier@{EASYTIER_VERSION}"
        lic_dir.mkdir(parents=True, exist_ok=True)
        (lic_dir / "NOTICE.txt").write_text(
            f"Brioche bundles unmodified EasyTier {EASYTIER_VERSION} binaries (easytier-core, easytier-cli)\n"
            "from https://github.com/EasyTier/EasyTier under the GNU Lesser General Public License v3.0 (LGPL-3.0).\n",
            encoding="utf-8",
        )
    print(f"EasyTier ready in {out_dir}")


if __name__ == "__main__":
    main()
