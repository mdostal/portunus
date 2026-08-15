"""`portunus report` (portunus-metadata-crawl Slice 2)."""
from portunus import Registry
from portunus.cli import main


def test_report_prints_to_stdout(home, capsys):
    Registry().add("x", "sm-x", org="firefly-events", project="shindig", description="a key")
    rc = main(["report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# Portunus Vault Report" in out
    assert "**x**" in out


def test_report_writes_to_file(home, capsys, tmp_path):
    Registry().add("x", "sm-x", org="firefly-events", project="shindig", description="a key")
    out_path = tmp_path / "report.md"
    rc = main(["report", "--out", str(out_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "wrote report" in out
    assert out_path.exists()
    content = out_path.read_text()
    assert "**x**" in content


def test_report_scoped_by_org(home, capsys):
    Registry().add("a", "sm-a", org="firefly-events", project="shindig", description="x", purpose="y")
    Registry().add("b", "sm-b", org="other-org", project="gig-tracker", description="x", purpose="y")
    rc = main(["report", "--org", "firefly-events"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "**a**" in out
    assert "**b**" not in out
