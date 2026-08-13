"""ARCA/OSTIARIUS boundary injection adapters.

Each adapter answers exactly one dangerous question -- "put this value at
this target" -- and is one of the few pieces of code, alongside the sinks in
resolver.py, ever allowed to see a resolved secret value. An adapter's
``inject()`` MUST NEVER return the value, log it, or include it in an
exception message -- on the happy path or the failure path. Callers pass an
adapter's ``inject`` as the boundary callable to ``Resolver.resolve_call``,
so the value flows resolver -> adapter -> target and nowhere else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

try:
    import yaml  # optional third-party dep, per repo convention (see hive/lib/config.py)
except Exception:
    yaml = None

_FILE_FORMATS = ("env", "json", "yaml")


class AdapterError(RuntimeError):
    """An adapter could not complete injection. Message NEVER includes the value."""


@runtime_checkable
class SecretAdapter(Protocol):
    def inject(self, value: str, **target_params: str) -> None:
        """Place `value` at the target described by target_params. Returns nothing."""
        ...


class EnvVarAdapter:
    """Injects into the current process's environment.

    Distinct from resolver.py's existing subprocess-argv sink: this sets the
    variable in THIS process's environment, for callers who want the value
    available to code running in-process (or to a child process they spawn
    afterward via a normal env-inheriting exec).
    """

    def inject(self, value: str, var_name: str = "") -> None:
        if not var_name:
            raise AdapterError("env adapter requires a non-empty var_name")
        os.environ[var_name] = value


class FileAdapter:
    """Writes a 0600 file, templating `value` under `key` in the requested format.

    Generalizes resolver.py's existing 0600-temp-file discipline to a
    caller-specified path/format instead of only an anonymous temp file.
    """

    def inject(self, value: str, path: str = "", fmt: str = "", key: str = "") -> None:
        if fmt not in _FILE_FORMATS:
            raise AdapterError(f"unsupported file format: {fmt!r} (want one of {_FILE_FORMATS})")
        if not key:
            raise AdapterError("file adapter requires a non-empty key")
        if not path:
            raise AdapterError("file adapter requires a non-empty path")

        if fmt == "env":
            content = f"{key}={value}\n"
        elif fmt == "json":
            content = json.dumps({key: value}, indent=2)
        else:  # yaml
            if yaml is not None:
                content = yaml.safe_dump({key: value})
            else:
                # Minimal fallback so this adapter never hard-requires PyYAML.
                content = f"{key}: {value}\n"

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        os.chmod(target, 0o600)
