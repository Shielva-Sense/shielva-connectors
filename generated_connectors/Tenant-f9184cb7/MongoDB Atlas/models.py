"""Pydantic request/response schemas for the MongoDB Atlas Admin API.

camelCase aliases match Atlas wire format; the connector boundary uses
``Dict[str, Any]`` payloads but exposes these models so callers building
ad-hoc requests have a typed scaffold.

Local dataclass mirrors of common Atlas resources (kept for SDK-free use in
test harnesses) live at the bottom of this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _AtlasModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Pagination(_AtlasModel):
    """Atlas pagination query params (`pageNum` / `itemsPerPage`)."""
    page_num: int = Field(default=1, alias="pageNum")
    items_per_page: int = Field(default=100, alias="itemsPerPage")


class ProviderSettings(_AtlasModel):
    provider_name: str = Field(default="AWS", alias="providerName")
    region_name: str = Field(default="US_EAST_1", alias="regionName")
    instance_size_name: str = Field(default="M10", alias="instanceSizeName")


class CreateProjectRequest(_AtlasModel):
    name: str
    org_id: str = Field(alias="orgId")
    with_default_alerts_settings: bool = Field(
        default=True, alias="withDefaultAlertsSettings"
    )


class CreateClusterRequest(_AtlasModel):
    name: str
    cluster_type: str = Field(default="REPLICASET", alias="clusterType")
    num_shards: int = Field(default=1, alias="numShards")
    mongo_db_major_version: str = Field(default="7.0", alias="mongoDBMajorVersion")
    provider_settings: ProviderSettings = Field(
        default_factory=ProviderSettings, alias="providerSettings"
    )


class DatabaseUserRole(_AtlasModel):
    database_name: str = Field(default="admin", alias="databaseName")
    role_name: str = Field(alias="roleName")


class CreateDatabaseUserRequest(_AtlasModel):
    username: str
    password: str
    database_name: str = Field(default="admin", alias="databaseName")
    roles: List[DatabaseUserRole] = Field(default_factory=list)
    scopes: List[Dict[str, Any]] = Field(default_factory=list)


class NetworkAccessEntry(_AtlasModel):
    cidr_block: Optional[str] = Field(default=None, alias="cidrBlock")
    ip_address: Optional[str] = Field(default=None, alias="ipAddress")
    aws_security_group: Optional[str] = Field(default=None, alias="awsSecurityGroup")
    comment: Optional[str] = None


class AtlasOrgResponse(_AtlasModel):
    org_id: str = Field(alias="id")
    name: str
    is_deleted: bool = Field(default=False, alias="isDeleted")
    links: List[Dict[str, Any]] = Field(default_factory=list)


class AtlasProjectResponse(_AtlasModel):
    project_id: str = Field(alias="id")
    name: str
    org_id: str = Field(alias="orgId")
    cluster_count: int = Field(default=0, alias="clusterCount")
    created: Optional[datetime] = None


class AtlasClusterResponse(_AtlasModel):
    cluster_id: Optional[str] = Field(default=None, alias="id")
    name: str
    cluster_type: str = Field(default="REPLICASET", alias="clusterType")
    state_name: str = Field(default="IDLE", alias="stateName")
    mongo_db_version: Optional[str] = Field(default=None, alias="mongoDBVersion")
    connection_strings: Dict[str, Any] = Field(
        default_factory=dict, alias="connectionStrings"
    )


class PaginatedResponse(_AtlasModel):
    """Generic Atlas paginated envelope."""
    results: List[Dict[str, Any]] = Field(default_factory=list)
    total_count: int = Field(default=0, alias="totalCount")
    links: List[Dict[str, Any]] = Field(default_factory=list)


# ── Lightweight dataclass mirrors (no pydantic dependency) ─────────────────


@dataclass
class AtlasStatus:
    """Local AtlasStatus record with health + auth shims.

    Mirrors the shape of ``ConnectorStatus`` from ``shared.base_connector``
    but is safe to import / use even when the SDK is unavailable.
    """

    connector_id: str
    health: str = "healthy"
    auth_status: str = "pending"
    message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def auth_status_value(self) -> str:
        val = self.auth_status
        return getattr(val, "value", str(val))

    @property
    def health_value(self) -> str:
        val = self.health
        return getattr(val, "value", str(val))


@dataclass
class AtlasOrg:
    id: str
    name: str
    is_deleted: bool = False
    links: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AtlasProject:
    id: str
    name: str
    org_id: str
    cluster_count: int = 0
    created: Optional[str] = None


@dataclass
class AtlasCluster:
    id: str
    name: str
    cluster_type: str = "REPLICASET"
    state_name: str = "IDLE"
    mongo_db_version: Optional[str] = None
    connection_strings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AtlasDatabaseUser:
    username: str
    database_name: str = "admin"
    roles: List[Dict[str, Any]] = field(default_factory=list)
    scopes: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AtlasNetworkAccessEntry:
    cidr_block: Optional[str] = None
    ip_address: Optional[str] = None
    aws_security_group: Optional[str] = None
    comment: Optional[str] = None
