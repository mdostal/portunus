"""`portunus t3 ...`: the 1Password-CLI-compatible reader T3 Code's secret
provider runs, plus its launcher and opt-in flag.

`t3 op` is the only code path that writes a value to stdout, so most of this
file is about the cases where it must write nothing: reader disabled, stdout
a terminal, the caller not T3 Code, wrong account, wrong reference, a
reference the broker will not release.
"""
import io
import json
import os
import stat
import subprocess
import sys

import pytest

from portunus import t3 as t3_mod
from portunus.audit import AuditChain
from portunus.broker import Broker
from portunus.cli import _build, main
from portunus.localvault import LocalEncryptedBackend
from portunus.registry import Registry

SECRET = "hunter2-test"
OTHER = " second hunter2-test value\n"  # whitespace must survive byte for byte
REF = "op://portunus/sudo-pw/value"
T3_SERVER = ("t3code", ["/opt/t3code-bin/t3code", "/opt/t3code-bin/resources/app.asar/apps/server/dist/bin.mjs"])
READ = ["--cache=false", "read", "--account=portunus", "--no-newline"]


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def _add(name, sm_name, value, state="enabled", gated=False):
    Registry().add(name, sm_name, kind="password", state=state)
    LocalEncryptedBackend().store(sm_name, value)
    if gated:
        Broker(Registry()).gate(name, on=True)


@pytest.fixture
def vault(home):
    """An enabled reader over a vault with two enabled references."""
    _add("sudo-pw", "dostal-sudo", SECRET)
    _add("api.key_2", "dostal-api", OTHER)
    t3_mod.set_enabled(True)
    return home


class FakeOut:
    """A stdout stand-in: bytes land in `.buffer`, and it can claim to be a
    terminal."""

    def __init__(self, tty=False):
        self.buffer = io.BytesIO()
        self._tty = tty

    def isatty(self):
        return self._tty


def run_op(argv, parent=T3_SERVER, tty=False, env=None):
    out, err = FakeOut(tty), io.StringIO()
    rc = t3_mod.op_main(argv, _build, stdout=out, stderr=err,
                        inspect=lambda: parent, env=env if env is not None else {})
    return rc, out.buffer.getvalue(), err.getvalue()


def _t3_audit():
    return [(e["secret"], e["result"]) for e in AuditChain().entries()
            if e["action"] == "t3_provider_read"]


def _assert_refused(result, reason, secret=None, code=1):
    rc, out, err = result
    assert rc == code
    assert out == b""
    assert err.startswith("portunus t3 op: ") and err.count("\n") == 1
    assert SECRET not in err
    entry = _t3_audit()[-1]
    assert entry[1] == f"denied:{reason}"
    if secret is not None:
        assert entry[0] == secret


# --- references -------------------------------------------------------------

def test_t3_reference_round_trips():
    assert t3_mod.t3_reference("sudo-pw") == REF
    assert t3_mod.parse_reference(REF) == "sudo-pw"
    assert t3_mod.parse_reference(t3_mod.t3_reference("my key.v2_x")) == "my key.v2_x"


@pytest.mark.parametrize("name", ["", "a/b", "a:b", " lead", "trail ", "naïve", "a{b", "a?b=c"])
def test_t3_reference_rejects_names_t3_cannot_express(name):
    with pytest.raises(t3_mod.BadReference):
        t3_mod.t3_reference(name)


@pytest.mark.parametrize("reference", [
    "op://Private/sudo-pw/value",          # another vault
    "op://portunus/sudo-pw/password",      # another field
    "op://portunus/sudo-pw/section/value",  # four segments
    "op://portunus/sudo-pw/value?attribute=otp",
    "op://portunus/sudo-pw",
    "op://portunus//value",
    "op://PORTUNUS/sudo-pw/value",
    "portunus/sudo-pw/value",
    "{{secret:sudo-pw}}",
    "hunter2-test",
])
def test_parse_reference_rejects_everything_else(reference):
    with pytest.raises(t3_mod.BadReference) as excinfo:
        t3_mod.parse_reference(reference)
    assert reference not in str(excinfo.value)  # never echo what might be a value


