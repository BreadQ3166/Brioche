"""Create a portable Windows ZIP containing Brioche.exe and all runtime files."""
import argparse
from pathlib import Path
import platform
import runpy
import zipfile
from package_notices import collect_notices

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--binary-dir', type=Path, default=root / 'dist/Brioche')
parser.add_argument('--output-dir', type=Path, default=root / 'releases')
args = parser.parse_args()
if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
    raise SystemExit('Build this portable package on Windows x64')
for path in ('Brioche.exe', '_internal/network/brioche-network.exe'):
    if not (args.binary_dir / path).is_file():
        raise SystemExit('Incomplete Windows bundle; build it first')
version = runpy.run_path(str(root / 'src/brioche/__init__.py'))['__version__']
collect_notices(root, args.binary_dir)
args.output_dir.mkdir(parents=True, exist_ok=True)
output = args.output_dir / f'Brioche-{version}-windows-x64.zip'
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in sorted(args.binary_dir.rglob('*')):
        if path.is_file():
            archive.write(path, Path('Brioche') / path.relative_to(args.binary_dir))
print(output)
