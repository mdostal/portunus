"""The load-bearing tests: the resolver must inject at the boundary and must
NEVER leak a plaintext value into a return value, the audit log, or disk in a
non-0600 file."""
import os
import stat

import pytest

from portunus import UnknownReference
from portunus.broker import ApprovalRequired, NotInjectable

SECRET = "FAKE-TEST-VALUE-do-not-leak-0xDEAD"


def _register(stack, name="shared-anthropic", sm="dostal-shared-anthropic"):
    stack["registry"].add(name, sm, scope="shared", kind="anthropic")
    stack["backend"].set(sm, SECRET)


def test_find_refs_extracts_names_only(stack):
    text = "a={{secret:shared-anthropic}} b={{secret:shared-openai}} a2={{secret:shared-anthropic}}"
    assert stack["resolver"].find_refs(text) == ["shared-anthropic", "shared-openai"]


def test_boundary_receives_value_but_it_is_not_returned(stack):
    _register(stack)
    seen = {}

    def boundary(resolved: str):
        seen["text"] = resolved
        return "HTTP-200"

    result = stack["resolver"].resolve_call(
        "Authorization: Bearer {{secret:shared-anthropic}}", boundary
    )
    # boundary got the real value...
    assert SECRET in seen["text"]
    # ...but resolve_call returns only the boundary's result, never the value.
    assert result == "HTTP-200"
    assert SECRET not in str(result)


def test_value_never_lands_in_audit_log(stack, home):
    _register(stack)
    stack["resolver"].resolve_call("{{secret:shared-anthropic}}", lambda s: None)
    log_text = (home / "audit.log").read_text()
    assert SECRET not in log_text
    assert "dostal-shared-anthropic" in log_text  # the SM name is fine to log
    # exactly one successful resolve entry
    oks = [e for e in stack["audit"].entries() if e["action"] == "resolve" and e["result"] == "ok"]
    assert len(oks) == 1


def test_resolve_exec_substitutes_argv(stack):
    _register(stack)
    captured = {}
    stack["resolver"].resolve_exec(
        ["curl", "-H", "Authorization: Bearer {{secret:shared-anthropic}}", "https://x"],
        runner=lambda argv: captured.setdefault("argv", argv),
    )
    assert captured["argv"][2] == f"Authorization: Bearer {SECRET}"
    assert captured["argv"][0] == "curl"


def test_resolve_to_tempfile_is_0600_and_holds_value(stack):
    _register(stack)
    path = stack["resolver"].resolve_to_tempfile("key={{secret:shared-anthropic}}")
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        assert open(path).read() == f"key={SECRET}"
    finally:
        os.unlink(path)


def test_unknown_reference_raises_and_fetches_nothing(stack):
    with pytest.raises(UnknownReference):
        stack["resolver"].resolve_call("{{secret:nope}}", lambda s: pytest.fail("boundary ran"))


def test_dropped_reference_fails_closed(stack):
    _register(stack)
    stack["registry"].set_state("shared-anthropic", "dropped")
    with pytest.raises(NotInjectable):
        stack["resolver"].resolve_call("{{secret:shared-anthropic}}", lambda s: pytest.fail("ran"))


def test_gated_reference_requires_approval_then_allows(stack):
    _register(stack)
    stack["broker"].gate("shared-anthropic", on=True)
    with pytest.raises(ApprovalRequired):
        stack["resolver"].resolve_call("{{secret:shared-anthropic}}", lambda s: None)
    # after a human approves, it resolves
    stack["broker"].approve("shared-anthropic", ttl=2)
    seen = {}
    stack["resolver"].resolve_call("{{secret:shared-anthropic}}", lambda s: seen.setdefault("v", s))
    assert seen["v"] == SECRET


def test_no_placeholder_passthrough(stack):
    seen = {}
    stack["resolver"].resolve_call("nothing to see here", lambda s: seen.setdefault("v", s))
    assert seen["v"] == "nothing to see here"


# --- story 02 (portunus-vault-routing): per-reference backend router ----

def test_backend_for_none_is_byte_identical_to_no_router(stack):
    """Default behavior (backend_for omitted) is the exact pre-existing
    code path -- self.backend.access(...) directly."""
    _register(stack)
    from portunus import Resolver
    resolver = Resolver(stack["registry"], stack["backend"], stack["broker"])
    assert resolver.backend_for is None
    seen = {}
    resolver.resolve_call("{{secret:shared-anthropic}}", lambda s: seen.setdefault("v", s))
    assert seen["v"] == SECRET


def test_backend_for_used_when_provided(stack):
    from portunus import Resolver, MockBackend
    _register(stack)
    other = MockBackend()
    other.set("dostal-shared-anthropic", "FROM-OTHER-BACKEND")

    def router(ref):
        return other

    resolver = Resolver(stack["registry"], stack["backend"], stack["broker"], backend_for=router)
    seen = {}
    resolver.resolve_call("{{secret:shared-anthropic}}", lambda s: seen.setdefault("v", s))
    assert seen["v"] == "FROM-OTHER-BACKEND"


def test_backend_for_receives_the_full_reference(stack):
    from portunus import Resolver
    _register(stack)
    seen_refs = []

    def router(ref):
        seen_refs.append(ref)
        return stack["backend"]

    resolver = Resolver(stack["registry"], stack["backend"], stack["broker"], backend_for=router)
    resolver.resolve_call("{{secret:shared-anthropic}}", lambda s: None)
    assert seen_refs[0].name == "shared-anthropic"
