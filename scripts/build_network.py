"""Build the bundled userspace Tailscale helper on the target platform."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--go', default=shutil.which('go'))
args = parser.parse_args()
if not args.go:
    raise SystemExit('Install Go 1.27.4 or newer, or provide --go /path/to/go')
macos12 = sys.platform == 'darwin'
module_flags = ['-modfile=go.macos12.mod'] if macos12 else []
if macos12:
    import platform
    if tuple(map(int, platform.mac_ver()[0].split('.')[:1])) < (12,):
        raise SystemExit('macOS 12 or newer is required')
    version = subprocess.check_output([args.go, 'version'], text=True)
    if 'go1.26.' not in version:
        raise SystemExit('macOS 12 compatibility requires Go 1.26.11 (not Go 1.27). Use --go to select the compatible toolchain.')
output = root / 'runtime/network' / ('brioche-network.exe' if sys.platform == 'win32' else 'brioche-network')
output.parent.mkdir(parents=True, exist_ok=True)
env = os.environ.copy()
env['CGO_ENABLED'] = '0'
env['GOTOOLCHAIN'] = 'local'
subprocess.run([args.go, 'test', *module_flags, '-mod=readonly', '-timeout', '100s', './...'], cwd=root/'network-helper', env=env, check=True)
subprocess.run([args.go, 'build', *module_flags, '-mod=readonly', '-trimpath', '-ldflags=-s -w', '-o', str(output), '.'], cwd=root/'network-helper', env=env, check=True)
print(output)
# Record notices for the modules actually linked into this helper.
notice = output.parent / 'licenses'
notice.mkdir(exist_ok=True)
metadata = subprocess.check_output([args.go, 'version', '-m', str(output)], text=True)
(output.parent/'DEPENDENCIES.txt').write_text(metadata, encoding='utf-8')
cache = Path(subprocess.check_output([args.go, 'env', 'GOMODCACHE'], text=True).strip())
for line in metadata.splitlines():
    fields=line.split()
    if len(fields)<3 or fields[0]!='dep':
        continue
    module, version=fields[1:3]
    escaped=''.join('!'+c.lower() if c.isupper() else c for c in module)
    source=cache / (escaped+'@'+version)
    target=notice / (module.replace('/','_')+'@'+version)
    for candidate in source.iterdir() if source.is_dir() else []:
        if candidate.is_file() and candidate.name.lower().startswith(('license','copying','copyright','notice')):
            target.mkdir(parents=True,exist_ok=True)
            destination=target/candidate.name
            temporary=target/(candidate.name+".tmp")
            shutil.copy2(candidate,temporary)
            temporary.replace(destination)
