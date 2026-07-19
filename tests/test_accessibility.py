"""Accessibility guard for the dashboard. Static structural checks — no browser —
so new UI that skips the bar fails here. Covers what regresses in practice: a chart
or control shipped without a screen-reader name, the tab pattern coming unwired, or
the document losing lang/focus/motion affordances. It can't judge contrast or
runtime state; that stays a human review concern."""

from html.parser import HTMLParser
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
VOID = {"meta", "img", "input", "br", "hr", "link", "area", "base", "col",
        "embed", "source", "track", "wbr"}


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        rec = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(rec)
        if tag not in VOID:
            self._stack.append(rec)

    def handle_endtag(self, tag):
        if self._stack and self._stack[-1]["tag"] == tag:
            self._stack.pop()

    def handle_data(self, data):
        for rec in self._stack:  # text bubbles to every open ancestor
            rec["text"] += data


def elements():
    c = Collector()
    c.feed(HTML)
    return c.elements


def draw_args(canvas_id):
    """The paren-balanced argument string of draw($("<id>"), …), or None."""
    start = HTML.find(f'draw($("{canvas_id}")')
    if start < 0:
        return None
    i = HTML.index("(", start)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "(":
            depth += 1
        elif HTML[j] == ")":
            depth -= 1
            if depth == 0:
                return HTML[i + 1:j]
    return None


def test_document_has_lang():
    html_el = next(e for e in elements() if e["tag"] == "html")
    assert html_el["attrs"].get("lang")


def test_viewport_meta_present():
    assert 'name="viewport"' in HTML


def test_focus_visible_ring():
    assert ":focus-visible" in HTML and "outline" in HTML


def test_reduced_motion_honored():
    assert "prefers-reduced-motion" in HTML


def test_dark_and_light_both_present():
    assert "prefers-color-scheme: dark" in HTML and "color-scheme: light" in HTML


def test_tab_pattern_is_wired():
    els = elements()
    assert [e for e in els if e["attrs"].get("role") == "tablist"], "tab nav needs role=tablist"
    panels = {e["attrs"].get("id") for e in els if e["attrs"].get("role") == "tabpanel"}
    # every tab-bar button (they carry data-tab) must be a fully wired tab — so a
    # button that loses its role can't just drop out of the checked set
    tab_buttons = [e for e in els if e["tag"] == "button" and "data-tab" in e["attrs"]]
    assert len(tab_buttons) >= 2, "expected the tab bar's buttons"
    for t in tab_buttons:
        assert t["attrs"].get("role") == "tab", "each tab-bar button needs role=tab"
        assert "aria-selected" in t["attrs"], "each tab needs aria-selected"
        assert t["attrs"].get("aria-controls") in panels, "tab must point at a real tabpanel"


def test_static_controls_have_accessible_names():
    for e in elements():
        if e["tag"] == "button" or e["attrs"].get("role") in ("tab", "button"):
            name = e["text"].strip() or (e["attrs"].get("aria-label") or "").strip()
            assert name, f"control without an accessible name: {e['attrs']}"


def test_every_chart_is_labelled():
    canvases = [e for e in elements() if e["tag"] == "canvas"]
    assert canvases, "expected chart canvases"
    for c in canvases:
        cid = c["attrs"].get("id")
        if c["attrs"].get("aria-label"):
            continue  # statically labelled is fine
        args = draw_args(cid)
        assert args is not None, f"canvas #{cid} is never drawn and has no aria-label"
        assert "name:" in args, (
            f"canvas #{cid} is drawn without a name — screen readers get nothing. "
            f"pass {{ name: '…' }} to draw() or set a static aria-label")
