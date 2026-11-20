"""Targeted dotenv edits; untouched bindings retain their exact UTF-8 text."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import tempfile

from dotenv.parser import parse_stream


class EnvFile:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('Refusing an env symlink')
        self.text = self.path.read_bytes().decode('utf-8') if self.path.exists() else ''
        self.bindings = list(parse_stream(io.StringIO(self.text)))
        if any(binding.error for binding in self.bindings):
            raise ValueError('Invalid dotenv syntax; no configuration was written')
        self.values = {b.key: b.value for b in self.bindings if b.key is not None and b.value is not None}

    @staticmethod
    def _replace_value(original, value):
        match = re.match(r"\s*(?:export[ \t]+)?(?:'[^']*'|[^\s=]+)[ \t]*=[ \t]*", original)
        if not match:
            # A bare variable has no value. Preserve its newline when assigning it.
            ending = '\r\n' if original.endswith('\r\n') else '\n' if original.endswith('\n') else ''
            return original.rstrip('\r\n') + '=' + json.dumps(value, ensure_ascii=False) + ending
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
        return original[:start] + json.dumps(value, ensure_ascii=False) + original[end:]

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
                result += key + '=' + json.dumps(value, ensure_ascii=False) + ending
        return result


def private_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing an env symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, delete=False) as f:
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
    docs = [EnvFile(root / '.env'), EnvFile(root / 'home-runner/.env')]
    for doc in docs:
        if 'CLOUD_INTERNAL_KEY' in doc.values:
            private_write(doc.path, doc.updated(remove={'CLOUD_INTERNAL_KEY'}))


def sync_components(root):
    source = EnvFile(root / '.env')
    runner = EnvFile(root / 'home-runner/.env')
    gateway = EnvFile(root / 'gateway/.env')
    # Cloud configuration belongs exclusively to the gateway and storage.
    shared = {k: v for k, v in source.values.items() if not k.startswith('CLOUD_')}
    plan = [(source, source.updated(remove={'CLOUD_INTERNAL_KEY'})),
            (runner, runner.updated(shared, remove={b.key for b in runner.bindings if b.key and b.key.startswith('CLOUD_')})),
            (gateway, gateway.updated(shared))]
    for doc, text in plan:
        if text != doc.text or not doc.path.exists():
            private_write(doc.path, text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--isolate-runner', action='store_true')
    args = parser.parse_args()
    try:
        isolate_runner(args.root) if args.isolate_runner else sync_components(args.root)
    except (OSError, ValueError):
        parser.exit(1, 'Cannot update env files safely; inspect permissions and dotenv syntax.\n')
