"""Build a small NVENC-capable FFmpeg from pinned upstream source, never a system install."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "ffmpeg": (
        "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
        "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e",
    ),
    "nvheaders": (
        "https://github.com/FFmpeg/nv-codec-headers/archive/refs/tags/n12.1.14.1.tar.gz",
        "f40c8a27417eb2688412da32985e382e811196311c4d103d1fcaafad2876cfc1",
    ),
}
parser = argparse.ArgumentParser()
parser.add_argument("--platform", choices=("linux", "windows"), required=True)
parser.add_argument("--work-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/media")
parser.add_argument("--source-archive", type=Path)
args = parser.parse_args()
work = args.work_dir.resolve()
work.mkdir(parents=True, exist_ok=True)
out = args.output_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
archives = {}
for name, (url, expected) in SOURCES.items():
    archive = work / (name + (".tar.xz" if name == "ffmpeg" else ".tar.gz"))
    if not archive.exists():
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            archive.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise SystemExit("Source checksum mismatch: " + name)
    archives[name] = archive
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise SystemExit("Unsafe source archive")
        tar.extractall(work)
source = work / "ffmpeg-9.0.2"
headers = work / "nv-codec-headers-n12.1.14.1"
prefix = work / "headers"
subprocess.run(["make", "install", "PREFIX=" + str(prefix)], cwd=headers, check=True)
env = os.environ.copy()
env["PKG_CONFIG_PATH"] = str(prefix / "lib/pkgconfig") + (
    os.pathsep + env["PKG_CONFIG_PATH"] if env.get("PKG_CONFIG_PATH") else ""
)
build = work / ("build-" + args.platform)
build.mkdir(exist_ok=True)
flags = [
    "--disable-autodetect",
    "--disable-everything",
    "--disable-doc",
    "--disable-debug",
    "--disable-ffplay",
    "--disable-ffprobe",
    "--disable-network",
    "--disable-shared",
    "--enable-static",
    "--enable-ffmpeg",
    "--enable-avcodec",
    "--enable-avformat",
    "--enable-avdevice",
    "--enable-avfilter",
    "--enable-swscale",
    "--enable-ffnvcodec",
    "--enable-nvenc",
    "--enable-encoder=h264_nvenc",
    "--enable-decoder=rawvideo,wrapped_avframe",
    "--enable-parser=h264",
    "--enable-muxer=nut,null",
    "--enable-protocol=file,pipe",
    "--enable-filter=scale,format,color,testsrc2,null",
]
if args.platform == "linux":
    flags += [
        "--enable-indev=xcbgrab,lavfi",
        "--enable-libxcb",
        "--enable-libxcb-shm",
        "--enable-libxcb-xfixes",
        "--enable-libxcb-shape",
    ]
else:
    flags += [
        "--enable-cross-compile",
        "--target-os=mingw32",
        "--arch=x86_64",
        "--cross-prefix=x86_64-w64-mingw32-",
        "--pkg-config=pkg-config",
        "--enable-indev=gdigrab,lavfi",
        "--extra-ldflags=-static -static-libgcc",
    ]
# Optional private development headers/tools; CI uses standard build packages.
if env.get("BRIOCHE_MEDIA_CFLAGS"):
    flags += ["--extra-cflags=" + env["BRIOCHE_MEDIA_CFLAGS"]]
if env.get("BRIOCHE_MEDIA_LDFLAGS") and args.platform == "linux":
    flags += ["--extra-ldflags=" + env["BRIOCHE_MEDIA_LDFLAGS"]]
subprocess.run([str(source / "configure"), *flags], cwd=build, env=env, check=True)
name = "ffmpeg.exe" if args.platform == "windows" else "ffmpeg"
subprocess.run(
    ["make", "-j", str(min(4, os.cpu_count() or 1)), name],
    cwd=build,
    env=env,
    check=True,
)
shutil.copy2(build / name, out / name)
notices = out / "licenses"
notices.mkdir(exist_ok=True)
for file in ("COPYING.LGPLv2.1", "COPYING.LGPLv3", "LICENSE.md"):
    if (source / file).is_file():
        shutil.copy2(source / file, notices / file)
shutil.copy2(
    headers / "include/ffnvcodec/nvEncodeAPI.h", notices / "nvEncodeAPI-headers.txt"
)
metadata = {
    "sources": {
        name: {"url": url, "sha256": digest} for name, (url, digest) in SOURCES.items()
    },
    "platform": args.platform,
    "configure": flags,
    "compiler": subprocess.check_output(
        ["x86_64-w64-mingw32-gcc" if args.platform == "windows" else "cc", "--version"],
        text=True,
    ).splitlines()[0],
}
(out / "BUILD.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
if args.source_archive:
    args.source_archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        args.source_archive, "w", compression=zipfile.ZIP_STORED
    ) as archive:
        for label, path in archives.items():
            archive.write(path, "sources/" + path.name)
        archive.write(Path(__file__), "build_media.py")
        archive.writestr("BUILD.json", json.dumps(metadata, indent=2))
print(out / name)
