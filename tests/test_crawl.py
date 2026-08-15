"""portunus crawl -- discovery/context-bundling (portunus-metadata-crawl
Slice 1). A discovery tool, not a writer: never mutates a Reference, never
touches a value."""
import ast
import inspect

from portunus import Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.crawl import crawl_candidates
from portunus.rotation import RotationBinding, save_rotation_bindings


def test_incomplete_reference_is_a_candidate(home):
    reg = Registry()
    reg.add("x", "sm-x", project="shindig")  # no description/purpose/org/tags
    candidates = crawl_candidates(reg)
    names = [c["name"] for c in candidates]
    assert "x" in names


def test_fully_filled_reference_is_not_a_candidate(home):
    reg = Registry()
    reg.add(
        "x", "sm-x", org="firefly-events", project="shindig",
        description="a stripe key", purpose="billing",
    )
    candidates = crawl_candidates(reg)
    names = [c["name"] for c in candidates]
    assert "x" not in names


def test_candidate_bundles_known_context(home):
    reg = Registry()
    reg.add(
        "x", "SHINDIG_API_KEY", org="firefly-events", project="shindig",
        group="shindig/api", repo="shindig-web",
    )
    candidates = crawl_candidates(reg)
    c = candidates[0]
    assert c["sm_name"] == "SHINDIG_API_KEY"
    assert c["group"] == "shindig/api"
    assert c["org"] == "firefly-events"
    assert c["repo"] == "shindig-web"
    assert c["missing"]["description"] is True
    assert c["missing"]["purpose"] is True
    assert c["missing"]["org_or_project_or_tags"] is False  # org is set


def test_candidate_bundles_vault_binding_when_configured(home):
    save_vault_bindings({"shindig": VaultBinding("shindig", backend="gcp", sync_mode="cached")})
    reg = Registry()
    reg.add("x", "sm-x", project="shindig")
    candidates = crawl_candidates(reg)
    assert candidates[0]["vault_binding"] == {
        "backend": "gcp", "sync_mode": "cached", "account": "", "wif_audience": "",
    }


def test_candidate_has_no_vault_binding_when_unconfigured(home):
    reg = Registry()
    reg.add("x", "sm-x", project="shindig")
    candidates = crawl_candidates(reg)
    assert candidates[0]["vault_binding"] is None


def test_candidate_bundles_rotation_binding_when_configured(home):
    save_rotation_bindings({"vercel": RotationBinding("vercel", status="stub", account="acme-team")})
    reg = Registry()
    reg.add("x", "sm-x", project="shindig")
    from portunus.registry import Registry as R
    R().retag("x", provider="vercel")
    candidates = crawl_candidates(Registry())
    assert candidates[0]["rotation_binding"] == {"status": "stub", "account": "acme-team"}


def test_org_filter_scopes_candidates(home):
    reg = Registry()
    reg.add("a", "sm-a", org="firefly-events", project="shindig")
    reg.add("b", "sm-b", org="other-org", project="gig-tracker")
    candidates = crawl_candidates(reg, org="firefly-events")
    names = [c["name"] for c in candidates]
    assert names == ["a"]


def test_project_filter_scopes_candidates(home):
    reg = Registry()
    reg.add("a", "sm-a", project="shindig")
    reg.add("b", "sm-b", project="gig-tracker")
    candidates = crawl_candidates(reg, project="shindig")
    names = [c["name"] for c in candidates]
    assert names == ["a"]


def test_crawl_never_mutates_the_registry(home):
    reg = Registry()
    reg.add("x", "sm-x", project="shindig")
    crawl_candidates(reg)
    reloaded = Registry().require("x")
    assert reloaded.description == ""
    assert reloaded.purpose == ""


def test_crawl_candidates_never_touches_a_value_source_check():
    """Structural: crawl_candidates() has no .access( call anywhere -- it
    is, like discover.py, structurally incapable of fetching a value."""
    src = inspect.getsource(crawl_candidates)
    tree = ast.parse(src)
    code = ast.unparse(tree)
    assert ".access(" not in code
