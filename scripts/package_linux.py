"""Create a local Ubuntu amd64 .deb from a tested PyInstaller directory."""
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import runpy
import argparse

root = Path(__file__).resolve().parents[1]
version = runpy.run_path(str(root / "src/brioche/__init__.py"))["__version__"]
if platform.system() != "Linux" or platform.machine() != "x86_64":
    raise SystemExit("此打包脚本仅适用于 Linux x86_64")
parser = argparse.ArgumentParser()
parser.add_argument("--binary-dir", type=Path, default=root / "dist/Brioche")
parser.add_argument("--output-dir", type=Path, default=root / "releases")
args = parser.parse_args()
binary_dir = args.binary_dir
if not (binary_dir / "Brioche").exists():
    raise SystemExit("先运行 scripts/build.py")

from package_notices import collect_notices
collect_notices(root, binary_dir)

releases = args.output_dir
releases.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="brioche-deb-") as scratch:
    package = Path(scratch)
    control = package / "DEBIAN"
    control.mkdir()
    (control / "control").write_text(
        f"Package: brioche\nVersion: {version}\nArchitecture: amd64\n"
        "Maintainer: Brioche Project\nSection: net\nPriority: optional\n"
        "Depends: libc6 (>= 2.35), libgl1, libegl1, libx11-6, libfontconfig1, libglib2.0-0\n"
        "Description: Brioche encrypted remote desktop development prototype\n"
        " H.264 desktop sharing and keyboard/mouse control for logged-in Xorg sessions.\n",
        encoding="utf-8")
    shutil.copytree(binary_dir, package / "opt/brioche", symlinks=True)
    launcher = package / "usr/bin/brioche"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nexec /opt/brioche/Brioche "$@"\n')
    launcher.chmod(0o755)
    desktop = package / "usr/share/applications/brioche.desktop"
    desktop.parent.mkdir(parents=True)
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Brioche\n"
                       "Comment=Encrypted remote desktop\nExec=brioche\nIcon=brioche\n"
                       "Terminal=false\nCategories=Network;RemoteAccess;\n")
    icon = package / "usr/share/icons/hicolor/scalable/apps/brioche.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(root / "assets/brioche.svg", icon)
    output = releases / f"brioche_{version}_amd64.deb"
    subprocess.run(["dpkg-deb", "-Zzstd", "-z6", "--root-owner-group", "--build", str(package), str(output)], check=True)
    print(output)
