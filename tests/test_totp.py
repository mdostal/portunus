"""PANT-1404 / GH #253: TOTP seeds held by Portunus.

An agent gets the current code (and seconds left) for a totp reference and
nothing else: no CLI command or MCP tool returns the seed or the otpauth
URI, every code handed out is gated and audited, recovery codes are
one-time, and leak-scan looks for the seed.
"""
import base64
import json
import subprocess

import pytest

from portunus import totp
from portunus import prompt as prompt_mod
from portunus.broker import ApprovalRequired, NotInjectable
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend
from portunus.resolver import NotTotp, TotpNotResolvable

# RFC 6238 Appendix B seeds (ASCII), one per hash.
RFC_SEEDS = {
    "SHA1": b"12345678901234567890",
    "SHA256": b"12345678901234567890123456789012",
    "SHA512": b"1234567890123456789012345678901234567890123456789012345678901234",
}
RFC_VECTORS = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]

SEED = base64.b32encode(b"portunus-totp-seed!!").decode().rstrip("=")  # 32 chars
URI = f"otpauth://totp/GitHub:octo%40example.com?secret={SEED}&issuer=GitHub"
NEEDLES = (SEED, SEED.lower(), "otpauth://")


def _b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=")


def _assert_never_seen(*texts):
    for text in texts:
        for needle in NEEDLES:
            assert needle not in text, f"{needle!r} leaked"


def _assert_no_leak_on_disk(home):
    """Only the encrypted vault may hold the seed -- never the registry,
    audit log, approvals or anything else under PORTUNUS_HOME."""
    for path in home.rglob("*"):
        if path.is_file() and path.name not in ("vault", "master.key"):
            data = path.read_bytes()
            assert SEED.encode() not in data, path
            assert b"otpauth://" not in data, path


# --- RFC 6238 / RFC 4226 -------------------------------------------------------

@pytest.mark.parametrize("at,sha1,sha256,sha512", RFC_VECTORS)
def test_rfc6238_vectors(at, sha1, sha256, sha512):
    for algorithm, expected in (("SHA1", sha1), ("SHA256", sha256), ("SHA512", sha512)):
        params = totp.TotpParams(secret=RFC_SEEDS[algorithm], algorithm=algorithm, digits=8)
        assert totp.code_at(params, at) == expected, algorithm


@pytest.mark.parametrize("at,sha1,sha256,sha512", RFC_VECTORS)
def test_rfc6238_vectors_through_a_stored_uri(at, sha1, sha256, sha512):
    for algorithm, expected in (("SHA1", sha1), ("SHA256", sha256), ("SHA512", sha512)):
        uri = (f"otpauth://totp/RFC:test?secret={_b32(RFC_SEEDS[algorithm])}"
               f"&algorithm={algorithm}&digits=8&period=30")
        result = totp.current(totp.normalize(uri), now=at)
        assert result["code"] == expected
        assert result["seconds_left"] == 30 - (at % 30)
        assert set(result) == {"code", "seconds_left", "period", "digits"}


def test_rfc4226_hotp_vectors():
    expected = ["755224", "287082", "359152", "969429", "338314",
                "254676", "287922", "162583", "399871", "520489"]
    assert [totp.hotp(b"12345678901234567890", c) for c in range(10)] == expected


# --- parsing ----------------------------------------------------------------------

def test_parse_uri_and_canonical_form():
    params = totp.parse(URI)
    assert params.issuer == "GitHub" and params.account == "octo@example.com"
    assert (params.algorithm, params.digits, params.period) == ("SHA1", 6, 30)
    canonical = totp.normalize(URI)
    assert canonical.startswith("otpauth://totp/GitHub:octo@example.com?secret=" + SEED)
    assert totp.parse(canonical) == params


def test_bare_seed_accepts_spaces_lowercase_and_padding():
    grouped = " ".join(SEED.lower()[i:i + 4] for i in range(0, len(SEED), 4))
    assert totp.parse(grouped).secret == b"portunus-totp-seed!!"
    assert totp.parse(SEED + "====").secret == b"portunus-totp-seed!!"