def test_cli_ref(home, capsys):
    _add("sudo-pw", "dostal-sudo", SECRET)
    assert main(["t3", "ref", "sudo-pw"]) == 0
    assert capsys.readouterr().out == REF + "\n"

    assert main(["t3", "ref", "nope"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "unknown reference" in captured.err

    Registry().add("team/key", "dostal-team-key")
    assert main(["t3", "ref", "team/key"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "cannot be used from T3 Code" in captured.err


# --- enable / disable / status ----------------------------------------------

def test_enable_disable_status(home, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(home / "no-such-bin"))
    assert main(["t3", "status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["enabled"] is False
    assert report["ready"] is False
    assert report["launcher"] == {"installed": False, "path": None, "ours": False}
    assert report["in_t3_thread"] is False
    assert report["prompt_surface"] == "auto"
    assert report["reference_format"] == "op://portunus/<reference-name>/value"
    assert report["provider"] == {"kind": "1Password", "name": "Portunus", "account": "portunus"}

    assert main(["t3", "enable"]) == 0
    assert "enabled" in capsys.readouterr().out
    flag = home / "t3.json"
    assert json.loads(flag.read_text()) == {"enabled": True}
    assert stat.S_IMODE(flag.stat().st_mode) == 0o600
    assert t3_mod.is_enabled()

    monkeypatch.setenv("T3CODE_THREAD_ID", "thread-1")
    main(["t3", "status", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["enabled"] is True
    assert report["in_t3_thread"] is True
    assert report["prompt_surface"] == "t3"

    assert main(["t3", "disable"]) == 0
    assert not t3_mod.is_enabled()
    changes = [e["result"] for e in AuditChain().entries() if e["action"] == "t3_config_changed"]
    assert changes == ["reader enabled", "reader disabled"]


def test_status_text_tells_the_human_what_to_type(home, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(home / "no-such-bin"))
    assert main(["t3", "status"]) == 0
    out = capsys.readouterr().out
    assert "reader: disabled" in out
    assert "portunus t3 install-shim" in out
    assert "name:    Portunus" in out
    assert "account: portunus" in out
    assert "op://portunus/<reference-name>/value" in out


def test_status_does_not_create_the_home(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "not-yet"
    monkeypatch.setenv("PORTUNUS_HOME", str(missing))
    assert main(["t3", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    assert not missing.exists()


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"enabled": "yes"}', '{"enabled": 1}'])
def test_a_damaged_flag_file_means_disabled(home, content):
    (home / "t3.json").write_text(content)
    assert t3_mod.is_enabled() is False


# --- the launcher -----------------------------------------------------------

@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """A scratch bin directory that is the whole PATH, so no `op` or
    `portunus` installed on this machine can influence the test."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


def _foreign_op(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "op"
    path.write_text("#!/bin/sh\necho 1Password CLI\n")
    path.chmod(0o755)
    return path


def test_install_shim_writes_an_executable_launcher(home, bindir, capsys):
    assert main(["t3", "install-shim", "--dir", str(bindir)]) == 0
    captured = capsys.readouterr()
    shim = bindir / "op"
    text = shim.read_text()
    lines = text.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[1] == "# portunus-t3-op-shim"
    assert lines[-1].startswith("exec '") and lines[-1].endswith(' t3 op "$@"')
    assert stat.S_IMODE(shim.stat().st_mode) == 0o755
    assert t3_mod.is_our_shim(shim)
    assert str(shim) in captured.out
    assert "portunus t3 enable" in captured.out  # reminded that it is still off

    # Re-running replaces our own launcher without complaint.
    assert main(["t3", "install-shim", "--dir", str(bindir)]) == 0
    capsys.readouterr()

    main(["t3", "status", "--json"])
    launcher = json.loads(capsys.readouterr().out)["launcher"]
    assert launcher == {"installed": True, "path": str(shim), "ours": True}


def test_install_shim_uses_the_absolute_path_of_portunus(home, bindir):
    fake = bindir / "portunus"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    result = t3_mod.install_shim(bindir)
    assert result["command"] == [str(fake)]
    assert (bindir / "op").read_text().splitlines()[-1] == f"exec '{fake}' t3 op \"$@\""
    assert t3_mod.default_shim_dir() == bindir


def test_install_shim_refuses_to_overwrite_a_foreign_op(home, bindir, capsys):
    foreign = _foreign_op(bindir)
    before = foreign.read_text()
    assert main(["t3", "install-shim", "--dir", str(bindir)]) == 1
    assert "not the Portunus launcher" in capsys.readouterr().err
    assert foreign.read_text() == before

    assert main(["t3", "install-shim", "--dir", str(bindir), "--force"]) == 0
    assert t3_mod.is_our_shim(foreign)


def test_install_shim_refuses_when_another_op_is_on_path(home, bindir, tmp_path, monkeypatch, capsys):
    elsewhere = _foreign_op(tmp_path / "onepassword")
    monkeypatch.setenv("PATH", os.pathsep.join([str(elsewhere.parent), str(bindir)]))
    assert main(["t3", "install-shim", "--dir", str(bindir)]) == 1
    err = capsys.readouterr().err
    assert str(elsewhere) in err and "conflict" in err
    assert not (bindir / "op").exists()

    assert main(["t3", "install-shim", "--dir", str(bindir), "--force"]) == 0
    assert "another `op` is also on PATH" in capsys.readouterr().err
    assert t3_mod.is_our_shim(bindir / "op")
    assert elsewhere.read_text() == "#!/bin/sh\necho 1Password CLI\n"

    main(["t3", "status", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["launcher"]["ours"] is False  # the foreign one comes first on PATH
    assert report["ready"] is False


def test_install_shim_warns_when_the_directory_is_not_on_path(home, bindir, tmp_path, capsys):
    target = tmp_path / "off-path"
    assert main(["t3", "install-shim", "--dir", str(target)]) == 0
    assert "not on PATH" in capsys.readouterr().err
    assert t3_mod.is_our_shim(target / "op")


def test_uninstall_shim_only_removes_ours(home, bindir, tmp_path, capsys):
    foreign = _foreign_op(tmp_path / "onepassword")
    assert main(["t3", "uninstall-shim", "--dir", str(foreign.parent)]) == 1
    assert "not removing" in capsys.readouterr().err
    assert foreign.exists()

    main(["t3", "install-shim", "--dir", str(bindir)])
    capsys.readouterr()
    assert main(["t3", "uninstall-shim", "--dir", str(bindir)]) == 0
    assert f"removed {bindir / 'op'}" in capsys.readouterr().out
    assert not (bindir / "op").exists()

    assert main(["t3", "uninstall-shim", "--dir", str(bindir)]) == 0
    assert "nothing removed" in capsys.readouterr().out


def test_uninstall_shim_finds_ours_on_path_and_skips_foreign(home, bindir, tmp_path, monkeypatch, capsys):
    foreign = _foreign_op(tmp_path / "onepassword")
    main(["t3", "install-shim", "--dir", str(bindir), "--force"])
    monkeypatch.setenv("PATH", os.pathsep.join([str(foreign.parent), str(bindir)]))
    capsys.readouterr()
    assert main(["t3", "uninstall-shim"]) == 0
    assert not (bindir / "op").exists()
    assert foreign.exists()


def test_launcher_run_from_a_non_t3_process_is_refused(vault, bindir):
    """End to end through the real launcher and a real process tree: the
    parent here is the Python running pytest, so the read is refused."""
    t3_mod.install_shim(bindir, command=[sys.executable, "-m", "portunus"])
    env = dict(os.environ, PORTUNUS_HOME=str(vault))
    proc = subprocess.run([str(bindir / "op"), *READ, REF], capture_output=True, env=env,
                          timeout=60)
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert b"only the T3 Code server" in proc.stderr
    assert SECRET.encode() not in proc.stderr
    assert _t3_audit()[-1][1].startswith("denied:parent:python")


# --- read -------------------------------------------------------------------

def test_read_returns_the_exact_value_without_a_newline(vault):
    rc, out, err = run_op([*READ, REF])
    assert (rc, out, err) == (0, SECRET.encode(), "")
    assert _t3_audit() == [("dostal-sudo", "ok")]
    resolves = [e["result"] for e in AuditChain().entries()
                if e["action"] == "resolve" and e["secret"] == "dostal-sudo"]
    assert "ok" in resolves  # went through the resolver like any other read
    assert SECRET not in (vault / "audit.log").read_text()


def test_read_keeps_whitespace_byte_for_byte(vault):
    rc, out, _ = run_op([*READ, "op://portunus/api.key_2/value"])
    assert rc == 0
    assert out == OTHER.encode()


@pytest.mark.parametrize("argv", [
    ["read", "--account=portunus", "--no-newline", REF],
    ["--cache=false", "read", "--account", "portunus", "--no-newline", REF],
    ["--cache=false", "--account=portunus", "read", "-n", REF],
    ["--cache=false", "read", REF, "--no-newline", "--account=portunus"],
])
def test_read_tolerates_equivalent_spellings(vault, argv):
    assert run_op(argv)[:2] == (0, SECRET.encode())


def test_read_through_the_cli_entry_point(vault, monkeypatch, capsysbinary):
    monkeypatch.setattr(t3_mod, "inspect_parent", lambda pid=None: T3_SERVER)
    assert main(["t3", "op", *READ, REF]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == SECRET.encode()
    assert captured.err == b""


def test_read_through_the_cli_with_an_explicit_home(vault, tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setattr(t3_mod, "inspect_parent", lambda pid=None: T3_SERVER)
    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path / "empty-home"))
    assert main(["t3", "op", *READ, REF]) == 1
    assert capsysbinary.readouterr().out == b""
    assert main(["--home", str(vault), "t3", "op", *READ, REF]) == 0
    assert capsysbinary.readouterr().out == SECRET.encode()
    assert os.environ["PORTUNUS_HOME"] == str(tmp_path / "empty-home")


def test_cli_entry_point_refuses_a_real_non_t3_parent(vault, capsysbinary):
    """No injected parent: pytest's own parent is normally a shell, a Python
    or an agent, never T3 Code."""
    if t3_mod.parent_allowed(*t3_mod.inspect_parent())[0]:
        pytest.skip("pytest was started directly by a node/electron process")
    assert main(["t3", "op", *READ, REF]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"only the T3 Code server" in captured.err


# --- read: refusals ---------------------------------------------------------

def test_refused_when_not_enabled(vault):
    t3_mod.set_enabled(False)
    _assert_refused(run_op([*READ, REF]), "disabled")
    (vault / "t3.json").unlink()
    _assert_refused(run_op([*READ, REF]), "disabled")


def test_refused_when_stdout_is_a_terminal(vault):
    _assert_refused(run_op([*READ, REF], tty=True), "tty")


@pytest.mark.parametrize("account", ["--account=my.1password.com", "--account=Portunus",
                                     "--account=", None])
def test_refused_for_any_other_account(vault, account):
    argv = ["--cache=false", "read", "--no-newline", REF]
    if account is not None:
        argv.insert(2, account)
    _assert_refused(run_op(argv), "account")


@pytest.mark.parametrize("reference", [
    "op://Private/sudo-pw/value",
    "op://portunus/sudo-pw/password",
    "op://portunus/sudo-pw/value?attribute=otp",
    "op://portunus/a/b/value",
    "sudo-pw",
])
def test_refused_for_a_bad_reference_shape(vault, reference):
    result = run_op([*READ, reference])
    _assert_refused(result, "reference", secret="-")
    assert reference not in result[2]


def test_refused_for_an_unknown_reference(vault):
    _assert_refused(run_op([*READ, "op://portunus/nope/value"]), "unknown-reference", "nope")


def test_refused_for_a_gated_reference_until_approved(vault):
    _add("root-pw", "dostal-root", SECRET, gated=True)
    result = run_op([*READ, "op://portunus/root-pw/value"])
    _assert_refused(result, "approval-required", "dostal-root")
    assert "portunus approve root-pw" in result[2]

    Broker(Registry()).approve("root-pw", ttl_seconds=600)
    assert run_op([*READ, "op://portunus/root-pw/value"])[:2] == (0, SECRET.encode())


def test_refused_for_a_reference_that_is_not_enabled(vault):
    _add("parked", "dostal-parked", SECRET, state="dropped")
    _assert_refused(run_op([*READ, "op://portunus/parked/value"]), "not-injectable",
                    "dostal-parked")


def test_refused_when_role_policy_denies_the_reader(vault):
    """Role enforcement applies here as for any other resolve: the reader
    runs as whoever the T3 Code server runs as (`tester` in this suite)."""
    from portunus.roles import set_enforcement, set_policy

    Registry().add("org-key", "dostal-org-key", org="acme", state="enabled")
    LocalEncryptedBackend().store("dostal-org-key", SECRET)
    set_policy("org", "acme", "dev", ["read"], principal="someone-else")
    assert run_op([*READ, "op://portunus/org-key/value"])[:2] == (0, SECRET.encode())
    set_enforcement(True)
    _assert_refused(run_op([*READ, "op://portunus/org-key/value"]), "not-authorized",
                    "dostal-org-key")


def test_refused_when_the_vault_has_no_value(vault):
    Registry().add("hollow", "dostal-hollow", state="enabled")
    _assert_refused(run_op([*READ, "op://portunus/hollow/value"]), "backend-error",
                    "dostal-hollow")


@pytest.mark.parametrize("parent,label", [
    (("zsh", ["/usr/bin/zsh", "-c", "op read ..."]), "zsh"),
    (("bash", ["bash"]), "bash"),
    (("claude.exe", ["/home/u/.nvm/versions/node/v24/bin/claude"]), "claude"),
    (("codex", ["codex", "exec"]), "codex"),
    (("python3.12", ["python3", "-c", "..."]), "python3.12"),
    (("node", ["node", "/home/u/.nvm/lib/node_modules/@anthropic-ai/claude-code/cli.js"]), "agent-cli"),
    (("cat", ["cat"]), "cat"),
    (("", []), "unknown"),
])
def test_refused_for_a_parent_that_is_not_t3_code(vault, parent, label):
    _assert_refused(run_op([*READ, REF], parent=parent), f"parent:{label}")


def test_extra_parents_from_env(vault):
    parent = ("t3-server", ["/opt/t3/t3-server"])
    _assert_refused(run_op([*READ, REF], parent=parent), "parent:t3-server")
    env = {"PORTUNUS_T3_PARENTS": "bun, t3-server"}
    assert run_op([*READ, REF], parent=parent, env=env)[:2] == (0, SECRET.encode())
    # The allow-list cannot be used to let a shell or an agent in.
    env = {"PORTUNUS_T3_PARENTS": "zsh,claude"}
    _assert_refused(run_op([*READ, REF], parent=("zsh", ["zsh"]), env=env), "parent:zsh")
    _assert_refused(run_op([*READ, REF], parent=("claude", ["claude"]), env=env), "parent:claude")


@pytest.mark.parametrize("argv", [
    [],
    ["--version"],
    ["whoami"],
    ["item", "get", "sudo-pw"],
    ["read", "--account=portunus", REF],                       # would append a newline
    ["read", "--account=portunus", "--no-newline"],             # no reference
    ["read", "--account=portunus", "--no-newline", REF, REF],
    ["read", "--account=portunus", "--no-newline", "--force", REF],
    ["read", "--account=portunus", "--account=portunus", "--no-newline", REF],
    ["inject", "--account=portunus"],                           # no template
    ["inject", "--account=portunus", "-i"],
    ["inject", "--account=portunus", "-o", "/x", "-i", "/y"],
])
def test_anything_but_the_two_t3_calls_is_a_usage_error(vault, argv):
    _assert_refused(run_op(argv), "usage", code=2)


def test_refusal_without_a_home_creates_nothing(tmp_path, monkeypatch):
    missing = tmp_path / "not-yet"
    monkeypatch.setenv("PORTUNUS_HOME", str(missing))
    rc, out, err = run_op([*READ, REF])
    assert (rc, out) == (1, b"")
    assert "portunus t3 enable" in err
    assert not missing.exists()


# --- inject -----------------------------------------------------------------

SEPARATOR = "__t3-secret-6f1c2f0e-58a1-4c7e-9a43-0d6f7c1b2a90__"


def _template(tmp_path, references, name="t3code-op-inject-1"):
    """The file T3 Code writes: `{{ <reference> }}<separator>` per reference."""
    path = tmp_path / name
    path.write_text("".join(f"{{{{ {reference} }}}}{SEPARATOR}" for reference in references))
    return path


def _inject(path, **kwargs):
    return run_op(["--cache=false", "inject", "--account=portunus", "-i", str(path)], **kwargs)


def test_inject_round_trips_two_references(vault, tmp_path):
    path = _template(tmp_path, [REF, "op://portunus/api.key_2/value"])
    rc, out, err = _inject(path)
    assert (rc, err) == (0, "")
    values = out.decode().split(SEPARATOR)
    assert values == [SECRET, OTHER, ""]  # exactly how T3 Code splits it
    assert sorted(_t3_audit()) == [("dostal-api", "ok"), ("dostal-sudo", "ok")]
    assert path.exists()  # the template is T3 Code's to clean up


def test_inject_repeated_reference_and_long_flag(vault, tmp_path):
    path = _template(tmp_path, [REF, REF])
    rc, out, _ = run_op(["inject", "--account", "portunus", "--in-file", str(path)])
    assert rc == 0
    assert out.decode().split(SEPARATOR) == [SECRET, SECRET, ""]
    assert _t3_audit() == [("dostal-sudo", "ok")]


@pytest.mark.parametrize("bad,reason,secret", [
    ("op://portunus/nope/value", "unknown-reference", "nope"),
    ("op://portunus/parked/value", "not-injectable", "dostal-parked"),
    ("op://portunus/root-pw/value", "approval-required", "dostal-root"),
    ("op://Private/sudo-pw/password", "reference", "-"),
])
def test_inject_fails_closed_when_one_reference_is_unreadable(vault, tmp_path, bad, reason, secret):
    _add("parked", "dostal-parked", SECRET, state="dropped")
    _add("root-pw", "dostal-root", SECRET, gated=True)
    result = _inject(_template(tmp_path, [REF, bad]))
    _assert_refused(result, reason, secret)
    assert not [entry for entry in _t3_audit() if entry[1] == "ok"]


def test_inject_checks_the_same_guard_rails(vault, tmp_path):
    path = _template(tmp_path, [REF])
    _assert_refused(_inject(path, tty=True), "tty")
    _assert_refused(_inject(path, parent=("zsh", ["zsh"])), "parent:zsh")
    _assert_refused(run_op(["inject", "--account=work", "-i", str(path)]), "account")
    t3_mod.set_enabled(False)
    _assert_refused(_inject(path), "disabled")


def test_inject_rejects_templates_t3_would_not_write(vault, tmp_path):
    def write(text, name):
        path = tmp_path / name
        path.write_text(text)
        return path

    _assert_refused(_inject(write("no references here", "none")), "template")
    _assert_refused(_inject(write(f"{{{{ {REF} }}}} and {{{{secret:sudo-pw}}}}", "stray")),
                    "template")
    _assert_refused(_inject(write(f"{{{{ {REF} }}}}" * 65, "many")), "template")
    _assert_refused(_inject(write(f"{{{{ {REF} }}}}" + "x" * (256 * 1024), "big")), "template")
    _assert_refused(_inject(tmp_path / "missing"), "template")
    _assert_refused(_inject(tmp_path), "template")

    assert _inject(write(f"{{{{ {REF} }}}}" * 64, "max"))[0] == 0


# --- the parent check -------------------------------------------------------

@pytest.mark.parametrize("exe,argv,extra,allowed,seen", [
    # T3 Code's server as it really appears (desktop build, CLI, plain node).
    ("t3code", ["/opt/t3code-bin/t3code", "--require", "/opt/t3code-bin/resources/app.asar/x.cjs",
                "/opt/t3code-bin/resources/app.asar/apps/server/dist/bin.mjs"], (), True, "t3code"),
    ("t3", ["t3", "serve"], (), True, "t3"),
    ("node", ["node", "/usr/lib/node_modules/t3/dist/bin.mjs"], (), True, "node"),
    ("node", ["node", "-e", "require('child_process').spawnSync('op')"], (), True, "node"),
    ("electron", ["/usr/lib/electron/electron", "/opt/t3/app.asar"], (), True, "electron"),
    ("/usr/bin/Node", ["node"], (), True, "node"),
    ("node (deleted)", ["node", "server.js"], (), True, "node"),
    # A home directory called "claude" is not an agent.
    ("node", ["node", "/home/claude/.local/lib/node_modules/t3/dist/bin.mjs"], (), True, "node"),
    # Shells.
    ("sh", ["sh", "-c", "op read"], (), False, "sh"),
    ("bash", ["/bin/bash"], (), False, "bash"),
    ("zsh", ["-zsh"], (), False, "zsh"),
    ("fish", ["fish"], (), False, "fish"),
    ("dash", ["dash"], (), False, "dash"),
    # Agent CLIs, native and under node.
    ("claude", ["claude"], (), False, "claude"),
    ("claude.exe", ["/home/u/bin/claude", "--output-format", "stream-json"], (), False, "claude"),
    ("codex", ["codex"], (), False, "codex"),
    ("node", ["node", "/home/u/.nvm/versions/node/v24/bin/claude"], (), False, "agent-cli"),
    ("node", ["node", "--no-warnings", "/usr/lib/node_modules/@openai/codex/bin/codex.js"], (),
     False, "agent-cli"),
    ("node", ["claude"], (), False, "claude"),
    # Python, any version.
    ("python", ["python", "x.py"], (), False, "python"),
    ("python3.12", ["python3", "-m", "pytest"], (), False, "python3.12"),
    # Not on the allow-list, unknown, or unreadable.
    ("ruby", ["ruby"], (), False, "ruby"),
    ("systemd", ["/usr/lib/systemd/systemd", "--user"], (), False, "systemd"),
    ("", [], (), False, "unknown"),
    # PORTUNUS_T3_PARENTS adds names but never a shell or an agent.
    ("bun", ["bun", "server.ts"], ("bun",), True, "bun"),
    ("bun", ["bun", "server.ts"], (), False, "bun"),
    ("T3 Code Helper", ["T3 Code Helper"], ("T3 Code Helper",), True, "t3 code helper"),
    ("zsh", ["zsh"], ("zsh",), False, "zsh"),
    ("codex", ["codex"], ("codex",), False, "codex"),
    ("python3", ["python3"], ("python3",), False, "python3"),
])
def test_parent_allowed(exe, argv, extra, allowed, seen):
    assert t3_mod.parent_allowed(exe, argv, extra) == (allowed, seen)


def test_inspect_parent_reads_a_real_process():
    exe, argv = t3_mod.inspect_parent(os.getpid())  # this pytest process
    assert exe.lower().startswith("python")
    assert argv


def test_inspect_parent_of_a_missing_process_is_empty():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert t3_mod.inspect_parent(proc.pid) == ("", [])


# --- structure --------------------------------------------------------------

def test_op_subcommand_is_registered_and_bypasses_argparse(vault, capsysbinary):
    """`t3 op --cache=false ...` must not die in argparse on op's own flags,
    and a bad call must not print a usage screen."""
    assert main(["t3", "op", "--cache=false", "whoami"]) == 2
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert captured.err.count(b"\n") == 1 and b"usage:" not in captured.err
