"""Integration Builder — Code viewer API routes."""

import io
import json
import zipfile
from pathlib import Path

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from integration.core.config import settings
from integration.services import r2_service
from integration.services.code_quality import analyze_directory, analyze_file

logger = structlog.get_logger(__name__)

codeview_router = APIRouter(prefix="/sessions", tags=["codeview"])


async def _resolve_session_meta(session_id: str, tenant_id: str) -> dict:
    """Fetch session document from MongoDB and return a normalized metadata dict.

    Returns:
        {
            "service_slug": str,   # clean slug (no _connector suffix)
            "stored_tenant_id": str,
            "stored_tenant_name": str,
            "session_id": str,
        }
    """
    import re as _re_cv

    from integration.db.database import sessions_collection

    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "provider": 1,
            "service": 1,
            "service_slug": 1,
            "tenant_id": 1,
            "tenant_name": 1,
            "output_dir": 1,
        },
    )
    if not session:
        session = await sessions_collection().find_one(
            {"_id": oid},
            {
                "provider": 1,
                "service": 1,
                "service_slug": 1,
                "tenant_id": 1,
                "tenant_name": 1,
                "output_dir": 1,
            },
        )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    service = session.get("service", "")
    if not service:
        raise HTTPException(status_code=404, detail="Session has no service")

    service_slug = session.get("service_slug") or service.replace("-", "_").lower()
    clean = _re_cv.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug

    return {
        "service_slug": clean,
        "service": service,
        "provider": session.get("provider", "") or "",
        "stored_tenant_id": session.get("tenant_id", ""),
        "stored_tenant_name": (session.get("tenant_name") or "").strip().lower(),
        "session_id": session_id,
        "output_dir": session.get("output_dir") or "",
    }


def _slug_candidates(meta: dict) -> list[str]:
    """Candidate output-dir slugs for a session, most-specific first.

    The directory a connector was written to can follow different slug
    conventions depending on where it was built — the session's service_slug
    (e.g. "google_gmail_168e0e"), the bare service ("gmail"), or
    provider_service ("google_gmail"). Try each so resolution doesn't 404 when
    the dir name doesn't match service_slug exactly.
    """
    provider = (meta.get("provider") or "").strip().lower()
    service = (meta.get("service") or "").strip().lower()
    raw = [
        meta.get("service_slug") or "",
        service,
        f"{provider}_{service}" if provider and service else "",
    ]
    out: list[str] = []
    for s in raw:
        if s and s not in out:
            out.append(s)
    return out


async def _resolve_output_dir(session_id: str, tenant_id: str) -> Path:
    """Resolve the generated code directory for a session.

    Tries local disk first; raises 404 if not found (use _resolve_output_dir_or_r2 for R2 fallback).
    """
    meta = await _resolve_session_meta(session_id, tenant_id)

    # Prefer the session's stored output_dir (set by import-existing and Electron SAD sessions).
    # This is the canonical path for imported connectors that live outside GENERATED_CODE_DIR.
    if meta.get("output_dir"):
        _stored = Path(meta["output_dir"])
        if _stored.exists():
            return _stored

    _base = Path(settings.GENERATED_CODE_DIR)
    for _tid in [meta["stored_tenant_id"], tenant_id, meta["stored_tenant_name"]]:
        if not _tid:
            continue
        for _slug in _slug_candidates(meta):
            _candidate = _base / _tid / f"{_slug}_connector"
            if _candidate.exists():
                return _candidate
    raise HTTPException(status_code=404, detail=f"No generated files found for {meta['service_slug']}")


async def _disk_or_r2(session_id: str, tenant_id: str) -> tuple[Path | None, dict | None]:
    """Return (disk_path, None) if code exists on disk, else (None, r2_meta).

    r2_meta contains {service_slug, r2_tenant_id} needed to call r2_service methods.
    Never raises — callers decide how to handle missing data.
    """
    try:
        meta = await _resolve_session_meta(session_id, tenant_id)
    except HTTPException:
        return None, None

    clean = meta["service_slug"]
    _base = Path(settings.GENERATED_CODE_DIR)
    for _tid in [meta["stored_tenant_id"], tenant_id, meta["stored_tenant_name"]]:
        if not _tid:
            continue
        for _slug in _slug_candidates(meta):
            _candidate = _base / _tid / f"{_slug}_connector"
            if _candidate.exists():
                return _candidate, None

    # Disk missing — fall back to R2 (uploaded under the canonical service_slug)
    r2_tenant = meta["stored_tenant_id"] or tenant_id
    return None, {
        "service_slug": clean,
        "r2_tenant_id": r2_tenant,
        "session_id": session_id,
    }