def test_repr_redacts_the_seed():
    text = repr(totp.parse(URI)) + str(totp.parse(URI))
    assert "redacted" in text
    assert "portunus-totp-seed" not in text and SEED not in text


@pytest.mark.parametrize("bad", [
    "", "not base32 at all!", "GEZDGNA",  # too short
    "otpauth://hotp/x?secret=" + SEED,
    "otpauth://totp/x?issuer=nosecret",
    f"otpauth://totp/x?secret={SEED}&algorithm=MD5",
    f"otpauth://totp/x?secret={SEED}&digits=4",
    f"otpauth://totp/x?secret={SEED}&period=0",
    f"otpauth://totp/x?secret={SEED}&digits=six",
])
def test_invalid_values_raise_without_echoing_the_seed(bad):
    with pytest.raises(totp.TotpError) as exc:
        totp.parse(bad)
    assert SEED not in str(exc.value)


def test_decode_qr_uses_zbarimg_output():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="EAN-13 junk\n" + URI + "\n", stderr="")

    assert totp.decode_qr("/img.png", runner=runner, which=lambda _: "/usr/bin/zbarimg") == URI
    assert calls == [["zbarimg", "--quiet", "--raw", "/img.png"]]


def test_decode_qr_errors_cleanly():
    with pytest.raises(totp.TotpError, match="zbarimg"):
        totp.decode_qr("/img.png", which=lambda _: None)

    def no_code(argv, **kw):
        return subprocess.CompletedProcess(argv, 4, stdout="", stderr="")

    with pytest.raises(totp.TotpError, match="no otpauth"):
        totp.decode_qr("/img.png", runner=no_code, which=lambda _: "/usr/bin/zbarimg")


# --- resolver: otp() and the sink refusal ------------------------------------------

def _add_totp(stack, name="gh-2fa", sm_name="dostal-gh-2fa", state="enabled", value=URI):
    stack["registry"].add(name, sm_name, state=state, secret_type="totp")
    stack["backend"].set(sm_name, totp.normalize(value))


def test_otp_returns_code_only_and_audits(stack):
    _add_totp(stack)
    result = stack["resolver"].otp("gh-2fa", now=1111111111)
    expected = totp.code_at(totp.parse(URI), 1111111111)
    assert result == {"code": expected, "seconds_left": 30 - (1111111111 % 30),
                      "period": 30, "digits": 6}
    _assert_never_seen(json.dumps(result))
    issued = [e for e in stack["audit"].entries() if e["action"] == "otp"]
    assert [(e["secret"], e["result"]) for e in issued] == [("dostal-gh-2fa", "issued")]


def test_every_code_is_audited(stack):
    _add_totp(stack)
    for _ in range(3):
        stack["resolver"].otp("gh-2fa")
    assert sum(1 for e in stack["audit"].entries()
               if e["action"] == "otp" and e["result"] == "issued") == 3


def test_otp_respects_lifecycle_state(stack):
    _add_totp(stack, state="dropped")
    with pytest.raises(NotInjectable):
        stack["resolver"].otp("gh-2fa")
    assert not any(e["action"] == "otp" for e in stack["audit"].entries())


def test_otp_per_use_gate(stack):
    _add_totp(stack)
    stack["broker"].gate("gh-2fa")
    with pytest.raises(ApprovalRequired):
        stack["resolver"].otp("gh-2fa")
    stack["broker"].approve("gh-2fa", ttl=1)
    assert stack["resolver"].otp("gh-2fa")["code"]
    with pytest.raises(ApprovalRequired):  # the one-tick approval is spent
        stack["resolver"].otp("gh-2fa")


def test_otp_per_session_gate(stack):
    _add_totp(stack)
    stack["broker"].gate("gh-2fa")
    stack["broker"].approve("gh-2fa", ttl_seconds=7200)
    for _ in range(5):
        assert stack["resolver"].otp("gh-2fa")["code"]


def test_otp_refuses_a_non_totp_reference(stack):
    stack["registry"].add("plain", "dostal-plain", state="enabled")
    stack["backend"].set("dostal-plain", "hunter2-hunter2")
    with pytest.raises(NotTotp):
        stack["resolver"].otp("plain")


