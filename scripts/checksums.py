"""Hash downloadable release packages; no credentials or user state included."""
import argparse
import hashlib
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('directory', nargs='?', type=Path, default=Path('releases'))
args = parser.parse_args()
paths = sorted(p for p in args.directory.iterdir() if p.suffix in ('.zip', '.deb', '.exe', '.apk'))
if not paths:
    raise SystemExit('No packages found')
lines = []
for path in paths:
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    lines.append(f'{digest.hexdigest()}  {path.name}\n')
(args.directory / 'SHA256SUMS').write_text(''.join(lines), encoding='utf-8')
