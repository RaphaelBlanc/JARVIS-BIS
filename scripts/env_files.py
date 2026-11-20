"""Targeted dotenv edits and layered runtime configuration."""
import argparse
import io
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


def sync_components(root, strict=True):
    source = EnvFile(root / '.env', strict=strict)
    runner = EnvFile(root / 'home-runner/.env', strict=strict)
    gateway = EnvFile(root / 'gateway/.env', strict=strict)
    if not source.path.exists():
        isolate_runner(root)
        return
    # Cloud configuration belongs exclusively to the gateway and storage.
    shared = {k: v for k, v in source.values.items() if not k.startswith('CLOUD_')}
    plan = [(source, source.updated(remove={'CLOUD_INTERNAL_KEY'})),
            (runner, runner.updated(shared, remove={b.key for b in runner.bindings if b.key and b.key not in shared})),
            (gateway, gateway.updated(shared, remove={b.key for b in gateway.bindings if b.key and not b.key.startswith('CLOUD_') and b.key not in shared}))]
    # A cloud secret in a linked root/runner must be fixed explicitly. Other
    # links remain readable for legacy local installs; never replace the link.
    for doc, text in plan:
        if doc.path.is_symlink() and text != doc.text and 'CLOUD_INTERNAL_KEY' in doc.values and doc is not gateway:
            raise ValueError('Cloud key in linked configuration; remove it explicitly')
    for doc, text in plan:
        if text != doc.text or not doc.path.exists():
            if doc.path.is_symlink() and not strict:
                warnings.warn(f'Leaving linked component configuration unchanged: {doc.path}')
            else:
                private_write(doc.path, text)


def load_gateway(root):
    """Process environment wins; root owns shared settings, gateway owns CLOUD_*.

    Component-only installs without a root .env keep their existing semantics.
    Values absent at the root cannot leak back from a stale component copy.
    """
    root = Path(root)
    source = EnvFile(root / '.env', strict=False)
    component = EnvFile(root / 'gateway/.env', strict=False)
    values = ({k: v for k, v in source.values.items() if not k.startswith('CLOUD_')}
              if source.path.exists() else dict(component.values))
    values.update({k: v for k, v in component.values.items() if k.startswith('CLOUD_')})
    load_dotenv(stream=io.StringIO(''.join(k + '=' + EnvFile._quote(v) + '\n' for k, v in values.items())), override=False)


def load_shared(root, component=None):
    root = Path(root)
    path = root / '.env'
    if not path.exists() and component:
        path = root / component / '.env'
    values = EnvFile(path, strict=False).values
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
