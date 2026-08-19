"""portunus-petitio-rbac Story 04: a genuinely brand-new PORTUNUS_HOME starts
with enforcement on. An EXISTING vault (this story shipping is not the first
time the directory has ever existed) is never retroactively changed -- that
would be this project's first-ever breaking upgrade behavior.

The only reliable signal for "brand new" is whether the PORTUNUS_HOME
directory itself existed before paths.home() was ever called for it --
NOT whether registry.json/roles-enforce.json exist, since by the time
check_injectable() ever runs, a real registry entry (and therefore
registry.json) already exists. The pytest `home` fixture's `tmp_path` is
always pre-created by pytest itself, so every EXISTING test in this suite
that uses `home` naturally exercises the "existing vault" path already --
this file's own tests use a genuinely not-yet-existing subdirectory to
exercise the "brand new" path."""
from portunus.roles import enforcement_is_on


def test_brand_new_portunus_home_defaults_to_enforcement_on(tmp_path, monkeypatch):
    brand_new = tmp_path / "never-existed-before"
    assert not brand_new.exists()
    monkeypatch.setenv("PORTUNUS_HOME", str(brand_new))
    monkeypatch.delenv("DOSTAL_SECRETS_HOME", raising=False)

    assert enforcement_is_on() is True


def test_existing_portunus_home_enforcement_unaffected(tmp_path, monkeypatch):
    """Simulates a vault that already existed before this story shipped --
    the directory is created FIRST (as if by an earlier portunus version),
    then this story's code runs against it for the first time."""
    pre_existing = tmp_path / "pre-existing-vault"
    pre_existing.mkdir()
    (pre_existing / "registry.json").write_text('{"x": {"sm_name": "sm-x"}}')
    monkeypatch.setenv("PORTUNUS_HOME", str(pre_existing))
    monkeypatch.delenv("DOSTAL_SECRETS_HOME", raising=False)

    assert enforcement_is_on() is False  # unchanged -- never retroactive


def test_existing_home_fixture_directories_stay_default_off(home):
    """Direct proof, not just absence of regressions: the pytest `home`
    fixture's tmp_path is always pre-created by pytest, so it exercises the
    "existing" path -- every other test in this whole suite relying on
    default-off behavior is relying on this, not on luck."""
    assert enforcement_is_on() is False


def test_calling_home_twice_on_a_brand_new_path_does_not_flip_it_back(tmp_path, monkeypatch):
    """The stamp happens exactly once, at first creation -- a human
    explicitly running `roles enforce off` afterward must stick, not get
    silently re-stamped back to on by a later call."""
    from portunus.paths import home
    from portunus.roles import set_enforcement

    brand_new = tmp_path / "never-existed-before"
    monkeypatch.setenv("PORTUNUS_HOME", str(brand_new))
    monkeypatch.delenv("DOSTAL_SECRETS_HOME", raising=False)

    home()  # first call -- stamps on
    assert enforcement_is_on() is True
    set_enforcement(False)  # human explicitly turns it off
    home()  # second call -- directory now exists, must NOT re-stamp
    assert enforcement_is_on() is False
