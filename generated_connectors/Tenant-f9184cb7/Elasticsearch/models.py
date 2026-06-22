"""Dataclass models for the Elasticsearch connector (with @property shims).

These mirror the most common shapes returned by the Elasticsearch REST API.
The connector returns raw dicts so callers always get the full API surface;
these models exist for typed-construction in tests and helpers, and to expose
camelCase / snake_case shims for the Elasticsearch field naming conventions.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ClusterHealth:
    """`GET /_cluster/health` response envelope."""
    cluster_name: str
    status: str  # "green" | "yellow" | "red"
    number_of_nodes: int = 0
    number_of_data_nodes: int = 0
    active_primary_shards: int = 0
    active_shards: int = 0
    relocating_shards: int = 0
    initializing_shards: int = 0
    unassigned_shards: int = 0
    timed_out: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    # ── @property shims — Elasticsearch uses snake_case in JSON, mirror it ──
    @property
    def clusterName(self) -> str:  # noqa: N802
        return self.cluster_name

    @property
    def numberOfNodes(self) -> int:  # noqa: N802
        return self.number_of_nodes

    @property
    def activePrimaryShards(self) -> int:  # noqa: N802
        return self.active_primary_shards

    @property
    def unassignedShards(self) -> int:  # noqa: N802
        return self.unassigned_shards


@dataclass
class IndexInfo:
    """One entry from `GET /_cat/indices?format=json` -> response[*]."""
    index: str
    health: str = "green"
    status: str = "open"
    uuid: str = ""
    pri: int = 0
    rep: int = 0
    docs_count: int = 0
    docs_deleted: int = 0
    store_size: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def docsCount(self) -> int:  # noqa: N802
        return self.docs_count

    @property
    def docsDeleted(self) -> int:  # noqa: N802
        return self.docs_deleted

    @property
    def storeSize(self) -> str:  # noqa: N802
        return self.store_size


@dataclass
class IndexDocumentResponse:
    """`POST /{index}/_doc` or `PUT /{index}/_doc/{id}` response."""
    _index: str
    _id: str
    _version: int = 0
    result: str = "created"  # "created" | "updated" | "noop"
    _seq_no: int = 0
    _primary_term: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def index(self) -> str:
        return self._index

    @property
    def doc_id(self) -> str:
        return self._id

    @property
    def version(self) -> int:
        return self._version

    @property
    def seqNo(self) -> int:  # noqa: N802
        return self._seq_no

    @property
    def primaryTerm(self) -> int:  # noqa: N802
        return self._primary_term


@dataclass
class SearchHit:
    """One hit from `POST /{index}/_search` -> hits.hits[*]."""
    _index: str
    _id: str
    _score: Optional[float] = None
    _source: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def index(self) -> str:
        return self._index

    @property
    def doc_id(self) -> str:
        return self._id

    @property
    def score(self) -> Optional[float]:
        return self._score

    @property
    def source(self) -> Dict[str, Any]:
        return self._source


@dataclass
class SearchResponse:
    """`POST /{index}/_search` response envelope."""
    took: int = 0
    timed_out: bool = False
    hits: List[SearchHit] = field(default_factory=list)
    total: int = 0
    max_score: Optional[float] = None
    aggregations: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def timedOut(self) -> bool:  # noqa: N802
        return self.timed_out

    @property
    def maxScore(self) -> Optional[float]:  # noqa: N802
        return self.max_score


@dataclass
class BulkResponse:
    """`POST /_bulk` response envelope."""
    took: int = 0
    errors: bool = False
    items: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
