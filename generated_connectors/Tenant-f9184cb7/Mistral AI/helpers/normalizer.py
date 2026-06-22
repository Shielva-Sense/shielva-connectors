"""Normalize Mistral API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict


def _epoch_to_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_model(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Mistral model record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    model = raw if isinstance(raw, dict) else {}
    source_id = model.get("id", "")
    capabilities = model.get("capabilities") or {}
    cap_summary = ", ".join(k for k, v in capabilities.items() if v) if isinstance(capabilities, dict) else ""
    content_parts = [
        model.get("description", "") or "",
        f"owned_by={model.get('owned_by', '')}" if model.get("owned_by") else "",
        f"capabilities={cap_summary}" if cap_summary else "",
    ]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=source_id,
        content=" | ".join(p for p in content_parts if p) or source_id,
        content_type="text",
        author=model.get("owned_by"),
        created_at=_epoch_to_dt(model.get("created")),
        updated_at=None,
        metadata={
            "owned_by": model.get("owned_by", ""),
            "max_context_length": model.get("max_context_length"),
            "capabilities": capabilities,
            "kind": "mistral.model",
        },
        source="mistral.model",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_file(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Mistral uploaded-file record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    file = raw if isinstance(raw, dict) else {}
    source_id = file.get("id", "")
    purpose = file.get("purpose", "")
    size = file.get("bytes", 0)
    filename = file.get("filename", source_id)
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=filename,
        content=f"Purpose: {purpose} ({size} bytes)" if purpose else f"{size} bytes",
        content_type="text",
        author=None,
        created_at=_epoch_to_dt(file.get("created_at")),
        updated_at=None,
        metadata={
            "purpose": purpose,
            "bytes": size,
            "status": file.get("status", ""),
            "sample_type": file.get("sample_type", ""),
            "kind": "mistral.file",
        },
        source="mistral.file",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_fine_tuning_job(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Mistral fine-tuning job record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    job = raw if isinstance(raw, dict) else {}
    source_id = job.get("id", "")
    model = job.get("model", "")
    status = job.get("status", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Fine-tune {source_id} ({model})" if model else f"Fine-tune {source_id}",
        content=f"status={status}, model={model}",
        content_type="text",
        author=None,
        created_at=_epoch_to_dt(job.get("created_at")),
        updated_at=None,
        metadata={
            "status": status,
            "model": model,
            "fine_tuned_model": job.get("fine_tuned_model"),
            "hyperparameters": job.get("hyperparameters") or {},
            "training_files": job.get("training_files") or [],
            "kind": "mistral.fine_tuning",
        },
        source="mistral.fine_tuning",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