_LANG_MAP = {
    ".py": "python",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "text",
    ".md": "markdown",
    ".sh": "shell",
    ".env": "text",
    ".cfg": "text",
    ".ini": "text",
}

_SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", "node_modules"}


@codeview_router.get("/{session_id}/files")
async def get_file_tree(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return the file tree with quality scores for generated code.

    Reads from local disk when available; falls back to R2 when the server
    has no local copy (e.g. after a redeploy or on a different worker).
    """
    out_dir, r2_meta = await _disk_or_r2(session_id, x_tenant_id)

    # ── R2 path ──────────────────────────────────────────────────────────
    if out_dir is None:
        if r2_meta is None:
            raise HTTPException(status_code=404, detail="Session not found")

        r2_files = await r2_service.list_connector_files(r2_meta["r2_tenant_id"], r2_meta["service_slug"], session_id)
        if not r2_files:
            raise HTTPException(
                status_code=404,
                detail="No generated files found (disk and R2 both empty)",
            )

        tree = []
        for rel in sorted(r2_files):
            ext = Path(rel).suffix.lower()
            lang = _LANG_MAP.get(ext, "text")
            content = (
                await r2_service.get_connector_file(r2_meta["r2_tenant_id"], r2_meta["service_slug"], session_id, rel)
                or ""
            )
            tree.append(
                {
                    "path": rel,
                    "size": len(content.encode("utf-8")),
                    "language": lang,
                    "quality_score": None,
                    "line_count": len(content.splitlines()) if content else None,
                    "function_count": None,
                    "class_count": None,
                    "source": "r2",
                }
            )

        logger.info("codeview.file_tree_r2", session_id=session_id, file_count=len(tree))
        package_root = f"{r2_meta['service_slug']}_connector"
        return {
            "session_id": session_id,
            "directory": f"r2://{r2_service.connector_session_r2_prefix(r2_meta['r2_tenant_id'], r2_meta['service_slug'], session_id)}",
            "package_root": package_root,
            "file_count": len(tree),
            "average_quality_score": 0,
            "total_lines": sum(e.get("line_count") or 0 for e in tree),
            "files": tree,
            "source": "r2",
        }

    # ── Disk path (primary) ───────────────────────────────────────────────
    quality = analyze_directory(str(out_dir))
    tree = []
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _SKIP_DIRS for part in f.parts):
            continue
        rel = str(f.relative_to(out_dir))
        ext = f.suffix.lower()
        lang = _LANG_MAP.get(ext, "text")

        entry: dict = {
            "path": rel,
            "size": f.stat().st_size,
            "language": lang,
            "quality_score": None,
            "line_count": None,
            "function_count": None,
            "class_count": None,
        }

        if lang == "python":
            file_quality = next(
                (fq for fq in quality.get("files", []) if fq.get("path") == rel),
                {},
            )
            entry["quality_score"] = file_quality.get("quality_score")
            entry["line_count"] = file_quality.get("line_count")
            entry["function_count"] = file_quality.get("function_count")
            entry["class_count"] = file_quality.get("class_count")

        tree.append(entry)

    logger.info(
        "codeview.file_tree",
        session_id=session_id,
        file_count=len(tree),
        avg_quality=quality.get("average_quality_score", 0),
    )
    return {
        "session_id": session_id,
        "directory": str(out_dir),
        "package_root": out_dir.name,
        "file_count": len(tree),
        "average_quality_score": quality.get("average_quality_score", 0),
        "total_lines": quality.get("total_lines", 0),
        "files": tree,
    }


@codeview_router.get("/{session_id}/connector-metadata")
async def get_connector_metadata_from_session(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return connector.json metadata for a session — used by Deploy step to build the install form."""
    out_dir = await _resolve_output_dir(session_id, x_tenant_id)
    meta_path = out_dir / "metadata" / "connector.json"
    if not meta_path.exists():
        raise HTTPException(
            status_code=404,
            detail="connector.json not found — run generate_metadata step first",
        )
    import json as _json

    return _json.loads(meta_path.read_text(encoding="utf-8"))


@codeview_router.get("/{session_id}/files/{file_path:path}")
async def get_file_content(
    session_id: str,
    file_path: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return the content of a specific generated file.

    Reads from local disk first; falls back to R2 when disk is absent.
    """
    # Reject obvious path traversal attempts before any disk/R2 lookup
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    out_dir, r2_meta = await _disk_or_r2(session_id, x_tenant_id)

    # ── R2 path ──────────────────────────────────────────────────────────
    if out_dir is None:
        if r2_meta is None:
            raise HTTPException(status_code=404, detail="Session not found")

        content = await r2_service.get_connector_file(
            r2_meta["r2_tenant_id"], r2_meta["service_slug"], session_id, file_path
        )
        if content is None:
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

        ext = Path(file_path).suffix.lower()
        lang = _LANG_MAP.get(ext, "text")
        logger.info("codeview.file_content_r2", session_id=session_id, file_path=file_path)
        return {
            "path": file_path,
            "content": content,
            "size": len(content.encode("utf-8")),
            "language": lang,
            "quality": None,
            "source": "r2",
        }

    # ── Disk path (primary) ───────────────────────────────────────────────
    target = out_dir / file_path

    # Security: ensure the resolved path is within out_dir
    try:
        target.resolve().relative_to(out_dir.resolve())
    except ValueError:
        logger.warning(
            "codeview.path_traversal_attempt",
            session_id=session_id,
            file_path=file_path,
        )
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not target.exists():
        # Last resort: try R2 even though disk dir exists (file may not have been written yet)
        meta = await _resolve_session_meta(session_id, x_tenant_id)
        r2_tid = meta["stored_tenant_id"] or x_tenant_id
        content = await r2_service.get_connector_file(r2_tid, meta["service_slug"], session_id, file_path)
        if content is not None:
            ext = Path(file_path).suffix.lower()
            lang = _LANG_MAP.get(ext, "text")
            return {
                "path": file_path,
                "content": content,
                "size": len(content.encode("utf-8")),
                "language": lang,
                "quality": None,
                "source": "r2",
            }
        logger.warning("codeview.file_not_found", session_id=session_id, file_path=file_path)
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    content = target.read_text(encoding="utf-8")
    ext = target.suffix.lower()
    lang = _LANG_MAP.get(ext, "text")

    quality: dict = {}
    if lang == "python":
        quality = analyze_file(str(target))

    logger.info(
        "codeview.file_content",
        session_id=session_id,
        file_path=file_path,
        size=target.stat().st_size,
        quality_score=quality.get("quality_score"),
    )

    return {
        "path": file_path,
        "content": content,
        "size": target.stat().st_size,
        "language": lang,
        "quality": quality if quality else None,
    }


@codeview_router.get("/{session_id}/download-zip")
async def download_connector_zip(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Stream the connector codebase as a .zip file.

    Filename:
      - If connector.json has a version number → {connector_name}_v{version}.zip
      - Otherwise                              → {connector_name}_unversioned.zip
    """
    out_dir = await _resolve_output_dir(session_id, x_tenant_id)

    # Determine version from connector.json metadata (generated in generate_metadata step)
    version: str | None = None
    connector_name_slug = out_dir.name  # e.g. "paytm_upi_connector"
    meta_path = out_dir / "metadata" / "connector.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            version = str(meta.get("version") or "").strip() or None
        except Exception:
            pass

    zip_name = f"{connector_name_slug}_v{version}.zip" if version else f"{connector_name_slug}_unversioned.zip"

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out_dir.rglob("*")):
            if not f.is_file():
                continue
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            zf.write(f, arcname=str(f.relative_to(out_dir.parent)))

    buf.seek(0)

    logger.info(
        "codeview.zip_download",
        session_id=session_id,
        zip_name=zip_name,
        size=buf.getbuffer().nbytes,
    )

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )
