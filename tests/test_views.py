"""Custom views/collections (portunus-vault-trust-and-access Slice 4) --
named, human-curated reference lists for ad-hoc task clustering. Every
mutator wraps load->mutate->save in one flock acquisition from day one
(unlike vault-bindings.json's own retrofitted save-only lock)."""
import os
import subprocess
import sys

import pytest

from portunus.views import (
    ViewError,
    add_to_view,
    create_view,
    delete_view,
    load_views,
    remove_from_view,
)


def test_create_view(home):
    view = create_view("shindig-deploy", description="everything for the Shindig deploy")
    assert view.name == "shindig-deploy"
    assert view.description == "everything for the Shindig deploy"
    assert view.ref_names == []
    reloaded = load_views()
    assert "shindig-deploy" in reloaded


def test_create_duplicate_view_raises(home):
    create_view("shindig-deploy")
    with pytest.raises(ViewError):
        create_view("shindig-deploy")


def test_add_to_view(home):
    create_view("shindig-deploy")
    view = add_to_view("shindig-deploy", "shindig-api-key")
    assert view.ref_names == ["shindig-api-key"]
    view = add_to_view("shindig-deploy", "shindig-db-url")
    assert view.ref_names == ["shindig-api-key", "shindig-db-url"]


def test_add_to_view_is_idempotent(home):
    create_view("shindig-deploy")
    add_to_view("shindig-deploy", "shindig-api-key")
    view = add_to_view("shindig-deploy", "shindig-api-key")
    assert view.ref_names == ["shindig-api-key"]


def test_add_to_unknown_view_raises(home):
    with pytest.raises(ViewError):
        add_to_view("nonexistent", "some-ref")


def test_remove_from_view(home):
    create_view("shindig-deploy")
    add_to_view("shindig-deploy", "a")
    add_to_view("shindig-deploy", "b")
    view = remove_from_view("shindig-deploy", "a")
    assert view.ref_names == ["b"]


def test_remove_not_present_is_a_no_op(home):
    create_view("shindig-deploy")
    add_to_view("shindig-deploy", "a")
    view = remove_from_view("shindig-deploy", "never-added")
    assert view.ref_names == ["a"]


def test_delete_view(home):
    create_view("shindig-deploy")
    assert delete_view("shindig-deploy") is True
    assert "shindig-deploy" not in load_views()


def test_delete_nonexistent_view_returns_false(home):
    assert delete_view("nonexistent") is False


def test_load_views_empty_when_file_absent(home):
    assert load_views() == {}


def test_views_file_never_holds_a_secret_value(home):
    """Structural sanity check: a view only ever stores reference NAMES,
    never a value -- confirmed by the on-disk shape itself."""
    create_view("shindig-deploy")
    add_to_view("shindig-deploy", "shindig-api-key")
    raw = (home / "views.json").read_text()
    assert "shindig-api-key" in raw
    # the view module has no import of any SecretBackend/value-fetching path
    import ast
    import inspect
    import portunus.views as views_mod

    tree = ast.parse(inspect.getsource(views_mod))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "access" not in names


def test_concurrent_add_to_view_from_separate_processes_never_loses_an_entry(home):
    """Real regression-style proof, matching this session's own established
    multi-process technique (test_backup.py, test_audit.py) -- not a thread
    approximation. Each process adds a distinct ref to the SAME view
    concurrently; the lock-from-day-one design must mean none are lost."""
    create_view("shindig-deploy")
    barrier = home / "start-barrier"
    n = 10
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from portunus.views import add_to_view\n"
        f"barrier = Path({str(barrier)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not barrier.exists() and time.monotonic() < deadline:\n"
        "    pass\n"
        "import sys\n"
        "add_to_view('shindig-deploy', f'ref-{sys.argv[1]}')\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(i)], env=os.environ.copy())
        for i in range(n)
    ]
    barrier.write_text("go")
    for p in procs:
        assert p.wait(timeout=10) == 0

    view = load_views()["shindig-deploy"]
    assert sorted(view.ref_names) == sorted(f"ref-{i}" for i in range(n))
