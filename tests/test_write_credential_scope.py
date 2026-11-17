"""The UI's write credential (ui/lib/write-auth.ts, first-cut-trust t1c) guards
the HTTP routes only. The host CLI and `portunus mcp` (stdio) change the vault
in-process, straight through Registry/Broker, so they need no credential and
keep working unchanged. This pins that: if either ever starts writing through
the UI's HTTP API, it would need the credential too, and this test says so.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "portunus"
UI_API = ROOT / "ui" / "app" / "api"


def _ui_routes() -> list[str]:
    return sorted(p.parent.name for p in UI_API.glob("*/route.ts"))


def test_the_ui_route_list_is_found():
    routes = _ui_routes()
    assert {"inject", "drop", "retag"} <= set(routes)


def test_cli_and_mcp_never_call_the_ui_http_api():
    pattern = re.compile(r"/api/(" + "|".join(map(re.escape, _ui_routes())) + r")\b")
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line) and "shallow=1" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "the CLI/MCP now reaches the UI's HTTP API; send X-Portunus-Write-Token "
        "on writes and update docs/http-routes.md:\n" + "\n".join(offenders)
    )


def test_the_ui_url_is_only_ever_opened_in_a_browser():
    # PORTUNUS_UI_URL is read by `portunus ui open` / `prompt --surface ui`
    # (both through _open_ui), which hand the URL to a browser and never send
    # a request through it.
    reads = re.compile(r"environ(\.get\(|\[)\s*[\"']PORTUNUS_UI_URL")
    hits = [(p, m.start()) for p in SRC.rglob("*.py") for m in reads.finditer(p.read_text())]
    assert [p.name for p, _ in hits] == ["cli.py"]
    cli = (SRC / "cli.py").read_text()
    open_ui = cli.index("def _open_ui(")
    next_def = cli.index("\ndef ", open_ui + 1)
    assert all(open_ui < at < next_def for _, at in hits)
