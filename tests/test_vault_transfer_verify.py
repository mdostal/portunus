"""portunus-vault-transfer Story 03: `portunus vault access verify` -- a
real per-reference reachability check via Resolver.resolve_call(), the
same boundary-safe pattern test_boundary_receives_value_but_it_is_not_
returned already proves never leaks. Failures are translated into
actionable hints, never a raw traceback. CLI-only (design-discussion.md
§4) -- not wired here, cli.py's own smoke test covers that."""
import pytest

from portunus.backend import BackendError, VaultBinding
from portunus.vault_transfer import verify_access

SECRET = "FAKE-TEST-VALUE-do-not-leak-0xDEAD"


def test_verify_reports_reachable_for_a_clean_reference(stack):
    stack["registry"].add("a", "sm-a", scope="shared")
    stack["backend"].set("sm-a", SECRET)
    report = verify_access(stack["registry"], stack["resolver"], {})
    assert report["reachable"] == ["a"]
    assert report["needs_drop"] == []
    assert report["needs_auth"] == []


def test_verify_never_leaks_the_value_anywhere_in_its_output(stack, capsys):
    stack["registry"].add("a", "sm-a")
    stack["backend"].set("sm-a", SECRET)
    report = verify_access(stack["registry"], stack["resolver"], {})
    assert SECRET not in repr(report)
    assert SECRET not in str(report)


def test_verify_reports_drop_hint_for_a_requested_reference(stack):
    stack["registry"].add("a", "sm-a", state="requested")
    report = verify_access(stack["registry"], stack["resolver"], {})
    assert report["reachable"] == []
    assert len(report["needs_drop"]) == 1
    entry = report["needs_drop"][0]
    assert entry["name"] == "a"
    assert entry["hint"] == "portunus drop a sm-a --stdin"


def test_verify_reports_actionable_auth_hint_naming_the_bindings_account_and_project(stack):
    """Not a generic 'auth failed' message -- names the binding's own
    account/project in the suggested gcloud command."""
    stack["registry"].add("a", "sm-a", project="p1")  # never .set() on the mock backend -> BackendError
    vault_bindings = {"p1": VaultBinding(project="p1", backend="gcp", account="user@example.com")}
    report = verify_access(stack["registry"], stack["resolver"], vault_bindings)
    assert report["reachable"] == []
    assert len(report["needs_auth"]) == 1
    hint = report["needs_auth"][0]["hint"]
    assert "user@example.com" in hint
    assert "p1" in hint
    assert "gcloud projects add-iam-policy-binding p1" in hint
    assert "portunus auth login user@example.com" in hint


def test_verify_falls_back_to_placeholder_when_no_binding_account_is_known(stack):
    stack["registry"].add("a", "sm-a", project="unconfigured")
    report = verify_access(stack["registry"], stack["resolver"], {})
    assert len(report["needs_auth"]) == 1
    assert "<account>" in report["needs_auth"][0]["hint"]


def test_verify_project_filter_checks_only_that_project(stack):
    stack["registry"].add("a", "sm-a", project="p1")
    stack["registry"].add("b", "sm-b", project="p2")
    stack["backend"].set("sm-a", SECRET)
    stack["backend"].set("sm-b", SECRET)
    report = verify_access(stack["registry"], stack["resolver"], {}, project="p1")
    assert report["reachable"] == ["a"]


def test_verify_no_filter_checks_every_reference(stack):
    stack["registry"].add("a", "sm-a", project="p1")
    stack["registry"].add("b", "sm-b", project="p2")
    stack["backend"].set("sm-a", SECRET)
    stack["backend"].set("sm-b", SECRET)
    report = verify_access(stack["registry"], stack["resolver"], {})
    assert set(report["reachable"]) == {"a", "b"}


def test_verify_at_scale_one_bad_reference_never_aborts_the_batch(stack):
    for i in range(300):
        name = f"ref-{i}"
        sm = f"sm-{i}"
        if i % 10 == 0:
            stack["registry"].add(name, sm, state="requested")  # deliberately broken
        else:
            stack["registry"].add(name, sm)
            stack["backend"].set(sm, SECRET)
    report = verify_access(stack["registry"], stack["resolver"], {})
    assert len(report["reachable"]) == 270
    assert len(report["needs_drop"]) == 30
    assert len(report["needs_auth"]) == 0


# --- structural guard: the boundary ignores its own argument entirely ---------

def test_verify_boundary_structurally_ignores_its_own_argument():
    """The one hard invariant this story exists to prove: the boundary
    callable never constructs its return value FROM the resolved value it
    receives -- always the same literal, regardless of input."""
    import ast
    import inspect
    from portunus.vault_transfer import _reachable_boundary

    tree = ast.parse(inspect.getsource(_reachable_boundary))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    arg_names = {a.arg for a in func.args.args}
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id in arg_names:
            pytest.fail(f"boundary references its own argument {node.id!r} -- must ignore it entirely")
    ret = func.body[-1]
    assert isinstance(ret, ast.Return)
    assert isinstance(ret.value, ast.Constant)
    assert isinstance(ret.value.value, str)


def test_vault_transfer_verify_path_never_imports_secret_backend_machinery():
    import ast
    import inspect
    import portunus.vault_transfer as vt

    tree = ast.parse(inspect.getsource(vt))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
    forbidden = {"LocalEncryptedBackend", "GcloudBackend", "SecretBackend", "Broker"}
    assert not (imported_names & forbidden)