def test_otp_unreadable_seed_never_echoes_it(stack):
    stack["registry"].add("bad", "dostal-bad", state="enabled", secret_type="totp")
    stack["backend"].set("dostal-bad", "otpauth://totp/x?secret=!!!" + SEED)
    with pytest.raises(totp.TotpError) as exc:
        stack["resolver"].otp("bad")
    _assert_never_seen(str(exc.value))
    assert any(e["result"] == "error:unreadable-seed" for e in stack["audit"].entries())


def test_totp_never_resolves_into_any_sink(stack):
    _add_totp(stack)
    resolver = stack["resolver"]
    with pytest.raises(TotpNotResolvable):
        resolver.resolve_to_tempfile("{{secret:gh-2fa}}")
    with pytest.raises(TotpNotResolvable):
        resolver.resolve_exec(["echo", "{{secret:gh-2fa}}"], runner=lambda argv: argv)
    seen = []
    with pytest.raises(TotpNotResolvable):
        resolver.resolve_call("{{secret:gh-2fa}}", seen.append)
    assert seen == []
    # TotpNotResolvable is a NotInjectable, so every existing sink's error
    # handling already catches it.
    assert issubclass(TotpNotResolvable, NotInjectable)
    assert any(e["result"] == "denied-totp-seed" for e in stack["audit"].entries())


def test_refused_resolve_does_not_spend_an_approval(stack):
    _add_totp(stack)
    stack["broker"].gate("gh-2fa")
    stack["broker"].approve("gh-2fa", ttl_seconds=60)
    with pytest.raises(TotpNotResolvable):
        stack["resolver"].resolve_to_tempfile("{{secret:gh-2fa}}")
    assert stack["resolver"].otp("gh-2fa")["code"]


# --- recovery codes -------------------------------------------------------------------

def test_recovery_code_burns_after_one_resolve(stack):
    stack["registry"].add("gh-recovery-1", "dostal-gh-recovery-1", state="enabled",
                          secret_type="recovery_code")
    stack["backend"].set("dostal-gh-recovery-1", "abcde-12345")
    seen = []
    stack["resolver"].resolve_call("{{secret:gh-recovery-1}}", seen.append)
    assert seen == ["abcde-12345"]
    assert stack["registry"].get("gh-recovery-1").state == "used"
    with pytest.raises(NotInjectable, match="already handed out"):
        stack["resolver"].resolve_call("{{secret:gh-recovery-1}}", seen.append)
    assert seen == ["abcde-12345"]
    assert any(e["action"] == "recovery_code" and e["result"] == "used"
               for e in stack["audit"].entries())


def test_unknown_secret_type_rejected(stack):
    with pytest.raises(ValueError):
        stack["registry"].add("x", "dostal-x", secret_type="hotp")


# --- leak-scan --------------------------------------------------------------------------

def test_leak_scan_searches_for_the_seed(stack, tmp_path):
    from portunus.leakscan import get_values, scan_paths

    _add_totp(stack)
    values = get_values(stack["registry"], stack["broker"], stack["backend"])
    assert values["gh-2fa"] == SEED
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("clean line\n"
                          f'{{"text": "setup key {SEED}"}}\n'
                          f'{{"text": "otpauth://totp/x?secret={SEED}&issuer=X"}}\n')
    findings, _ = scan_paths([str(transcript)], values, {})
    assert [(f.ref_name, f.line_number) for f in findings] == [("gh-2fa", 2), ("gh-2fa", 3)]
    # The scan resolved values directly, not through _fetch: no otp issued,
    # no refusal logged.
    assert not any(e["action"] == "otp" for e in stack["audit"].entries())


# --- CLI ------------------------------------------------------------------------------------

@pytest.fixture
def local(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def _drop_totp(monkeypatch, capsys, value=URI, extra=()):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(value + "\n"))
    rc = main(["drop", "gh-2fa", "dostal-gh-2fa", "--type", "totp", "--stdin", *extra])
    return rc, capsys.readouterr()


