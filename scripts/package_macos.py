"""Package a checked macOS 12 app. No Gatekeeper bypass or silent permissions."""
import argparse
from pathlib import Path
import platform
import runpy
import subprocess
from package_notices import collect_notices

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--app',type=Path,default=root/'dist/Brioche.app')
parser.add_argument('--output-dir',type=Path,default=root/'releases');args=parser.parse_args()
assert platform.system()=='Darwin'
version=runpy.run_path(str(root/'src/brioche/__init__.py'))['__version__']
architecture=platform.machine()
assert architecture in ('x86_64','arm64')
subprocess.run(['python3',str(root/'scripts/check_macos.py'),str(args.app),'--arch',architecture,
                '--output',str(root/'artifacts/macos12-audit.json')],check=True)
notices=args.app/'Contents/Resources'
collect_notices(root,notices)
subprocess.run(['codesign','--force','--deep','--sign','-',str(args.app)],check=True)
subprocess.run(['codesign','--verify','--deep','--strict',str(args.app)],check=True)
args.output_dir.mkdir(exist_ok=True,parents=True)
archive=args.output_dir/f'Brioche-{version}-macOS12-{architecture}.zip'
subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(args.app),str(archive)],check=True)
print(archive)
