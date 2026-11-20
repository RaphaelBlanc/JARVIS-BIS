"""Targeted dotenv edits and layered runtime configuration."""
import argparse
import io
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
import warnings

from dotenv.parser import parse_stream
from dotenv import load_dotenv


class EnvFile:
    def __init__(self, path, strict=True):
        self.path = Path(path)
        if strict and self.path.is_symlink():
            raise ValueError('Refusing an env symlink')
        self.text = self.path.read_bytes().decode('utf-8', errors='strict' if strict else 'surrogateescape') if self.path.exists() else ''
        self.bindings = list(parse_stream(io.StringIO(self.text)))
        self.values = {}
        for binding in self.bindings:
            invalid = binding.error or any(0xdc80 <= ord(c) <= 0xdcff for c in binding.original.string)
            if invalid:
                cloud = re.search(r'(?m)^\s*(?:export\s+)?(?:CLOUD_\w*|DEPLOY_MODE)\s*=', binding.original.string)
                if strict or cloud:
                    raise ValueError('Invalid dotenv syntax; no configuration was written')
                warnings.warn(f'Ignoring unreadable dotenv binding at {self.path}:{binding.original.line}')
            elif binding.key is not None and binding.value is not None:
                self.values[binding.key] = binding.value

    @staticmethod
    def _quote(value):
        # Literal newlines and single quotes round-trip through both dotenv and
        # systemd EnvironmentFile. JSON escapes do not have the same semantics.
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    @staticmethod
    def _replace_value(original, value):
        match = re.match(r"\s*(?:export[ \t]+)?(?:'[^']*'|[^\s=]+)[ \t]*=[ \t]*", original)
        if not match:
            # A bare variable has no value. Preserve its newline when assigning it.
            ending = '\r\n' if original.endswith('\r\n') else '\n' if original.endswith('\n') else ''
            return original.rstrip('\r\n') + '=' + EnvFile._quote(value) + ending
        start = end = match.end()
        if start < len(original) and original[start] in ('"', "'"):
            quote = original[start]
            end = start + 1
            while end < len(original):
                if original[end] == '\\':
                    end += 2
                elif original[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
        else:
            while end < len(original) and original[end] not in '\r\n':
                if original[end] in ' \t' and original[end:].lstrip(' \t').startswith('#'):
                    break
                end += 1
            while end > start and original[end - 1] in ' \t':
                end -= 1
        return original[:start] + EnvFile._quote(value) + original[end:]

    def updated(self, updates=None, remove=()):
        updates = updates or {}
        seen, parts = set(), []
        for binding in self.bindings:
            if binding.key in remove:
                # Drop the sensitive assignment, retaining preceding blank lines.
                leading = re.match(r'\s*', binding.original.string).group()
                parts.append(leading)
            elif binding.key in updates:
                seen.add(binding.key)
                parts.append(binding.original.string if binding.value == updates[binding.key]
                             else self._replace_value(binding.original.string, updates[binding.key]))
            else:
                parts.append(binding.original.string)
        result = ''.join(parts)
        ending = '\r\n' if '\r\n' in self.text else '\n'
        for key, value in updates.items():
            if key not in seen:
                if result and not result.endswith(('\n', '\r')):
                    result += ending
                result += key + '=' + self._quote(value) + ending
        return result


def private_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing an env symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', errors='surrogateescape', newline='', dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def isolate_runner(root):
    docs = [EnvFile(root / '.env', strict=False), EnvFile(root / 'home-runner/.env', strict=False)]
    for doc in docs:
        if 'CLOUD_INTERNAL_KEY' in doc.values:
            private_write(doc.path, doc.updated(remove={'CLOUD_INTERNAL_KEY'}))


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _managed(doc):
    path = doc.path.with_name('.env.jarvis-managed.json')
    if path.is_symlink():
        raise ValueError('Refusing a linked sync manifest')
    return json.loads(path.read_text()) if path.exists() else {}


def _removed(source, doc, owned):
    # An unreadable root binding is not proof that a setting was removed.
    if any(b.error or any(0xdc80 <= ord(c) <= 0xdcff for c in b.original.string) for b in source.bindings):
        return set()
    return {k for k, digest in owned.items() if k not in source.values
            and k in doc.values and _digest(doc.values[k]) == digest}


def sync_components(root, strict=True):
    source = EnvFile(root / '.env', strict=strict)
    runner = EnvFile(root / 'home-runner/.env', strict=strict)
    gateway = EnvFile(root / 'gateway/.env', strict=strict)
    if not source.path.exists():
        isolate_runner(root)
        return
    shared = {k: v for k, v in source.values.items() if not k.startswith('CLOUD_')}
    plan = [(source, source.updated(remove={'CLOUD_INTERNAL_KEY'}), None)]
    for doc in (runner, gateway):
        owned = _managed(doc)
        remove = _removed(source, doc, owned)
        if doc is runner:
            remove |= {k for k in doc.values if k.startswith('CLOUD_')}
        updated = {k: _digest(v) for k, v in shared.items()
                   if k in owned or doc.values.get(k) != v}
        # Keep tracking temporarily unreadable source settings for a later sync.
        updated.update({k: v for k, v in owned.items() if k not in shared and k not in remove})
        plan.append((doc, doc.updated(shared, remove=remove), updated))
    for doc, text, owned in plan:
        if doc.path.is_symlink() and text != doc.text and 'CLOUD_INTERNAL_KEY' in doc.values and doc is not gateway:
            raise ValueError('Cloud key in linked configuration; remove it explicitly')
    for doc, text, owned in plan:
        if doc.path.is_symlink():
            if text != doc.text:
                warnings.warn(f'Leaving linked configuration unchanged: {doc.path}')
            continue
        if text != doc.text or not doc.path.exists():
            if owned is not None and doc.path.exists():
                # Never retain a legacy cloud key in a runner backup.
                backup = doc.updated(remove={'CLOUD_INTERNAL_KEY'}) if doc is runner else doc.text
                private_write(doc.path.with_name('.env.jarvis-backup'), backup)
            private_write(doc.path, text)
        if owned is not None:
            private_write(doc.path.with_name('.env.jarvis-managed.json'), json.dumps(owned, sort_keys=True))


def _component_values(source, component):
    values = dict(component.values)
    if source.path.exists():
        for key in _removed(source, component, _managed(component)):
            values.pop(key, None)
        values.update({k: v for k, v in source.values.items() if not k.startswith('CLOUD_')})
    return values


def load_gateway(root):
    """Environment > root shared settings > component-only settings; CLOUD_* is dedicated."""
    root = Path(root)
    source = EnvFile(root / '.env', strict=False)
    component = EnvFile(root / 'gateway/.env', strict=False)
    values = _component_values(source, component)
    load_dotenv(stream=io.StringIO(''.join(k + '=' + EnvFile._quote(v) + '\n' for k, v in values.items())), override=False)


def load_shared(root, component=None):
    root = Path(root)
    source = EnvFile(root / '.env', strict=False)
    values = _component_values(source, EnvFile(root / component / '.env', strict=False)) if component else source.values
    load_dotenv(stream=io.StringIO(''.join(k + '=' + EnvFile._quote(v) + '\n' for k, v in values.items()
                                        if not k.startswith('CLOUD_'))), override=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--isolate-runner', action='store_true')
    parser.add_argument('--startup', action='store_true')
    args = parser.parse_args()
    try:
        isolate_runner(args.root) if args.isolate_runner else sync_components(args.root, strict=not args.startup)
    except (OSError, ValueError):
        parser.exit(1, 'Cannot update env files safely; inspect permissions and dotenv syntax.\n')
