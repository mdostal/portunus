"""portunus find --tags — CLI consumer of resolve_by_tags() (story 02)."""
from portunus.cli import main


def test_find_exact_match_prints_metadata_and_exits_zero(home, capsys):
    main(["reg", "add", "vercel-mdostal", "sm-vercel-mdostal"])
    capsys.readouterr()
    # Tag the reference directly via the registry API used by cli.py's _build().
    from portunus import Registry
    reg = Registry()
    reg.add("vercel-mdostal", "sm-vercel-mdostal", provider="vercel", project="mdostal.com", env="prod")

    rc = main(["find", "--tags", "provider=vercel,project=mdostal.com"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "vercel-mdostal" in out
    assert "prod" in out


def test_find_no_match_exits_nonzero_distinct_code(home, capsys):
    rc = main(["find", "--tags", "provider=aws"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "no match" in err.lower() or "no reference" in err.lower()


def test_find_ambiguous_prints_every_candidate_and_distinct_exit_code(home, capsys):
    from portunus import Registry
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")

    rc = main(["find", "--tags", "provider=vercel,project=mdostal.com"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "a" in err and "b" in err


def test_find_no_match_and_ambiguous_use_different_exit_codes(home, capsys):
    from portunus import Registry
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel")
    reg.add("b", "sm-b", provider="vercel")

    rc_ambiguous = main(["find", "--tags", "provider=vercel"])
    capsys.readouterr()
    rc_no_match = main(["find", "--tags", "provider=nope"])
    capsys.readouterr()

    assert rc_ambiguous != rc_no_match
    assert rc_ambiguous != 0 and rc_no_match != 0


def test_find_does_not_construct_a_resolver_or_touch_a_backend(home, capsys, monkeypatch):
    """find is metadata-only: it must not build a Resolver/backend at all."""
    from portunus import Registry
    import portunus.cli as cli_mod

    reg = Registry()
    reg.add("x", "sm-x", provider="vercel")

    def _boom(*a, **k):
        raise AssertionError("find must not construct a Resolver/backend")

    monkeypatch.setattr(cli_mod, "Resolver", _boom)

    rc = main(["find", "--tags", "provider=vercel"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "x" in out
