from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import GoogleSheetsAuthError, GoogleSheetsError, GoogleSheetsRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def normalize_sheet_rows(
    spreadsheet_id: str,
    sheet_title: str,
    headers: list[str],
    rows: list[list[str]],
    connector_id: str,
    tenant_id: str,
) -> list[ConnectorDocument]:
    """Convert raw sheet rows into ConnectorDocuments — one per data row.

    Row 0 of ``rows`` is expected to be the first *data* row (i.e. headers
    are passed separately).  The spreadsheet row number in the document title
    and metadata uses 1-based indexing matching Google Sheets UI where row 1
    is the header row, so data rows start at row 2.
    """
    documents: list[ConnectorDocument] = []
    for row_index, row in enumerate(rows):
        row_number = row_index + 2  # 1-based; row 1 = headers

        # Build header→value mapping, skipping empty cells
        values_dict: dict[str, str] = {}
        for col_idx, header in enumerate(headers):
            cell_value = row[col_idx] if col_idx < len(row) else ""
            if header and cell_value:
                values_dict[header] = cell_value

        # Tab-separated "header: value" pairs for non-empty cells
        content_parts = [f"{k}: {v}" for k, v in values_dict.items()]
        content = "\t".join(content_parts) if content_parts else "(empty row)"

        source_id = _sha256_prefix(
            f"{spreadsheet_id}:{sheet_title}:{row_index}", length=16
        )
        doc = ConnectorDocument(
            source_id=source_id,
            title=f"{sheet_title} — Row {row_number}",
            content=content,
            connector_id=connector_id,
            tenant_id=tenant_id,
            source_url=(
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
            ),
            metadata={
                "spreadsheet_id": spreadsheet_id,
                "sheet_title": sheet_title,
                "row_index": row_index,
                "row_number": row_number,
                "headers": headers,
                "values_dict": values_dict,
            },
        )
        documents.append(doc)
    return documents


def normalize_spreadsheet(
    spreadsheet: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a spreadsheet metadata response into a single ConnectorDocument."""
    spreadsheet_id: str = spreadsheet.get("spreadsheetId", "")
    title: str = spreadsheet.get("properties", {}).get("title", spreadsheet_id)
    sheets: list[dict[str, Any]] = spreadsheet.get("sheets", [])
    sheet_names = [
        s.get("properties", {}).get("title", "") for s in sheets if s
    ]
    content = ", ".join(sheet_names) if sheet_names else "(no sheets)"

    source_id = _sha256_prefix(spreadsheet_id, length=16)
    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
        metadata={
            "spreadsheet_id": spreadsheet_id,
            "sheet_names": sheet_names,
            "sheet_count": len(sheet_names),
        },
    )


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    GoogleSheetsAuthError is never retried — it requires human intervention.
    GoogleSheetsRateLimitError honours the Retry-After header when present.
    """
    last_exc: GoogleSheetsError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except GoogleSheetsAuthError:
            raise
        except GoogleSheetsRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = (
                exc.retry_after
                if exc.retry_after > 0
                else min(
                    base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                    + random.uniform(0, RETRY_JITTER_S),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
        except GoogleSheetsError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
