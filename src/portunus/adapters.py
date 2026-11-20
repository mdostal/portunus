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

import grp
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

try:
    import yaml  # optional third-party dep, per repo convention (see hive/lib/config.py)
except Exception:
    yaml = None

_FILE_FORMATS = ("env", "json", "yaml")
# Modes a file inject may ask for. Never world bits, never exec: a caller
# that isn't Portunus's own uid gets read (and, via a group-writable
# directory, delete) access through a shared group, not through "other".
_FILE_MODES = {"0600": 0o600, "0640": 0o640, "0660": 0o660}


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
    """Writes a file, templating `value` under `key` in the requested format.

    Generalizes resolver.py's existing 0600-temp-file discipline to a
    caller-specified path/format instead of only an anonymous temp file.

    The file is 0600 and owned by Portunus's own uid unless the caller asks
    for `mode` (0640/0660) and/or `group` (a name or gid). A caller running
    as another uid needs both to read the file it asked for (PANT-1142):
    0600 as Portunus's uid answers ok and leaves the caller a file it can't
    read. Group and mode are applied before the value is written, so a file
    that can't be given them never holds the value.

    The target must not exist yet, and a symlink there is never followed
    (first-cut-trust t1c). The path comes from whoever called inject -- over
    HTTP, possibly a process that can also plant files -- so following a
    symlink, or truncating a file someone else made, would write the value
    wherever they pointed it. The file is created with
    O_CREAT|O_EXCL|O_NOFOLLOW: the kernel refuses an existing file or link in
    one step, with no check-then-open gap. It is created at its final name,
    not a temp name renamed into place: rename() replaces whatever is there,
    and a temp file left behind when the UI kills an abandoned inject
    (PANT-1003) would hold the value under a name nobody cleans up.

    `replace=True` is the CLI-only escape hatch for refreshing a file you
    own (`portunus inject --replace`, never sent by the UI's HTTP route): it
    still refuses a symlink, writes a fresh temp file (mkstemp: O_EXCL, no
    follow) in the same directory and os.replace()s it over the target,
    which swaps the directory entry and never writes through it.
    """

    def inject(self, value: str, path: str = "", fmt: str = "", key: str = "",
               mode: str = "", group: str = "", replace: bool = False) -> None:
        if fmt not in _FILE_FORMATS:
            raise AdapterError(f"unsupported file format: {fmt!r} (want one of {_FILE_FORMATS})")
        if not key:
            raise AdapterError("file adapter requires a non-empty key")
        if not path:
            raise AdapterError("file adapter requires a non-empty path")
        file_mode = _file_mode(mode)
        gid = _group_id(group) if group else None

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
        if replace:
            if target.is_symlink():
                raise AdapterError(f"refusing to write {path}: it is a symlink")
            if target.exists() and not target.is_file():
                raise AdapterError(f"refusing to write {path}: it is not a regular file")
            fd, created = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        else:
            try:
                # 0600 from the start: write_text-then-chmod left it readable
                # under the default umask until the chmod ran (PANT-1003).
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
            except FileExistsError:
                raise AdapterError(
                    f"refusing to write {path}: something already exists there "
                    f"(inject never overwrites or follows a symlink; pick a new path, "
                    f"or from the CLI pass --replace for a regular file you own)"
                ) from None
            except OSError as exc:
                raise AdapterError(f"cannot create {path}: {exc.strerror}") from None
            created = str(target)

        try:
            with os.fdopen(fd, "w") as fh:
                try:
                    os.fchmod(fh.fileno(), 0o600)
                    if gid is not None and os.fstat(fh.fileno()).st_gid != gid:
                        os.fchown(fh.fileno(), -1, gid)
                    os.fchmod(fh.fileno(), file_mode)
                except OSError as exc:
                    raise AdapterError(
                        f"cannot give {path} group {group!r} / mode {oct(file_mode)}: "
                        f"{exc.strerror} (portunus runs as uid {os.getuid()}; it can only "
                        f"set a group it belongs to, or the directory's own setgid group)"
                    ) from None
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            if replace:
                os.replace(created, target)
        except BaseException:
            # Only ever the file this call created (O_EXCL / mkstemp), never
            # one that was there before.
            try:
                os.unlink(created)
            except FileNotFoundError:
                pass
            raise


# Not every platform has it (Windows); there, O_EXCL alone still refuses an
# existing link, since O_CREAT|O_EXCL never follows one.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _file_mode(mode: str) -> int:
    if not mode:
        return 0o600
    normalized = mode if len(mode) == 4 else "0" + mode
    if normalized not in _FILE_MODES:
        raise AdapterError(f"unsupported file mode: {mode!r} (want one of {sorted(_FILE_MODES)})")
    return _FILE_MODES[normalized]


def _group_id(group: str) -> int:
    if group.isdigit():
        return int(group)
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        raise AdapterError(f"unknown group: {group!r}") from None


def describe_file(path: str) -> dict:
    """Owner, group and mode of an injected file -- what a caller needs to
    tell whether it can read it. Metadata only, never the content."""
    st = os.stat(path)
    file_mode = stat.S_IMODE(st.st_mode)
    if file_mode & 0o040:
        readable_by = f"owner and group {st.st_gid}"
    else:
        readable_by = f"owner uid {st.st_uid} only"
    return {"path": path, "uid": st.st_uid, "gid": st.st_gid,
            "mode": f"{file_mode:04o}", "readable_by": readable_by}
