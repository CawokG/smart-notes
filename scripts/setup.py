#!/usr/bin/env python3
"""Install the project and the pinned upstream MuninnDB release locally."""
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parent.parent
VERSION = 'v0.9.0'
CHECKSUMS = {
    ('linux', 'amd64'): '4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b',
    ('linux', 'arm64'): '421ab1617b82eba55b4925f6c504793bbeaca13500621b994cddb3fae1e8db1c',
    ('darwin', 'amd64'): 'f26de1b7b72d8e287d85216acba26b35999c7fefec869d3890ff0051f8a7bab2',
    ('darwin', 'arm64'): 'bdbecd32e2df86f243dbb1cb2ad1a6ca8e3c4abb42f06a3bb2da43f0c17cf10a',
    ('windows', 'amd64'): 'e13a3536864537bd86a09d4202258813227081f8596bbae0853183d5798a8066',
}


def main():
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 or newer is required')
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    expected = CHECKSUMS.get((system, arch))
    if not expected:
        raise SystemExit(f'Unsupported platform: {system}/{machine}')
    name = f'muninn-{system}-{arch}' + ('.exe' if system == 'windows' else '')
    destination = ROOT / '.local' / 'bin' / ('muninn.exe' if system == 'windows' else 'muninn')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        url = f'https://github.com/scrypster/muninndb/releases/download/{VERSION}/{name}'
        print(f'Downloading MuninnDB {VERSION} ({system}/{arch})', flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
            temporary = Path(tmp.name)
            digest = hashlib.sha256()
            try:
                while block := response.read(1024 * 1024):
                    digest.update(block)
                    tmp.write(block)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            if digest.hexdigest() != expected:
                raise ValueError('MuninnDB checksum mismatch')
            temporary.chmod(0o755)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    env = ROOT / '.venv'
    venv.create(env, with_pip=True)
    python = env / ('Scripts/python.exe' if system == 'windows' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', str(ROOT)], check=True)
    print(f'Installed MuninnDB at {destination} and application in {env}')


if __name__ == '__main__':
    main()
