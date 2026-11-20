"""Shared notice collection for Windows and Linux bundles."""
import importlib.metadata
from pathlib import Path
import shutil


def collect_notices(root, binary_dir):
    notices = binary_dir / 'licenses'
    notices.mkdir(exist_ok=True)
    for dist in importlib.metadata.distributions():
        label = dist.metadata['Name']
        if not label:
            continue
        for item in dist.files or []:
            if any(word in str(item).lower() for word in ('license', 'copying', 'copyright')):
                source = Path(dist.locate_file(item))
                if source.is_file() and source.suffix.lower() not in ('.py', '.pyc', '.so', '.dll', '.pyd'):
                    target = notices / label / str(item).replace('..', '_')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + '.tmp')
                    shutil.copy2(source, temporary)
                    temporary.replace(target)
    (binary_dir / 'DEPENDENCIES.txt').write_text('\n'.join(sorted(
        f"{dist.metadata['Name']}=={dist.version}" for dist in importlib.metadata.distributions()
        if dist.metadata['Name'])) + '\n', encoding='utf-8')
    for name in ('README.md', 'THIRD_PARTY.md'):
        shutil.copy2(root / name, binary_dir / name)
    xcb = root / 'runtime/linux/usr/share/doc/libxcb-cursor0/copyright'
    if xcb.is_file():
        shutil.copy2(xcb, notices / 'libxcb-cursor0-copyright')
