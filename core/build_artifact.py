#!/usr/bin/env python3
"""Pre-generate connector ARTIFACTS and publish them to the on-prem Nexus PyPI repo.

Architecture (confirmed with the team):

    R2 (source of truth, modular connector source code)
        │  ── pre-generation (this script, run when connectors change) ──
        ▼
    one versioned wheel per connector  ──►  Nexus Repository (PyPI repo)
        │
        ▼
    shielva_connectors gateway  ──  pip install + importlib at runtime
                                    (entry-point discovery — see _load_installed_connectors)

Each source connector is already a Python package::

    {name}_connector/
        __init__.py        # exports the BaseConnector subclass
        connector.py       # CONNECTOR_TYPE = "..."  +  class XConnector(BaseConnector)
        models.py exceptions.py client/ helpers/ metadata/connector.json
        requirements.txt   # runtime deps (+ test deps we strip)

This packages each into ``shielva-connector-{type}-{version}-py3-none-any.whl`` with a
``shielva.connectors`` entry-point so the gateway can discover it via
``importlib.metadata.entry_points`` — no filesystem scan, fully modular.

The connector code imports ``shared.base_connector`` — that's HOST-provided by the
gateway, so it is NOT bundled and NOT declared as a dependency.

Usage::

    # build only (wheels land in ./dist_connectors/)
    python core/build_artifact.py --src ~/Documents/client_dir

    # build + publish to Nexus (creds from env — never hard-coded)
    export PYPI_PUBLISH_URL=https://nexus.shielva.ai/repository/shielva-pypi/
    export PYPI_USER=shielva-ci
    export PYPI_TOKEN=<nexus-ci-password>
    python core/build_artifact.py --src ~/Documents/client_dir --publish

    # single connector (fast iteration)
    python core/build_artifact.py --src ~/Documents/client_dir --only activecampaign --publish
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Files/dirs that must never end up inside a runtime wheel.
_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".shielva", "tests", ".git"}
_EXCLUDE_FILES = {
    ".DS_Store",
    "pytest.ini",
    "conftest.py",
    "plan_steps.json",
    "stepper_progress.json",
}
# Test-only deps in requirements.txt that the runtime artifact must not carry.
_TEST_DEP_RE = re.compile(r"^(pytest|pytest-|coverage|mock\b|responses|freezegun|tox)", re.I)
# `shared` is provided by the gateway host — never declare it as a wheel dep.
_HOST_DEP_RE = re.compile(r"^(shared|shielva[_-]connectors)\b", re.I)


def _norm(name: str) -> str:
    """PEP 503 normalize a distribution name component."""
    return re.sub(r"[-_.]+", "-", name).strip("-").lower()


def _parse_connector(connector_py: Path) -> tuple[str, str] | None:
    """Return (connector_type, ClassName) parsed from connector.py, or None.

    Robust to the common shapes seen across the library:
      • ``class XConnector(BaseConnector)``            — direct base
      • ``_BASE = BaseConnector; class XConnector(_BASE)``  — aliased base (try/except import)
      • ``CONNECTOR_TYPE`` declared at module OR class level
    The connector class is identified by ANY of: a base resolving to BaseConnector
    (directly or via alias), a ``CONNECTOR_TYPE`` class attribute, or a ``*Connector``
    class name.
    """
    try:
        tree = ast.parse(connector_py.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return None

    ctype: str | None = None
    base_aliases: set[str] = set()  # names bound to BaseConnector, e.g. `_BASE = BaseConnector`

    def _is_base_ref(v: ast.expr | None) -> bool:
        return (isinstance(v, ast.Name) and v.id.endswith("BaseConnector")) or (
            isinstance(v, ast.Attribute) and v.attr.endswith("BaseConnector")
        )

    # Pass 1 — CONNECTOR_TYPE literal (any scope) + BaseConnector aliases.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "CONNECTOR_TYPE":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        ctype = ctype or node.value.value
                if isinstance(t, ast.Name) and _is_base_ref(node.value):
                    base_aliases.add(t.id)

    def _base_matches(b: ast.expr) -> bool:
        if isinstance(b, ast.Name):
            return b.id.endswith("BaseConnector") or b.id in base_aliases
        if isinstance(b, ast.Attribute):
            return b.attr.endswith("BaseConnector")
        return False

    def _declares_ctype(cd: ast.ClassDef) -> bool:
        for n in cd.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                if any(isinstance(t, ast.Name) and t.id == "CONNECTOR_TYPE" for t in tg):
                    return True
        return False

    # Pass 2 — pick the connector class. Strong signals first, name suffix as fallback.
    cls: str | None = None
    fallback: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if any(_base_matches(b) for b in node.bases) or _declares_ctype(node):
                cls = cls or node.name
            elif node.name.endswith("Connector"):
                fallback = fallback or node.name
    cls = cls or fallback

    if ctype and cls:
        return ctype, cls
    return None


def _discover(
    src_root: Path,
) -> tuple[dict[str, tuple[Path, str, str]], list[tuple[str, str, str]]]:
    """Scan source → ({norm_type: (dir, type, class)}, [collisions]).

    Two source dirs can resolve to the SAME CONNECTOR_TYPE (a generic alias dir plus
    the branded dir, e.g. ``calendar_connector`` + ``google_calendar_connector`` →
    ``google-calendar``). Only one wheel per type may exist, so we keep ONE dir per
    type — preferring the dir whose name matches the type (the canonical/branded one)
    — and report every collision so the source dupes can be cleaned up.
    """
    chosen: dict[str, tuple[Path, str, str]] = {}
    collisions: list[tuple[str, str, str]] = []  # (norm_type, dropped_dir, kept_dir)

    def _name_matches(d: Path, key: str) -> bool:
        stem = _norm(re.sub(r"_?connector$", "", d.name))
        return stem == key

    for p in sorted(p for p in src_root.iterdir() if p.is_dir() and (p / "connector.py").exists()):
        parsed = _parse_connector(p / "connector.py")
        if not parsed:
            continue
        ctype, cls = parsed
        key = _norm(ctype)
        if key not in chosen:
            chosen[key] = (p, ctype, cls)
            continue
        kept_dir = chosen[key][0]
        # Prefer the dir whose name matches the type; otherwise keep the first.
        if _name_matches(p, key) and not _name_matches(kept_dir, key):
            collisions.append((key, kept_dir.name, p.name))
            chosen[key] = (p, ctype, cls)
        else:
            collisions.append((key, p.name, kept_dir.name))
    return chosen, collisions


def _runtime_deps(req_file: Path) -> list[str]:
    """Runtime dependencies for the wheel: requirements.txt minus test/host deps."""
    if not req_file.exists():
        return []
    out: list[str] = []
    for raw in req_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if _TEST_DEP_RE.match(line) or _HOST_DEP_RE.match(line):
            continue
        out.append(line)
    return out


def _copy_pkg(src_pkg: Path, dst_pkg: Path) -> None:
    """Copy the connector package, dropping test/junk dirs and files."""

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        drop = set()
        for n in names:
            if n in _EXCLUDE_DIRS or n in _EXCLUDE_FILES or n.endswith(".pyc"):
                drop.add(n)
        return drop

    shutil.copytree(src_pkg, dst_pkg, ignore=_ignore)


def _rewrite_imports(pkg_dst: Path, pkg_name: str) -> int:
    """Make the connector's intra-package ABSOLUTE imports package-qualified.

    The sources use ``from connector import``, ``from client import``,
    ``from models import`` etc. — valid only when the connector dir sits on
    sys.path (the filesystem loader's trick). As an installed wheel that breaks
    (`ModuleNotFoundError: No module named 'connector'`) AND every connector's
    bare `client`/`models` would collide. We rewrite each LOCAL module reference
    to ``from {pkg_name}.{module}`` — depth-independent, collision-free, importable
    by plain `pip install`. External imports (shared.*, aiohttp, structlog, …) are
    left untouched because they're not local top-level module names.
    """
    locals_: set[str] = set()
    for p in pkg_dst.iterdir():
        if p.is_dir() and (p / "__init__.py").exists():
            locals_.add(p.name)
        elif p.suffix == ".py" and p.stem != "__init__":
            locals_.add(p.stem)
    if not locals_:
        return 0
    alt = "|".join(re.escape(m) for m in sorted(locals_, key=len, reverse=True))
    from_re = re.compile(r"^(\s*)from\s+(" + alt + r")((?:\.[A-Za-z0-9_]+)*)\s+import\b")
    import_re = re.compile(r"^(\s*)import\s+(" + alt + r")\s*(?:#.*)?$")
    changed = 0
    for py in pkg_dst.rglob("*.py"):
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        dirty = False
        out: list[str] = []
        for ln in lines:
            m = from_re.match(ln)
            if m:
                indent, mod, sub = m.group(1), m.group(2), m.group(3)
                ln = f"{indent}from {pkg_name}.{mod}{sub} import" + ln[m.end() :]
                dirty = True
            else:
                mi = import_re.match(ln)
                if mi:
                    indent, mod = mi.group(1), mi.group(2)
                    ln = f"{indent}import {pkg_name}.{mod} as {mod}"
                    dirty = True
            out.append(ln)
        if dirty:
            py.write_text("\n".join(out) + "\n", encoding="utf-8")
            changed += 1
    return changed


def _pyproject(dist: str, version: str, pkg_dir_name: str, ctype: str, cls: str, deps: list[str]) -> str:
    dep_lines = ",\n    ".join(f'"{d}"' for d in deps)
    return f"""\
[build-system]
requires = ["setuptools>=61", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{dist}"
version = "{version}"
description = "Shielva connector artifact: {ctype}"
requires-python = ">=3.10"
dependencies = [
    {dep_lines}
]

[project.entry-points."shielva.connectors"]
{ctype} = "{pkg_dir_name}.connector:{cls}"

[tool.setuptools.packages.find]
include = ["{pkg_dir_name}", "{pkg_dir_name}.*"]

[tool.setuptools.package-data]
"*" = ["*.json", "*.md", "metadata/*", "instructions/*"]
"""


def _synthesize_docs(src_pkg: Path, meta: dict) -> dict:
    """Build a ``{title, sections}`` doc tree from a connector's metadata +
    instructions/setup.md, for connectors that ship no authored
    ``.shielva/docs/connector_docs.json``. Deterministic (no LLM): Overview from
    the description, Setup from setup.md, Authentication from install_fields, and an
    API reference from the declared apis. Matches the shape the docs viewer renders."""
    name = meta.get("display_name") or meta.get("name") or meta.get("connector_type") or "Connector"
    desc = (meta.get("description") or "").strip()
    cats = ", ".join(str(c) for c in (meta.get("categories") or []))
    secs: list[dict] = [
        {
            "id": "overview",
            "title": "Overview",
            "content": f"**{name}**" + (f"\n\n{desc}" if desc else "") + (f"\n\n**Category:** {cats}" if cats else ""),
        }
    ]
    setup_path = src_pkg / "instructions" / "setup.md"
    if setup_path.exists():
        try:
            body = setup_path.read_text(encoding="utf-8").strip()
            if body:
                secs.append({"id": "setup", "title": "Setup Guide", "content": body})
        except OSError:
            pass
    fields = meta.get("install_fields") or []
    if fields:
        lines = []
        for f in fields:
            req = " *(required)*" if f.get("required") else ""
            ln = f"- **{f.get('label') or f.get('key')}** (`{f.get('key')}`, {f.get('type', 'text')}){req}"
            if f.get("help"):
                ln += f" — {f['help']}"
            lines.append(ln)
        secs.append(
            {
                "id": "authentication",
                "title": "Authentication",
                "content": f"This connector authenticates via **{meta.get('auth_type', 'api_key')}**.\n\n"
                "Fields provided when installing:\n\n" + "\n".join(lines),
            }
        )
    apis = meta.get("apis") or []
    if apis:
        kids = []
        for a in apis:
            params = a.get("params") or []
            pmd = (
                "\n".join(
                    f"- `{p.get('key')}` ({p.get('type', 'text')})"
                    + (" *(required)*" if p.get("required") else "")
                    + (f" — {p['label']}" if p.get("label") else "")
                    for p in params
                )
                or "_No parameters._"
            )
            kids.append(
                {
                    "id": f"api-{(a.get('id') or str(a.get('name', '')).lower().replace(' ', '-'))}",
                    "title": a.get("name") or a.get("id") or "Operation",
                    "content": f"{a.get('description', '')}\n\n**HTTP method:** `{a.get('method', 'GET')}`\n\n"
                    f"**Parameters:**\n\n{pmd}",
                }
            )
        secs.append(
            {
                "id": "api-methods",
                "title": "API Methods",
                "content": f"This connector exposes **{len(apis)}** operation(s).",
                "children": kids,
            }
        )
    return {
        "title": f"{name} Documentation",
        "generated_by": "build_artifact:metadata-synthesis",
        "prompt_source": "connector.json + instructions/setup.md",
        "sections": secs,
    }


def build_one(src_pkg: Path, out_dir: Path, version: str) -> tuple[str, str] | None:
    """Build a single connector wheel. Returns (dist_name, wheel_path) or None."""
    connector_py = src_pkg / "connector.py"
    if not connector_py.exists():
        print(f"  · skip {src_pkg.name} (no connector.py)")
        return None
    parsed = _parse_connector(connector_py)
    if not parsed:
        print(f"  · skip {src_pkg.name} (no CONNECTOR_TYPE / BaseConnector subclass found)")
        return None
    ctype, cls = parsed
    dist = f"shielva-connector-{_norm(ctype)}"
    deps = _runtime_deps(src_pkg / "requirements.txt")

    with tempfile.TemporaryDirectory(prefix="conn-build-") as tmp:
        tmp_path = Path(tmp)
        pkg_dst = tmp_path / src_pkg.name
        _copy_pkg(src_pkg, pkg_dst)
        # Make intra-package absolute imports package-qualified so the wheel
        # imports cleanly once pip-installed (the connectors' `from client import`
        # etc. only work when the dir is on sys.path otherwise).
        _rewrite_imports(pkg_dst, src_pkg.name)
        # Bundle the authored docs INTO the wheel so they travel with the connector
        # artifact and the runtime can serve them live (GET /connectors/{type}/docs).
        # .shielva is excluded from the package, so lift just the docs json to a
        # package-data path (package-data already includes *.json).
        _docs_src = src_pkg / ".shielva" / "docs" / "connector_docs.json"
        if _docs_src.exists():
            shutil.copy2(_docs_src, pkg_dst / "_shielva_docs.json")
        else:
            # No AUTHORED docs → synthesize a {title, sections} tree from the
            # connector's own metadata + instructions/setup.md so every connector
            # serves useful documentation (GET /connectors/{type}/docs) instead of
            # "No documentation available". Deterministic, no LLM.
            import json as _dj

            try:
                _meta = _dj.loads((src_pkg / "metadata" / "connector.json").read_text(encoding="utf-8"))
                (pkg_dst / "_shielva_docs.json").write_text(
                    _dj.dumps(_synthesize_docs(src_pkg, _meta), indent=2), encoding="utf-8"
                )
            except (OSError, ValueError, KeyError):
                pass
        (tmp_path / "pyproject.toml").write_text(
            _pyproject(dist, version, src_pkg.name, ctype, cls, deps), encoding="utf-8"
        )
        # --no-isolation reuses this interpreter's setuptools/wheel (already present
        # in the venv) instead of building a fresh env per wheel — ~15× faster across
        # 200+ connectors. The build needs no exotic build deps, so this is safe.
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(out_dir),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"  ✗ {dist}: build failed\n{proc.stdout[-600:]}\n{proc.stderr[-600:]}")
            return None
    wheels = sorted(out_dir.glob(f"{_norm(dist).replace('-', '_')}-{version}-*.whl"))
    wheel = wheels[-1] if wheels else None
    print(f"  ✓ {dist}=={version}  ({ctype} → {cls})")
    return (dist, str(wheel) if wheel else "")


def write_snapshot(src_root: Path, version: str, dest: Path) -> int:
    """Consolidate every connector's metadata/connector.json into one file.

    The runtime catalog seeder (`services/connector_catalog.py`) reads this single
    file at boot, hashes it, and seeds Mongo when the hash changed — so the per-dir
    walk of ~200 connector.json files happens ONCE at build time, never at runtime.
    Schema:
        { "version": "1.0.0",
          "connectors": [ <metadata/connector.json verbatim, plus connector_type>, ... ] }
    """
    import json as _json

    chosen, _ = _discover(src_root)
    items: list[dict] = []
    for dir_, ctype, _cls in chosen.values():
        meta_path = dir_ / "metadata" / "connector.json"
        if not meta_path.exists():
            continue
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            continue
        # Belt-and-braces: stamp connector_type in case the metadata file omits it
        # or disagrees with the deduped type chosen by _discover().
        meta["connector_type"] = ctype
        items.append(meta)
    items.sort(key=lambda c: c.get("connector_type") or "")
    payload = {"version": version, "count": len(items), "connectors": items}
    dest.write_text(_json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"→ wrote snapshot ({len(items)} connectors) → {dest}")
    return len(items)


def write_manifest(src_root: Path, version: str, dest: Path) -> int:
    """Write a pip requirements manifest of every connector artifact.

    The gateway image installs the connector library by ``pip install -r`` this file
    from the Nexus PyPI index, so it must be committed and kept in sync with the published
    wheels. Derived by scanning source (no build needed) so it's cheap to regenerate.
    """
    chosen, _ = _discover(src_root)
    lines = sorted(f"shielva-connector-{key}=={version}" for key in chosen)
    dest.write_text(
        "# Auto-generated by core/build_artifact.py — connector artifact manifest.\n"
        "# Installed by the gateway image from the Nexus PyPI index. Do not hand-edit.\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print(f"→ wrote manifest ({len(lines)} connectors) → {dest}")
    return len(lines)


def publish(out_dir: Path) -> int:
    """twine-upload every wheel in out_dir to the PyPI repo. Creds from env.

    Publishes to the on-prem Nexus: set ``PYPI_PUBLISH_URL``
    (e.g. https://nexus.shielva.ai/repository/shielva-pypi/) + ``PYPI_USER`` +
    ``PYPI_TOKEN``.
    """
    user = os.environ.get("PYPI_USER", "")
    token = os.environ.get("PYPI_TOKEN", "")
    publish_url = os.environ.get("PYPI_PUBLISH_URL", "").rstrip("/")
    if not (publish_url and user and token):
        print("✗ publish needs PYPI_PUBLISH_URL + PYPI_USER + PYPI_TOKEN (Nexus)")
        return 2
    # Keep a trailing slash: Nexus 400s a twine POST to `…/repository/shielva-pypi`
    # but accepts `…/shielva-pypi/`. (publish_url was rstrip("/")-ed above.)
    repo_url = publish_url + "/"
    wheels = [str(p) for p in sorted(out_dir.glob("*.whl"))]
    if not wheels:
        print("✗ no wheels to publish")
        return 2
    print(f"→ publishing {len(wheels)} wheels to {repo_url}")
    # NB: the Nexus `shielva-pypi` repo is currently writePolicy=ALLOW (redeploy
    # permitted), so re-uploading an existing version SUCCEEDS (overwrites) rather
    # than erroring like the old JFrog repo did. BUT still bump CONNECTOR_VERSION for
    # any fix: pip's cache + the gateway's pinned installs won't re-pull an unchanged
    # version string, so a same-version overwrite won't reach running pods. (If you
    # want the registry to *enforce* immutability, set the repo to ALLOW_ONCE.)
    # See docs/CONNECTOR_PUBLISH_RUNBOOK.md.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "twine",
            "upload",
            "--repository-url",
            repo_url,
            "-u",
            user,
            "-p",
            token,
            *wheels,
        ],
        text=True,
    )
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--src",
        default=os.path.expanduser("~/Documents/client_dir"),
        help="dir of {name}_connector source packages (the R2 source, mirrored locally)",
    )
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "dist_connectors"),
        help="where built wheels land",
    )
    ap.add_argument(
        "--version",
        default=os.environ.get("CONNECTOR_VERSION", "1.0.0"),
        help="artifact version stamped on every wheel",
    )
    ap.add_argument("--only", default="", help="build only this connector type (fast iteration)")
    ap.add_argument("--publish", action="store_true", help="twine-upload to Nexus after building")
    ap.add_argument(
        "--manifest",
        default=str(Path(__file__).resolve().parent / "connectors-requirements.txt"),
        help="committed pip manifest the gateway image installs from",
    )
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="just (re)write the manifest by scanning source — no build/publish",
    )
    ap.add_argument(
        "--snapshot",
        default=str(Path(__file__).resolve().parent / "shielva_connectors.json"),
        help="consolidated metadata/connector.json snapshot, baked into the image",
    )
    ap.add_argument(
        "--snapshot-only",
        action="store_true",
        help="just (re)write the snapshot from metadata/connector.json — no build/publish",
    )
    args = ap.parse_args()

    src_root = Path(args.src).expanduser()
    if not src_root.is_dir():
        print(f"✗ --src not found: {src_root}")
        return 2

    if args.manifest_only:
        return 0 if write_manifest(src_root, args.version, Path(args.manifest)) else 1

    if args.snapshot_only:
        return 0 if write_snapshot(src_root, args.version, Path(args.snapshot)) else 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    chosen, collisions = _discover(src_root)
    if collisions:
        print(f"⚠ {len(collisions)} duplicate-type collision(s) — kept the canonical dir, dropped the alias:")
        for key, dropped, kept in collisions:
            print(f"    {key}: dropped {dropped}  (kept {kept})")
    pkgs = [dir_ for (dir_, _ctype, _cls) in chosen.values()]
    if args.only:
        pkgs = [p for p in pkgs if _norm(args.only) in _norm(p.name)]
    print(f"→ building {len(pkgs)} connector artifacts (v{args.version}) → {out_dir}")

    built, failed = [], []
    for p in sorted(pkgs):
        res = build_one(p, out_dir, args.version)
        (built if res else failed).append(p.name)

    print(f"\n✓ built {len(built)}   ✗ failed {len(failed)}")
    if failed:
        print("  failed:", ", ".join(failed[:20]) + (" …" if len(failed) > 20 else ""))

    # Keep the committed manifest + snapshot in lock-step with what we just built.
    write_manifest(src_root, args.version, Path(args.manifest))
    write_snapshot(src_root, args.version, Path(args.snapshot))

    if args.publish:
        return publish(out_dir)
    return 0 if built else 1


if __name__ == "__main__":
    raise SystemExit(main())
