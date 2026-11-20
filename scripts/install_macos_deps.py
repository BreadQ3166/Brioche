"""Resolve wheels for macOS 12 even when building on a newer hosted runner."""
import argparse
from pathlib import Path
import platform
import subprocess
import sys
import tomllib
from packaging.requirements import Requirement

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--wheelhouse',type=Path,required=True)
args=parser.parse_args();args.wheelhouse.mkdir(parents=True,exist_ok=True)
assert sys.platform=='darwin' and platform.machine() in ('x86_64','arm64')
requirements=tomllib.loads((root/'pyproject.toml').read_text())['project']['dependencies']
chosen=[str(Requirement(value)) for value in requirements
        if Requirement(value).marker is None or Requirement(value).marker.evaluate()]
subprocess.run([sys.executable,'-m','pip','download','--only-binary=:all:',
    '--platform','macosx_12_0_'+platform.machine(),'--python-version','3.11','--implementation','cp',
    '--abi','cp311','--abi','abi3','--dest',str(args.wheelhouse),*chosen,'pyinstaller==6.11.1'],check=True)
subprocess.run([sys.executable,'-m','pip','install','--no-index','--no-build-isolation',
    '--find-links',str(args.wheelhouse),'-e',str(root),'pyinstaller==6.11.1'],check=True)
