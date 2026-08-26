"""_synthesize_docs was one 76-line body with a cognitive complexity of 35.

It is four independent section builders now. These pin the behaviour that
refactor had to preserve — and the edges that made the original hard to read:
sections that vanish when they have nothing to say, and a setup.md that cannot
be read being a missing section rather than a failed build.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from build_artifact import _synthesize_docs


def _ids(doc: dict) -> list[str]:
    return [s["id"] for s in doc["sections"]]


def test_overview_is_always_there(tmp_path: Path) -> None:
    """Every other section can be absent; this one needs no data to exist."""
    doc = _synthesize_docs(tmp_path, {})

    assert _ids(doc) == ["overview"]
    assert doc["title"] == "Connector Documentation"


def test_a_connector_with_nothing_declared_yields_no_empty_sections(tmp_path: Path) -> None:
    """🚨 The viewer renders what it is given. An "Authentication" heading with
    no fields under it reads as broken docs, not as a connector without fields."""
    doc = _synthesize_docs(tmp_path, {"install_fields": [], "apis": []})

    assert "authentication" not in _ids(doc)
    assert "api-methods" not in _ids(doc)


def test_setup_md_becomes_a_section(tmp_path: Path) -> None:
    (tmp_path / "instructions").mkdir()
    (tmp_path / "instructions" / "setup.md").write_text("Create an app first.", encoding="utf-8")

    doc = _synthesize_docs(tmp_path, {})

    setup = next(s for s in doc["sections"] if s["id"] == "setup")
    assert setup["content"] == "Create an app first."


def test_an_empty_setup_md_is_not_a_section(tmp_path: Path) -> None:
    (tmp_path / "instructions").mkdir()
    (tmp_path / "instructions" / "setup.md").write_text("   \n", encoding="utf-8")

    assert "setup" not in _ids(_synthesize_docs(tmp_path, {}))


def test_an_unreadable_setup_md_does_not_fail_the_build(tmp_path: Path) -> None:
    """Docs are a nice-to-have; the artifact is valid without them. Raising here
    would fail a connector build over an unreadable file.

    🚨 The OSError is provoked by making setup.md a DIRECTORY, not by chmod 000.
    CI runs as root, and root bypasses file permissions entirely — a chmod-based
    version of this test passes locally and fails in CI, having proved nothing
    either way.
    """
    (tmp_path / "instructions").mkdir()
    # A directory where a file is expected: open() raises IsADirectoryError,
    # which is an OSError, for root and everyone else alike.
    (tmp_path / "instructions" / "setup.md").mkdir()

    doc = _synthesize_docs(tmp_path, {})

    assert "setup" not in _ids(doc)
    assert _ids(doc) == ["overview"], "the rest of the doc tree still builds"


def test_install_fields_render_with_required_and_help(tmp_path: Path) -> None:
    doc = _synthesize_docs(
        tmp_path,
        {
            "auth_type": "oauth2",
            "install_fields": [
                {"key": "client_id", "label": "Client ID", "type": "text", "required": True, "help": "From the app"},
                {"key": "scope"},
            ],
        },
    )

    auth = next(s for s in doc["sections"] if s["id"] == "authentication")
    assert "**oauth2**" in auth["content"]
    assert "*(required)*" in auth["content"]
    assert "— From the app" in auth["content"]
    # No label — the key stands in, rather than rendering "None".
    assert "**scope**" in auth["content"]


def test_apis_become_children_with_a_count(tmp_path: Path) -> None:
    doc = _synthesize_docs(
        tmp_path,
        {"apis": [{"name": "List Channels", "method": "POST", "params": [{"key": "types", "required": True}]}]},
    )

    api = next(s for s in doc["sections"] if s["id"] == "api-methods")
    assert "**1** operation(s)" in api["content"]
    child = api["children"][0]
    assert child["id"] == "api-list-channels", "a name with spaces has to slugify"
    assert "`POST`" in child["content"]
    assert "*(required)*" in child["content"]


def test_an_operation_without_parameters_says_so(tmp_path: Path) -> None:
    """An empty Parameters heading reads as missing documentation."""
    doc = _synthesize_docs(tmp_path, {"apis": [{"id": "ping"}]})

    child = next(s for s in doc["sections"] if s["id"] == "api-methods")["children"][0]
    assert "_No parameters._" in child["content"]


def test_the_display_name_wins_over_the_slug(tmp_path: Path) -> None:
    doc = _synthesize_docs(tmp_path, {"display_name": "Slack", "connector_type": "slack_connector"})

    assert doc["title"] == "Slack Documentation"


def test_section_order_is_stable(tmp_path: Path) -> None:
    """The viewer renders them in order; a reader expects Overview first."""
    (tmp_path / "instructions").mkdir()
    (tmp_path / "instructions" / "setup.md").write_text("go", encoding="utf-8")

    doc = _synthesize_docs(tmp_path, {"install_fields": [{"key": "k"}], "apis": [{"id": "a"}]})

    assert _ids(doc) == ["overview", "setup", "authentication", "api-methods"]
