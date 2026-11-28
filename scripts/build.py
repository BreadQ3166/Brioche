"""Build on the target OS. No cross-compilation or automatic publication."""
from pathlib import Path
import subprocess
import sys
import os
import argparse

root = Path(__file__).resolve().parents[1]
ext = ".exe" if sys.platform == "win32" else ""
et_core = root / "runtime/network" / f"easytier-core{ext}"
et_cli = root / "runtime/network" / f"easytier-cli{ext}"
if not et_core.is_file() or not et_cli.is_file():
    raise SystemExit("Build the embedded network module first: scripts/build_network.py")
media = root / "runtime/media" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
if sys.platform != 'darwin' and not media.is_file():
    raise SystemExit("Build the media module first: scripts/build_media.py")
parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", type=Path, default=root / "dist")
parser.add_argument("--work-dir", type=Path, default=root / "build")
options = parser.parse_args()
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "Brioche", "--distpath", str(options.output_dir), "--workpath", str(options.work_dir), "--paths", str(root / "src"),
        "--collect-all", "av",
        "--collect-data", "brioche",
        "--hidden-import", "PySide6.QtOpenGL",
        "--hidden-import", "PySide6.QtOpenGLWidgets",
        "--add-data", f"{root / 'assets'}{os.pathsep}assets",
        "--copy-metadata", "brioche-desktop",
        "--add-binary", f"{et_core}{os.pathsep}network",
        "--add-binary", f"{et_cli}{os.pathsep}network"]
for dll in sorted(et_core.parent.glob("*.dll")):
    args += ["--add-binary", f"{dll}{os.pathsep}network"]
if sys.platform == 'darwin':
    args += ['--additional-hooks-dir',str(root/'scripts/macos_hooks')]
else:
    args += ['--collect-all','mss']
if sys.platform != 'darwin':
    args += ["--add-binary", f"{media}{os.pathsep}media",
             "--add-data", f"{media.parent / 'licenses'}{os.pathsep}media/licenses",
             "--add-data", f"{media.parent / 'BUILD.json'}{os.pathsep}media"]
core_bin = root / "runtime/core" / ("brioche-core.exe" if sys.platform == "win32" else "brioche-core")
if core_bin.is_file():
    args += ["--add-binary", f"{core_bin}{os.pathsep}core"]
if (et_core.parent / "licenses").is_dir():
    args += ["--add-data", f"{et_core.parent / 'licenses'}{os.pathsep}network/licenses",
             "--add-data", f"{et_core.parent / 'DEPENDENCIES.txt'}{os.pathsep}network"]
if sys.platform.startswith("linux"):
    library = root / "runtime/linux/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0"
    if library.exists():
        args += ["--add-binary", f"{library}:."]
    args += ["--hidden-import", "Xlib.ext.xtest"]
else:
    args += ["--windowed"]
    if sys.platform == 'win32':args += ['--collect-submodules','pynput']
if sys.platform == 'darwin':
    args += ['--osx-bundle-identifier','com.brioche.desktop','--codesign-identity','-']
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtCore import Qt
    icons=options.work_dir/'brioche.iconset';icons.mkdir(parents=True,exist_ok=True)
    svg=QSvgRenderer(str(root/'assets/brioche.svg'))
    for size in (16,32,128,256,512):
        for scale in (1,2):
            image=QImage(size*scale,size*scale,QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent);painter=QPainter(image);svg.render(painter);painter.end()
            image.save(str(icons/f'icon_{size}x{size}{"@2x" if scale==2 else ""}.png'))
    icon=options.work_dir/'brioche.icns'
    subprocess.run(['iconutil','-c','icns',str(icons),'-o',str(icon)],check=True)
    args += ['--icon',str(icon)]
args += [str(root / "scripts/entrypoint.py")]
subprocess.run(args, cwd=root, check=True)
if sys.platform == 'darwin':
    import plistlib
    from brioche import __version__
    app=options.output_dir/'Brioche.app'
    info=app/'Contents/Info.plist'
    value=plistlib.loads(info.read_bytes())
    value.update(LSMinimumSystemVersion='12.0',NSHighResolutionCapable=True,
        CFBundleShortVersionString=__version__,CFBundleVersion=__version__,
        NSScreenCaptureUsageDescription='Brioche 需要屏幕录制权限来共享你选择的显示器。',
        NSDownloadsFolderUsageDescription='Brioche 将你确认接收的文件保存到所选目录。',
        NSDocumentsFolderUsageDescription='Brioche 仅传输你选择或确认的文件。',
        NSDesktopFolderUsageDescription='Brioche 仅传输你选择或确认的文件。')
    info.write_bytes(plistlib.dumps(value))
    subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True)
