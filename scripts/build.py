"""Build on the target OS. No cross-compilation or automatic publication."""
from pathlib import Path
import subprocess
import sys
import os
import argparse

root = Path(__file__).resolve().parents[1]
helper = root / "runtime/network" / ("brioche-network.exe" if sys.platform == "win32" else "brioche-network")
if not helper.is_file():
    raise SystemExit("Build the embedded network module first: scripts/build_network.py")
media = root / "runtime/media" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
if not media.is_file():
    raise SystemExit("Build the media module first: scripts/build_media.py")
parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", type=Path, default=root / "dist")
parser.add_argument("--work-dir", type=Path, default=root / "build")
options = parser.parse_args()
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "Brioche", "--distpath", str(options.output_dir), "--workpath", str(options.work_dir), "--paths", str(root / "src"),
        "--collect-all", "av", "--collect-all", "mss",
        "--collect-data", "brioche",
        "--add-data", f"{root / 'assets'}{os.pathsep}assets",
        "--copy-metadata", "brioche-desktop",
        "--add-binary", f"{helper}{os.pathsep}network",
        "--add-binary", f"{media}{os.pathsep}media",
        "--add-data", f"{media.parent / 'licenses'}{os.pathsep}media/licenses",
        "--add-data", f"{media.parent / 'BUILD.json'}{os.pathsep}media"]
if (helper.parent / "licenses").is_dir():
    args += ["--add-data", f"{helper.parent / 'licenses'}{os.pathsep}network/licenses",
             "--add-data", f"{helper.parent / 'DEPENDENCIES.txt'}{os.pathsep}network"]
if sys.platform.startswith("linux"):
    library = root / "runtime/linux/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0"
    if library.exists():
        args += ["--add-binary", f"{library}:."]
    args += ["--hidden-import", "Xlib.ext.xtest"]
else:
    args += ["--windowed", "--collect-submodules", "pynput"]
args += [str(root / "scripts/entrypoint.py")]
subprocess.run(args, cwd=root, check=True)
