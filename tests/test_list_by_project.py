"""Registry.list_by_project() -- metadata-only browse, zero-to-many, never a value."""
from portunus import Registry


def test_list_by_project_filters_by_project(home):
    reg = Registry()
    reg.add("a", "sm-a", provider="gcp", project="demo-project-483920", description="Auth secret")
    reg.add("b", "sm-b", provider="gcp", project="demo-project-483920", description="Billing key")
    reg.add("c", "sm-c", provider="gcp", project="firefly-events-inc")

    matches = reg.list_by_project("demo-project-483920")
    assert sorted(r.name for r in matches) == ["a", "b"]


def test_list_by_project_zero_matches_returns_empty_not_error(home):
    reg = Registry()
    reg.add("a", "sm-a", project="other-project")
    assert reg.list_by_project("nonexistent-project") == []


def test_list_by_project_filters_by_provider_and_env(home):
    reg = Registry()
    reg.add("a", "sm-a", provider="gcp", project="p", env="prod")
    reg.add("b", "sm-b", provider="aws", project="p", env="prod")
    reg.add("c", "sm-c", provider="gcp", project="p", env="staging")

    assert [r.name for r in reg.list_by_project("p", provider="gcp")] == ["a", "c"]
    assert [r.name for r in reg.list_by_project("p", provider="gcp", env="prod")] == ["a"]


def test_list_by_project_never_touches_a_backend(home):
    """Structural: list_by_project has no way to reach a value -- it's a pure
    registry read, same call shape as resolve_by_tags. Checked at the AST
    level (not the docstring) so descriptive prose can't false-positive."""
    import ast
    import inspect
    import textwrap
    from portunus.registry import Registry as R
    src = textwrap.dedent(inspect.getsource(R.list_by_project))
    tree = ast.parse(src)
    func = tree.body[0]
    body_without_docstring = func.body[1:] if ast.get_docstring(func) else func.body
    code_src = "\n".join(ast.unparse(node) for node in body_without_docstring)
    assert "backend" not in code_src
    assert ".access(" not in code_src
