"""portunus report -- Markdown vault-state report generator
(portunus-metadata-crawl Slice 2). Read-only, independent of crawl."""
import ast
import inspect

from portunus import Registry
from portunus.crawl import generate_report


def test_report_renders_org_project_structure(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig", description="a key")
    report = generate_report(reg)
    assert "## firefly-events" in report
    assert "### shindig" in report
    assert "**x**" in report
    assert "description: a key" in report


def test_report_includes_purpose_and_repo_when_set(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig", purpose="billing", repo="shindig-web")
    report = generate_report(reg)
    assert "purpose: billing" in report
    assert "repo: shindig-web" in report


def test_report_gap_section_lists_incomplete_references(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig")
    report = generate_report(reg)
    assert "## Gaps" in report
    assert "**x**: missing description, purpose" in report


def test_report_no_gaps_says_so_explicitly(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig", description="a", purpose="b")
    report = generate_report(reg)
    assert "None -- every reference in scope" in report


def test_report_handles_missing_org_and_project_buckets(home):
    reg = Registry()
    reg.add("orphan", "sm-orphan")
    report = generate_report(reg)
    assert "(no org set)" in report
    assert "(no project set)" in report


def test_report_org_filter_scopes_output(home):
    reg = Registry()
    reg.add("a", "sm-a", org="firefly-events", project="shindig", description="x", purpose="y")
    reg.add("b", "sm-b", org="other-org", project="gig-tracker", description="x", purpose="y")
    report = generate_report(reg, org="firefly-events")
    assert "firefly-events" in report
    assert "other-org" not in report
    assert "**a**" in report
    assert "**b**" not in report


def test_report_project_filter_scopes_output(home):
    reg = Registry()
    reg.add("a", "sm-a", project="shindig", description="x", purpose="y")
    reg.add("b", "sm-b", project="gig-tracker", description="x", purpose="y")
    report = generate_report(reg, project="shindig")
    assert "**a**" in report
    assert "**b**" not in report


def test_report_never_touches_a_value_source_check():
    src = inspect.getsource(generate_report)
    tree = ast.parse(src)
    code = ast.unparse(tree)
    assert ".access(" not in code
