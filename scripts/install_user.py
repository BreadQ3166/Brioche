"""Install the built DEB into this user's application directory without sudo."""
import hashlib
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import runpy
import argparse
import os

root = Path(__file__).resolve().parents[1]
version = runpy.run_path(str(root / "src/brioche/__init__.py"))["__version__"]
parser = argparse.ArgumentParser()
parser.add_argument("--bundle", type=Path, help="Install a locally built and verified onedir bundle instead of a DEB")
parser.add_argument("--reuse-libraries", action="store_true", help="Hardlink byte-identical immutable libraries from the managed previous version")
args = parser.parse_args()
package = root / f"releases/brioche_{version}_amd64.deb"
if args.bundle:
    if not (args.bundle / "Brioche").is_file() or not (args.bundle / "_internal/network/brioche-network").is_file():
        raise SystemExit("本地构建目录不完整")
else:
    expected = next(line.split()[0] for line in (root / "releases/SHA256SUMS").read_text().splitlines()
                    if line.endswith(package.name))
    if hashlib.sha256(package.read_bytes()).hexdigest() != expected:
        raise SystemExit("安装包校验失败")
local = Path.home() / ".local"
parent = local / "opt/brioche"
target = parent / version
launcher = local / "bin/brioche"
desktop = local / "share/applications/brioche.desktop"
icon = local / "share/icons/hicolor/scalable/apps/brioche.svg"
if target.exists():
    raise SystemExit(f"该版本已存在：{target}")
previous = None
if launcher.exists():
    # Upgrade only launchers previously created by this installer; retain old binaries.
    lines = launcher.read_text().splitlines()
    command = shlex.split(lines[1]) if len(lines) == 2 and lines[0] == "#!/bin/sh" else []
    if (len(command) != 3 or command[0] != "exec" or command[2] != "$@"
            or Path(command[1]).name != "Brioche"
            or Path(command[1]).parent.parent != parent):
        raise SystemExit("已有非 Brioche 管理的启动入口，停止覆盖")
    previous = Path(command[1]).parent
    if desktop.exists() and f'Exec="{launcher}"' not in desktop.read_text():
        raise SystemExit("已有其他应用的同名菜单入口，停止覆盖")
elif desktop.exists() or icon.exists():
    raise SystemExit("已有同名菜单或图标，停止覆盖")
parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix=".install-", dir=parent) as staging:
    staging = Path(staging)
    if args.bundle:
        source = args.bundle.resolve()
        def copy_file(src, dst):
            relative = Path(src).relative_to(source)
            old = previous / relative if previous else None
            # Application state always lives outside the installation tree.
            # Only share immutable native libraries, never code/configuration.
            immutable = "_internal" in relative.parts and (".so" in relative.name or relative.suffix in (".dll", ".dylib"))
            if args.reuse_libraries and immutable and old and old.is_file() and not old.is_symlink():
                if Path(src).stat().st_mode & 0o777 == old.stat().st_mode & 0o777 and Path(src).stat().st_size == old.stat().st_size and hashlib.sha256(Path(src).read_bytes()).digest() == hashlib.sha256(old.read_bytes()).digest():
                    try:
                        os.link(old, dst)
                        return dst
                    except OSError:
                        pass
            return shutil.copy2(src, dst)
        shutil.copytree(source, staging / "bundle", symlinks=True, copy_function=copy_file)
        (staging / "bundle").rename(target)
        icon_source = root / "assets/brioche.svg"
    else:
        subprocess.run(["dpkg-deb", "--extract", str(package), str(staging)], check=True)
        shutil.move(str(staging / "opt/brioche"), str(target))
        icon_source = staging / "usr/share/icons/hicolor/scalable/apps/brioche.svg"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icon_source, icon)
launcher.parent.mkdir(parents=True, exist_ok=True)
temporary_launcher = launcher.with_suffix(".new")
temporary_launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(str(target / "Brioche"))} "$@"\n')
temporary_launcher.chmod(0o755)
temporary_launcher.replace(launcher)
desktop.parent.mkdir(parents=True, exist_ok=True)
desktop.write_text('[Desktop Entry]\nType=Application\nName=Brioche\n'
                   'Name[zh_CN]=Brioche 远程桌面\nComment=Encrypted remote desktop\n'
                   f'Exec="{launcher}"\nIcon={icon}\nTerminal=false\n'
                   'Categories=Network;RemoteAccess;\nStartupWMClass=Brioche\n', encoding="utf-8")
updater = shutil.which("update-desktop-database")
if updater:
    subprocess.run([updater, str(desktop.parent)], check=True)
print(f"Installed: {target}")
print(f"Launcher: {launcher}")
print(f"Desktop entry: {desktop}")
