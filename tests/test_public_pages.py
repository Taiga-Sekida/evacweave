"""Every page the public service serves must work under its own Content-Security-Policy."""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)
PAGES = ("/", "/slides", "/api-guide", "/lab")
ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "apps" / "web" / "static"


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.inline_style_attrs = 0
        self.inline_handlers: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name in ("href", "src"):
            if values.get(name):
                self.links.append(values[name])
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1
        if "style" in values:
            self.inline_style_attrs += 1
        self.inline_handlers += [n for n in values if n.startswith("on")]


def _refs(path: str) -> _Refs:
    response = client.get(path)
    assert response.status_code == 200
    parser = _Refs()
    parser.feed(response.text)
    return parser


@pytest.mark.parametrize("path", PAGES)
def test_page_needs_nothing_the_csp_blocks(path):
    refs = _refs(path)
    # default-src/script-src/style-src are 'self' only: no CDN, inline script or inline style.
    assert refs.inline_scripts == 0
    assert refs.inline_styles == 0
    assert refs.inline_style_attrs == 0
    assert refs.inline_handlers == []
    for link in refs.links:
        parts = urlsplit(link)
        assert not parts.netloc and parts.scheme in ("", "data"), link


@pytest.mark.parametrize("path", PAGES)
def test_internal_links_resolve(path):
    for link in _refs(path).links:
        parts = urlsplit(link)
        if parts.scheme == "data":
            continue
        target = parts.path or "/"
        assert target.startswith("/"), link
        assert client.get(target).status_code == 200, link


def test_stock_swagger_pages_are_disabled_not_blank():
    # /docs and /redoc pull their UI from a CDN, which the CSP blocks, leaving a blank page.
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 200


def test_api_guide_lists_every_public_route():
    html = client.get("/api-guide").text
    routes = {
        route.path
        for route in app.routes
        if getattr(route, "include_in_schema", False) and route.path.startswith(("/v1/", "/health"))
    }
    assert routes  # guard against the filter silently matching nothing
    for path in routes:
        documented = path.replace("{kind}", "blocked")
        assert path in html or documented in html or path.replace("{kind}", "") in html, path


# Each page and the scripts that fill it (map-view.js is shared by both).
PAGE_SCRIPTS = {
    "index.html": ("map-view.js", "app.js"),
    "play.html": ("map-view.js", "play.js"),
}


@pytest.mark.parametrize(("page", "scripts"), PAGE_SCRIPTS.items())
def test_scenario_figures_come_from_showcase_not_markup(page, scripts):
    # The pages promise every figure is computed by the Core; a literal here would go
    # stale silently as soon as the synthetic scenario changes.
    html = (STATIC / page).read_text(encoding="utf-8")
    assert re.findall(r"\d+\s?[名秒]", html) == []
    assert re.findall(r"road_[a-z0-9]+", html) == []
    for name in scripts:
        script = (STATIC / name).read_text(encoding="utf-8")
        assert re.findall(r"\d+\s?[名秒]", script) == [], name


@pytest.mark.parametrize(("page", "scripts"), PAGE_SCRIPTS.items())
def test_script_only_targets_elements_the_page_has(page, scripts):
    # The scripts fill the page by id and data-bind; a renamed or removed element would
    # otherwise fail only at runtime, in the browser, with a blank panel.
    html = (STATIC / page).read_text(encoding="utf-8")
    script = "".join((STATIC / name).read_text(encoding="utf-8") for name in scripts)
    ids = set(re.findall(r'getElementById\("([\w-]+)"\)', script))
    ids |= set(re.findall(r'\$\("([\w-]+)"\)', script))
    binds = set(re.findall(r'\bbind\("([\w-]+)",', script))
    assert ids  # guard against the regexes silently matching nothing
    assert {i for i in ids if f'id="{i}"' not in html} == set()
    assert {b for b in binds if f'data-bind="{b}"' not in html} == set()


def test_root_is_the_play_page_and_old_slide_links_still_work():
    assert 'id="play-map"' in client.get("/").text
    assert 'id="shot-2"' in client.get("/slides").text
    for asked, target in (("/?shot=3", "/slides?shot=3"), ("/?shot=evil", "/slides"), ("/?shot=", "/slides")):
        response = client.get(asked, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (307, target)


def test_every_engine_rejection_reason_has_a_japanese_label():
    engine = (ROOT / "packages" / "simulation" / "engine.py").read_text(encoding="utf-8")
    reasons = set(re.findall(r'reject\(time_s, intent, "([a-z_]+)"\)', engine))
    reasons |= set(re.findall(r'return clock, "([a-z_]+)"', engine))
    assert len(reasons) >= 12  # guard against the regexes silently matching nothing
    script = (STATIC / "map-view.js").read_text(encoding="utf-8")
    block = script.split("const reasonNames = {", 1)[1].split("};", 1)[0]
    labelled = set(re.findall(r'^\s+([a-z_]+): "', block, re.M))
    assert reasons <= labelled, sorted(reasons - labelled)


def _js_names(name):
    script = (STATIC / "map-view.js").read_text(encoding="utf-8")
    block = script.split("const " + name + " = {", 1)[1].split("};", 1)[0]
    return dict(re.findall(r'^\s+([a-z_0-9]+): "([^"]+)"', block, re.M))


def test_names_gemini_reads_against_match_the_map():
    from apps.api.labels import NODE_NAMES, ROAD_NAMES, road_labels
    from apps.api.main import _reviewed_inputs

    assert _js_names("roadNames") == ROAD_NAMES
    assert _js_names("nodeNames") == NODE_NAMES
    for key in ("base", "river_district"):
        scenario, _ = _reviewed_inputs(key)
        assert [label.id for label in road_labels(scenario)] == [road.id for road in scenario.roads]


def test_no_script_writes_html_from_strings():
    # Server answers, residents' notes and reports are shown as text nodes only, so
    # nothing they contain can become markup or a script (XSS).
    for script in sorted((ROOT / "apps" / "web" / "static").glob("*.js")) + sorted(
        (ROOT / "apps" / "web" / "static").glob("*.mjs")
    ):
        text = script.read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert sink not in text, (script.name, sink)


def test_scripts_may_be_cached_briefly_but_pages_and_answers_never():
    from fastapi.testclient import TestClient

    from apps.api.main import app

    web = TestClient(app)
    assert web.get("/assets/play.js").headers["Cache-Control"] == "public, max-age=300"
    assert web.get("/").headers["Cache-Control"] == "no-store"
    assert web.get("/v1/agent/live").headers["Cache-Control"] == "no-store"


def test_unknown_scenario_names_never_reach_a_cache():
    from fastapi.testclient import TestClient

    import apps.api.main as main

    web = TestClient(main.app)
    assert web.get("/v1/agent/care/sweep").status_code == 200
    before = (main._compute_care_sweep.cache_info().currsize, main._load_care_set.cache_info().currsize)
    for name in ("a", "b", "c", "d", "e"):
        assert web.get(f"/v1/agent/care/sweep?scenario={name}").status_code == 404
        assert web.get(f"/v1/agent/care?scenario={name}").status_code == 404
        assert web.post("/v1/agent/challenge", json={"scenario": name}).status_code == 422
    after = (main._compute_care_sweep.cache_info().currsize, main._load_care_set.cache_info().currsize)
    assert after == before and main._load_reviewed_inputs.cache_info().currsize <= 2
