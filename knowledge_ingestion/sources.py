"""One intake policy for scanning and publication preflight.

Only the valid subset belongs to a scan. Skipped inputs remain warnings; if
they later become valid the changed membership invalidates the original plan.
"""
import hashlib
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .storage import contains_symlink

SOURCE_POLICY_VERSION = 'source-intake-1'
MAX_BYTES = 5 * 1024 * 1024


def source_path(uri):
    parsed = urlsplit(uri)
    if parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('invalid_source_uri')
    return Path(unquote(parsed.path))


def collect_sources(kind, sources, data, limit):
    """Return eligible metadata and warnings without following source links.

    Hidden directory trees are excluded, including an explicitly selected hidden
    service directory/file ancestor. Hidden filenames in an ordinary directory
    are allowed. `data` and its descendants are never source material.
    """
    data = Path(os.path.abspath(data))
    paths = [source_path(uri) for uri in sources]
    warnings, files = [], []

    def warn(path, reason):
        warnings.append({'path': str(path), 'reason': reason})

    def excluded(path, directory=False):
        if contains_symlink(path):
            warn(path, 'symlink')
            return True
        if path == data or data in path.parents:
            warn(path, 'application_data')
            return True
        parts = path.parts if directory else path.parent.parts
        if any(p.startswith('.') and p not in {'.', '..'} for p in parts):
            warn(path, 'hidden_service_directory')
            return True
        return False

    if kind == 'directory':
        root = paths[0]
        if not excluded(root, directory=True):
            if not root.is_dir():
                warn(root, 'not_found_or_not_directory')
            else:
                for base, dirs, names in os.walk(root, followlinks=False,
                        onerror=lambda e: warn(e.filename, 'unreadable')):
                    base = Path(base)
                    # Exclusions are silent on traversal, consistent with .obsidian.
                    kept = []
                    for name in sorted(dirs):
                        path = base / name
                        if path.is_symlink():
                            warn(path, 'symlink')
                        elif not name.startswith('.') and path != data:
                            kept.append(name)
                    dirs[:] = kept
                    for name in sorted(names):
                        path = base / name
                        if path.is_symlink():
                            warn(path, 'symlink')
                        elif path.suffix.lower() in {'.md', '.txt'}:
                            files.append(path)
    else:
        root = None
        files = paths
    accepted = []
    for path in sorted(set(files), key=str):
        if excluded(path):
            continue
        if not path.exists():
            warn(path, 'not_found'); continue
        if not path.is_file():
            warn(path, 'nonfile'); continue
        if path.suffix.lower() not in {'.md', '.txt'}:
            warn(path, 'unsupported_type'); continue
        try:
            with path.open('rb') as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                warn(path, 'too_large'); continue
            if b'\0' in raw:
                raise ValueError('binary')
            raw.decode('utf-8')
        except (UnicodeDecodeError, ValueError):
            warn(path, 'invalid_utf8_or_binary'); continue
        except OSError:
            warn(path, 'unreadable'); continue
        accepted.append({'path': path, 'locator': path.relative_to(root).as_posix() if root else path.as_uri(),
                         'sha256': hashlib.sha256(raw).hexdigest()})
    return accepted, sorted(warnings, key=lambda w: (w['path'], w['reason']))


class NoUsableSources(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__('no_usable_sources')
