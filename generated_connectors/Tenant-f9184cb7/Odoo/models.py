"""Pydantic request/response schemas for the Odoo JSON-RPC envelope.

These models describe the JSON-RPC 2.0 wire format. The connector boundary
itself uses plain Dict[str, Any] payloads; the schemas are exposed for
operators (and downstream tools) that want a typed reference.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _OdooModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class RpcParams(_OdooModel):
    """params block of a JSON-RPC ``call`` envelope."""

    service: str = "object"
    method: str = "execute_kw"
    args: List[Any] = Field(default_factory=list)


class RpcEnvelope(_OdooModel):
    """A full JSON-RPC 2.0 call envelope."""

    jsonrpc: str = "2.0"
    method: str = "call"
    params: RpcParams = Field(default_factory=RpcParams)
    id: Any = 1


class RpcErrorData(_OdooModel):
    """``error.data`` — Odoo's exception payload."""

    name: Optional[str] = None
    message: Optional[str] = None
    arguments: List[Any] = Field(default_factory=list)
    debug: Optional[str] = None


class RpcError(_OdooModel):
    code: Optional[int] = None
    message: Optional[str] = None
    data: Optional[RpcErrorData] = None


class RpcResponse(_OdooModel):
    """JSON-RPC 2.0 response — either ``result`` xor ``error``."""

    jsonrpc: str = "2.0"
    id: Any = None
    result: Optional[Any] = None
    error: Optional[RpcError] = None


class SearchReadRequest(_OdooModel):
    """Convenience shape for ``execute_kw(model, 'search_read', ...)``."""

    model: str
    domain: List[Any] = Field(default_factory=list)
    fields: List[str] = Field(default_factory=list)
    limit: int = 100
    offset: int = 0


class PageResult(_OdooModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_offset: Optional[int] = None
