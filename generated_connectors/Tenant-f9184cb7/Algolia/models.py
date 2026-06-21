"""Dataclass models for the most common Algolia REST API response shapes.

These exist for typed construction in tests and helpers. The connector
boundary always returns raw dicts so callers receive the full API surface
without forward-compat concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class IndexInfo:
    """One entry from ``GET /1/indexes`` → ``response.items[*]``."""

    name: str
    entries: int = 0
    data_size: int = 0
    file_size: int = 0
    last_build_time_s: int = 0
    primary: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def dataSize(self) -> int:  # noqa: N802 — Algolia camelCase
        return self.data_size

    @property
    def fileSize(self) -> int:  # noqa: N802
        return self.file_size

    @property
    def lastBuildTimeS(self) -> int:  # noqa: N802
        return self.last_build_time_s


@dataclass
class SearchHit:
    """One hit from a search response — ``objectID`` plus arbitrary attributes."""

    object_id: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    highlight_result: Dict[str, Any] = field(default_factory=dict)

    @property
    def objectID(self) -> str:  # noqa: N802 — Algolia field name
        return self.object_id

    @property
    def _highlightResult(self) -> Dict[str, Any]:  # noqa: N802
        return self.highlight_result


@dataclass
class SearchResponse:
    """``POST /1/indexes/{name}/query`` response envelope."""

    hits: List[SearchHit] = field(default_factory=list)
    nb_hits: int = 0
    page: int = 0
    nb_pages: int = 0
    hits_per_page: int = 20
    processing_time_ms: int = 0
    query: str = ""
    params: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def nbHits(self) -> int:  # noqa: N802
        return self.nb_hits

    @property
    def nbPages(self) -> int:  # noqa: N802
        return self.nb_pages

    @property
    def hitsPerPage(self) -> int:  # noqa: N802
        return self.hits_per_page

    @property
    def processingTimeMS(self) -> int:  # noqa: N802
        return self.processing_time_ms


@dataclass
class IndexingResponse:
    """``POST /1/indexes/{name}`` / batch / DELETE response."""

    task_id: int
    object_id: Optional[str] = None
    object_ids: List[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    created_at: Optional[str] = None
    deleted_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def taskID(self) -> int:  # noqa: N802
        return self.task_id

    @property
    def objectID(self) -> Optional[str]:  # noqa: N802
        return self.object_id

    @property
    def objectIDs(self) -> List[str]:  # noqa: N802
        return self.object_ids


@dataclass
class BrowseResponse:
    """``POST /1/indexes/{name}/browse`` paginated full-index export."""

    hits: List[Dict[str, Any]] = field(default_factory=list)
    cursor: Optional[str] = None
    nb_hits: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Synonym:
    """An Algolia synonym record."""

    object_id: str
    type: str  # synonym | onewaysynonym | altcorrection1 | altcorrection2 | placeholder
    synonyms: List[str] = field(default_factory=list)
    input: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """An Algolia merchandising rule record."""

    object_id: str
    description: Optional[str] = None
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    consequence: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)
