"""One release version everywhere (PANT-850): pyproject.toml, `__version__` (what
`portunus --version` and the self-update check report), manifest.json, and the Tauri
desktop shell, which is kept in lockstep with the Python package. Also pins manifest.json's
install instruction to the README's installer one-liner -- `pipx install portunus` would
fetch an unrelated PyPI project (docs/architecture.md, PyPI naming)."""
import json
import re
from pathlib import Path

import pytest

import portunus
from portunus.cli import main

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version():
    text = (ROOT / "pyproject.toml").read_text()
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    m = re.search(r'^version\s*=\s*"([^"]+)"', project, re.M)
    assert m, "pyproject.toml [project] has no version"
    return m.group(1)


def _manifest():
    return json.loads((ROOT / "manifest.json").read_text())


def test_dunder_version_matches_pyproject():
    assert portunus.__version__ == _pyproject_version()


def test_manifest_version_matches_pyproject():
    assert _manifest()["version"] == _pyproject_version()


def test_cli_version_flag_reports_pyproject_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"portunus {_pyproject_version()}"


def test_tauri_shell_version_matches_pyproject():
    want = _pyproject_version()
    tauri = ROOT / "ui" / "src-tauri"
    assert json.loads((tauri / "tauri.conf.json").read_text())["version"] == want
    cargo = (tauri / "Cargo.toml").read_text().split("[package]", 1)[1].split("\n[", 1)[0]
    assert re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.M).group(1) == want
    lock = (tauri / "Cargo.lock").read_text()
    m = re.search(r'name = "portunus-desktop"\nversion = "([^"]+)"', lock)
    assert m and m.group(1) == want


def test_changelog_has_a_section_for_the_current_version():
    assert f"## [{_pyproject_version()}]" in (ROOT / "CHANGELOG.md").read_text()


def test_manifest_install_is_the_readme_installer_one_liner():
    install = _manifest()["engine"]["install"]
    assert install == "curl -fsSL https://mdostal.github.io/portunus/install.sh | bash"
    assert install in (ROOT / "README.md").read_text()
    assert (ROOT / "scripts" / "install.sh").is_file()
