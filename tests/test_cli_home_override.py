"""--home global CLI flag: explicit, per-invocation vault targeting (story 05,
portunus-agent-ops-federation). Not automatic multi-vault federation -- just
an override for which single vault this one invocation reads/writes."""
import os

from portunus.cli import main


def test_home_flag_targets_a_different_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("PORTUNUS_HOME", raising=False)
    other_vault = tmp_path / "other-vault"

    rc = main(["--home", str(other_vault), "reg", "add", "x", "sm-x"])
    assert rc == 0
    assert (other_vault / "registry.json").exists()


def test_no_home_flag_behavior_unchanged(home, capsys):
    """Regression: omitting --home must be byte-identical to today (reads
    PORTUNUS_HOME set by the `home` fixture)."""
    rc = main(["reg", "add", "x", "sm-x"])
    capsys.readouterr()
    assert rc == 0
    assert (home / "registry.json").exists()


def test_home_flag_wins_over_ambient_portunus_home_env(tmp_path, monkeypatch):
    ambient = tmp_path / "ambient-vault"
    override = tmp_path / "override-vault"
    monkeypatch.setenv("PORTUNUS_HOME", str(ambient))

    rc = main(["--home", str(override), "reg", "add", "x", "sm-x"])
    assert rc == 0
    assert (override / "registry.json").exists()
    assert not (ambient / "registry.json").exists()


def test_home_override_does_not_leak_into_a_later_invocation_without_it(tmp_path, monkeypatch):
    """The override must not permanently mutate process state -- a later
    command without --home must fall back to the ambient env correctly."""
    ambient = tmp_path / "ambient-vault"
    override = tmp_path / "override-vault"
    monkeypatch.setenv("PORTUNUS_HOME", str(ambient))

    main(["--home", str(override), "reg", "add", "x", "sm-x"])
    assert os.environ.get("PORTUNUS_HOME") == str(ambient)

    rc = main(["reg", "add", "y", "sm-y"])
    assert rc == 0
    assert (ambient / "registry.json").exists()
    ambient_data = (ambient / "registry.json").read_text()
    assert '"y"' in ambient_data
    assert '"x"' not in ambient_data


def test_home_flag_works_for_find_and_audit_not_just_reg(tmp_path, monkeypatch):
    """Regression guard: every construction site (not just _build()'s)
    must respect --home."""
    monkeypatch.delenv("PORTUNUS_HOME", raising=False)
    other_vault = tmp_path / "other-vault"
    main(["--home", str(other_vault), "reg", "add", "x", "sm-x"])
    main(["--home", str(other_vault), "retag", "x", "--provider", "vercel"])

    rc = main(["--home", str(other_vault), "find", "--tags", "provider=vercel"])
    assert rc == 0

    rc = main(["--home", str(other_vault), "audit", "1"])
    assert rc == 0
