"""Create a portable, credential-free distribution from an explicit allowlist."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import zipfile

VERSION = '0.2.0'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist', type=Path, default=Path(__file__).parent / 'dist')
    args = parser.parse_args()
    app = Path(__file__).parent
    target = args.dist
    licenses = target / 'licenses'
    licenses.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    distribution = importlib.metadata.distribution('pyinstaller')
    sources = {
        'PYTHON-LICENSE.txt': base / 'LICENSE.txt',
        'TCL-TK-LICENSE.txt': base / 'tcl' / 'tk8.6' / 'license.terms',
        'PYINSTALLER-COPYING.txt': Path(distribution.locate_file('pyinstaller-' + distribution.version + '.dist-info/licenses/COPYING.txt')),
    }
    for name, path in sources.items():
        if not path.is_file():
            raise RuntimeError('Required redistribution notice missing: ' + name)
        shutil.copyfile(path, licenses / name)
    members = ['YandexDiskRescue.exe', 'YandexDiskRescueMCP.exe', 'README.md', 'LICENSE', 'THIRD_PARTY.md', 'PUBLIC_REVIEW.md', 'MCP.md', 'LIVE_VALIDATION.md']
    for name in members[2:]:
        shutil.copyfile(app / name, target / name)
    exe = target / members[0]
    manifest = {'version': VERSION, 'stage': 'prerelease', 'live_api_validated': False,
                'executable': {'name': exe.name, 'bytes': exe.stat().st_size, 'sha256': hashlib.sha256(exe.read_bytes()).hexdigest()}}
    mcp = target / 'YandexDiskRescueMCP.exe'
    manifest['mcp_executable'] = {'name': mcp.name, 'bytes': mcp.stat().st_size, 'sha256': hashlib.sha256(mcp.read_bytes()).hexdigest()}
    (target / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    members += ['release-manifest.json'] + ['licenses/' + name for name in sources]
    archive = target / ('YandexDiskRescue-' + VERSION + '-win-x64.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for name in members:
            zipped.write(target / name, name)
    print(json.dumps({'archive': archive.name, 'bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'members': members}))


if __name__ == '__main__':
    main()