def test_cli_drop_totp_and_otp_never_print_the_seed(home, local, monkeypatch, capsys):
    rc, out = _drop_totp(monkeypatch, capsys)
    assert rc == 0, out.err
    assert "dropped totp gh-2fa" in out.out and "GitHub/octo@example.com" in out.out
    _assert_never_seen(out.out, out.err)
    stored = LocalEncryptedBackend().access("dostal-gh-2fa")
    assert totp.parse(stored).secret == b"portunus-totp-seed!!"

    assert main(["state", "gh-2fa", "enabled"]) == 0
    capsys.readouterr()
    assert main(["otp", "gh-2fa"]) == 0
    text = capsys.readouterr()
    code, rest = text.out.split(None, 1)
    assert code.isdigit() and len(code) == 6 and "s left" in rest
    _assert_never_seen(text.out, text.err)

    assert main(["otp", "gh-2fa", "--json"]) == 0
    js = capsys.readouterr()
    assert set(json.loads(js.out)) == {"code", "seconds_left", "period", "digits"}
    _assert_never_seen(js.out, js.err)

    assert main(["status", "gh-2fa"]) == 0
    st = capsys.readouterr()
    assert "secret-type:   totp" in st.out
    _assert_never_seen(st.out, st.err)
    _assert_no_leak_on_disk(home)


def test_cli_drop_bare_seed(home, local, monkeypatch, capsys):
    rc, out = _drop_totp(monkeypatch, capsys, value=SEED.lower())
    assert rc == 0, out.err
    _assert_never_seen(out.out, out.err)


def test_cli_drop_invalid_totp_stores_nothing(home, local, monkeypatch, capsys):
    rc, out = _drop_totp(monkeypatch, capsys, value="otpauth://totp/x?secret=" + SEED + "&digits=3")
    assert rc != 0 and "nothing dropped" in out.err
    _assert_never_seen(out.out, out.err)
    assert main(["status", "gh-2fa"]) != 0


def test_cli_resolve_and_inject_refuse_totp(home, local, monkeypatch, capsys):
    _drop_totp(monkeypatch, capsys, extra=("--tags", "site=github"))
    main(["state", "gh-2fa", "enabled"])
    capsys.readouterr()
    assert main(["resolve", "{{secret:gh-2fa}}"]) != 0
    res = capsys.readouterr()
    assert "portunus otp gh-2fa" in res.err
    _assert_never_seen(res.out, res.err)

    target = home / "out.env"
    rc = main(["inject", "--tags", "site=github", "--target", "file",
               "--path", str(target), "--format", "env", "--key", "OTP"])
    inj = capsys.readouterr()
    assert rc != 0
    assert not target.exists()
    _assert_never_seen(inj.out, inj.err)
    _assert_no_leak_on_disk(home)


def test_cli_otp_on_plain_secret_errors(home, local, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("hunter2-hunter2\n"))
    main(["drop", "plain", "dostal-plain", "--stdin"])
    main(["state", "plain", "enabled"])
    capsys.readouterr()
    assert main(["otp", "plain"]) != 0
    assert "not a totp secret" in capsys.readouterr().err


def test_cli_drop_qr(home, local, monkeypatch, capsys):
    image = home / "qr.png"
    image.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(totp.shutil, "which", lambda cmd: "/usr/bin/zbarimg")
    monkeypatch.setattr(totp.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=URI + "\n", stderr=""))
    assert main(["drop", "gh-2fa", "dostal-gh-2fa", "--qr", str(image)]) == 0
    out = capsys.readouterr()
    assert "dropped totp gh-2fa" in out.out
    _assert_never_seen(out.out, out.err)
    assert totp.parse(LocalEncryptedBackend().access("dostal-gh-2fa")).issuer == "GitHub"
    _assert_no_leak_on_disk(home)


def test_cli_drop_qr_rejects_other_type(home, local, capsys):
    rc = main(["drop", "x", "dostal-x", "--qr", "/nope.png", "--type", "recovery-code"])
    assert rc != 0


