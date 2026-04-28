"""AST guard: forbid innerHTML assignments in dashboard template JS.

docs/SPEC/operational_notes.md §"Dashboard XSS hardening" captures
the rationale. The render path is el()/mount(); every prior
innerHTML= site was converted in the commits before this guard
landed. This test walks each template's <script> block(s), collects
every `.innerHTML` reference, and fails the build on any
assignment-context use.

Read-context uses (e.g. `return div.innerHTML;` in a hypothetical
escape helper) don't match the assignment patterns we forbid, so a
future "compute-the-escape" use would still pass — but the intent
of this guard is to push everyone onto el() and away from
string-building HTML altogether. If that ever feels wrong, adjust
this test with the reason in a comment.

Why regex rather than a real JS parser: introducing an acorn/esprima
dependency to catch one pattern isn't worth it. Everything we need
is lexically obvious — innerHTML followed by assignment operators.
Comments are stripped before matching so the helper's docstring
reference doesn't trip the check.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "trading_strands" / "dashboard" / "templates"
)

# Any of: `x.innerHTML =`, `x.innerHTML +=`. Loose enough to catch
# `foo.innerHTML  =` (extra whitespace) but tight enough that it
# doesn't match `div.innerHTML` appearing in a comment or docstring
# (those are stripped below).
_INNERHTML_ASSIGN_RE = re.compile(r"\.innerHTML\s*(?:\+=|=)(?!=)")

# Pull <script>…</script> blocks from a template. Case-insensitive
# to be forgiving; dotall so newlines inside the block are captured.
# Closing tag uses `\b[^>]*>` (matching anything up to the next `>`)
# rather than `\s*>` — HTML parsers accept whitespace and attributes
# on closing tags, and CodeQL's py/bad-tag-filter correctly flags
# the narrower `\s*>` form as a filter that could be bypassed by
# tabs / newlines / extra content inside the closer.
_SCRIPT_TAG_RE = re.compile(
    r"<script\b[^>]*>(.*?)</script\b[^>]*>",
    re.DOTALL | re.IGNORECASE,
)

# Child templates (index.html, change_password.html, login.html)
# slot their JS into base.html's `{% block scripts %}` rather than
# using a literal <script> tag — the tag wraps the slot in base.
# Capture that form too so the guard inspects every JS context.
_JINJA_SCRIPTS_BLOCK_RE = re.compile(
    r"\{%\s*block\s+scripts\s*%\}(.*?)\{%\s*endblock\s*%\}",
    re.DOTALL,
)

# Strip JS single-line // comments and /* … */ block comments so a
# comment that references "innerHTML" textually doesn't trip the
# assignment-regex.
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(js: str) -> str:
    js = _BLOCK_COMMENT_RE.sub("", js)
    js = _LINE_COMMENT_RE.sub("", js)
    return js


def _extract_scripts(template: str) -> list[tuple[int, str]]:
    """Return [(start_offset_in_template, js_source), ...] for every
    JS block in the template — both literal <script> tags and jinja
    `{% block scripts %}…{% endblock %}` slots that base.html wraps
    in a script tag at render time."""

    out: list[tuple[int, str]] = []
    for m in _SCRIPT_TAG_RE.finditer(template):
        out.append((m.start(1), m.group(1)))
    for m in _JINJA_SCRIPTS_BLOCK_RE.finditer(template):
        out.append((m.start(1), m.group(1)))
    return out


def test_no_inner_html_assignment_in_templates() -> None:
    violations: list[str] = []
    for path in TEMPLATE_DIR.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for offset, js in _extract_scripts(text):
            # Match against the stripped-comments version so a legit
            # comment mentioning `innerHTML` doesn't trip. But report
            # line numbers against the original text by re-finding
            # each match in the unstripped js (same offsets line up
            # as long as we don't report matches that were entirely
            # inside a comment, which is by definition false).
            stripped = _strip_comments(js)
            if not _INNERHTML_ASSIGN_RE.search(stripped):
                continue
            for m in _INNERHTML_ASSIGN_RE.finditer(js):
                approx_line = text[:offset + m.start()].count("\n") + 1
                violations.append(f"{path.name}:{approx_line}")
    unique = sorted(set(violations))
    assert not unique, (
        "innerHTML assignment found in dashboard template JS. "
        "Use el()/mount() from base.html instead — strings become "
        "text nodes automatically, so user-derived content can't "
        "escape its context. See docs/SPEC/operational_notes.md "
        "§'Dashboard XSS hardening'.\n\nViolations:\n  "
        + "\n  ".join(unique)
    )


def test_templates_have_script_blocks() -> None:
    """Sanity — make sure the extraction regex actually finds at
    least one <script> block in the templates that contain JS. If a
    future rename moves the JS out of a <script> tag, we'd silently
    stop guarding that file."""

    # These templates are allowed to have no scripts.
    no_script_ok = {"login.html", "change_password.html"}
    for path in TEMPLATE_DIR.glob("*.html"):
        if path.name in no_script_ok:
            continue
        scripts = _extract_scripts(path.read_text(encoding="utf-8"))
        assert scripts, (
            f"{path.name} has no <script> block — the no-innerHTML "
            "guard can't inspect it. Either add one (even empty) or "
            "mark it as exempt in this test."
        )
