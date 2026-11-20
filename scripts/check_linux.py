"""Run desktop input and Qt tests on an isolated X server, not the user's desktop."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import argparse

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--binary", type=Path)
parser.add_argument("--source-only", action="store_true")
parser.add_argument("--binary-only", action="store_true")
parser.add_argument("--browser", action="store_true", help="Run the development Chromium test as well")
parser.add_argument("--browser-only", action="store_true")
args = parser.parse_args()
xvfb = shutil.which("Xvfb") or str(root / "runtime/linux/usr/bin/Xvfb")
if not Path(xvfb).exists():
    raise SystemExit("请安装 xvfb 后重试；此脚本不修改当前桌面的输入。")
display = next(number for number in range(191, 240)
               if not Path(f"/tmp/.X11-unix/X{number}").exists()
               and not Path(f"/tmp/.X{number}-lock").exists())
env = os.environ.copy()
env["DISPLAY"] = f":{display}"
env["XDG_SESSION_TYPE"] = "x11"
env.pop("XAUTHORITY", None)
env["BRIOCHE_DESKTOP_TESTS"] = "1"
env["LD_LIBRARY_PATH"] = str(root / "runtime/linux/usr/lib/x86_64-linux-gnu") + (
    os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
artifacts = root / "artifacts"
artifacts.mkdir(exist_ok=True)
with (artifacts / "xvfb.log").open("w") as log:
    server = subprocess.Popen([xvfb, env["DISPLAY"], "-screen", "0", "2560x1440x24",
                               "-nolisten", "tcp", "-ac", "-noreset"], env=env,
                              stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("隔离测试显示器启动失败，见 artifacts/xvfb.log")
            if Path(f"/tmp/.X11-unix/X{display}").exists():
                break
            time.sleep(.05)
        checks = [("tests.txt", ["-m", "unittest", "discover", "-s", "tests", "-v"]),
                  ("gui-smoke.txt", ["tests/gui_smoke.py"])]
        if args.binary_only or args.browser_only:
            checks = []
        if args.browser or args.browser_only:
            checks.append(("browser-smoke-04.txt", ["tests/browser_smoke.py"]))
        if not args.source_only and not args.browser_only and (args.binary or root / "dist/Brioche/Brioche").exists():
            checks.append(("bundle-smoke.txt", ["scripts/verify_build.py", "--input-tests", "--web-tests"] +
                           (["--binary", str(args.binary)] if args.binary else [])))
        for filename, arguments in checks:
            with (artifacts / filename).open("w") as output:
                result = subprocess.run([sys.executable, *arguments], cwd=root, env=env,
                                        stdout=output, stderr=subprocess.STDOUT, timeout=90)
            print(f"{filename}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
            if result.returncode:
                raise SystemExit(result.returncode)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