def test_cli_drop_recovery_code_is_gated(home, local, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("  abcde-12345  \n"))
    assert main(["drop", "gh-rc-1", "dostal-gh-rc-1", "--type", "recovery-code", "--stdin"]) == 0
    out = capsys.readouterr()
    assert "abcde-12345" not in out.out + out.err
    assert LocalEncryptedBackend().access("dostal-gh-rc-1") == "abcde-12345"
    main(["state", "gh-rc-1", "enabled"])
    capsys.readouterr()
    assert main(["resolve", "{{secret:gh-rc-1}}"]) != 0
    assert "needs approval" in capsys.readouterr().err


# --- prompt (secure human entry) --------------------------------------------------------------

class FakeRunner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        rc, out = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def _use_runner(monkeypatch, runner):
    monkeypatch.setattr(prompt_mod.subprocess, "run", runner)
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: ["kdialog"])


def test_cli_prompt_totp(home, local, monkeypatch, capsys):
    runner = FakeRunner((0, URI + "\n"))
    _use_runner(monkeypatch, runner)
    rc = main(["prompt", "gh-2fa", "dostal-gh-2fa", "--type", "totp"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    result = json.loads(out.out)
    assert result["stored"] is True and result["secret_type"] == "totp"
    _assert_never_seen(out.out, out.err)
    # The dialog argv carries the request text, never a value.
    assert SEED not in " ".join(runner.calls[0][0])
    _assert_no_leak_on_disk(home)


def test_cli_prompt_invalid_totp_stores_nothing(home, local, monkeypatch, capsys):
    _use_runner(monkeypatch, FakeRunner((0, "not-a-seed\n")))
    rc = main(["prompt", "gh-2fa", "dostal-gh-2fa", "--type", "totp"])
    out = capsys.readouterr()
    assert rc != 0
    assert "not-a-seed" not in out.out + out.err
    assert main(["status", "gh-2fa"]) != 0


# --- MCP ----------------------------------------------------------------------------------------

def test_mcp_tools_never_return_the_seed(home, local, monkeypatch, capsys):
    from portunus import mcp_server

    _drop_totp(monkeypatch, capsys, extra=("--project", "acct", "--tags", "site=github"))
    main(["state", "gh-2fa", "enabled"])
    capsys.readouterr()

    tool_names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "portunus_otp" in tool_names

    by_name = mcp_server.portunus_otp(name="gh-2fa")
    by_tags = mcp_server.portunus_otp(tags={"site": "github"})
    assert set(by_name) == {"code", "seconds_left", "period", "digits"}
    assert set(by_tags) == set(by_name)

    outputs = [
        by_name, by_tags,
        mcp_server.portunus_list(project="acct"),
        mcp_server.portunus_search(query="gh"),
        mcp_server.portunus_resolve_to_tempfile(name="gh-2fa"),
        mcp_server.portunus_resolve_exec(argv=["echo", "{{secret:gh-2fa}}"], name="gh-2fa"),
        mcp_server.portunus_otp(name="nope"),
    ]
    for out in outputs:
        _assert_never_seen(json.dumps(out, default=str))
    assert "error" in outputs[4] and "error" in outputs[5] and "error" in outputs[6]
    _assert_no_leak_on_disk(home)


def test_mcp_otp_gated(home, local, monkeypatch, capsys):
    from portunus import mcp_server

    _drop_totp(monkeypatch, capsys)
    assert "error" in mcp_server.portunus_otp(name="gh-2fa")  # still dropped
    main(["state", "gh-2fa", "enabled"])
    main(["gate", "gh-2fa"])
    capsys.readouterr()
    assert "needs approval" in mcp_server.portunus_otp(name="gh-2fa")["error"]


def test_mcp_prompt_totp(home, local, monkeypatch):
    from portunus import mcp_server

    _use_runner(monkeypatch, FakeRunner((0, URI + "\n")))
    result = mcp_server.portunus_prompt(name="gh-2fa", sm_name="dostal-gh-2fa",
                                        enable=True, secret_type="totp")
    assert result["stored"] is True and result["secret_type"] == "totp"
    _assert_never_seen(json.dumps(result))
    assert set(mcp_server.portunus_otp(name="gh-2fa")) == {"code", "seconds_left", "period", "digits"}
