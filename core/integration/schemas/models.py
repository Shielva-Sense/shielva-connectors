"""Integration Builder — Pydantic data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────

class SessionStatus(str, Enum):
    PLANNING = "planning"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    EXECUTING = "executing"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"


class StepType(str, Enum):
    INSTALL_DEPS = "install_deps"
    CONFIGURE_AUTH = "configure_auth"
    SCAFFOLD_CODE = "scaffold_code"
    GENERATE_IMPLEMENTATION_PLAN = "generate_implementation_plan"
    WRITE_CONNECTOR = "write_connector"
    REVIEW_METHODS = "review_methods"
    SMOKE_TEST = "smoke_test"
    WRITE_TESTS = "write_tests"
    GENERATE_TEST_GUIDELINES = "generate_test_guidelines"
    IMPLEMENT_PERSISTENCE = "implement_persistence"
    RUN_TESTS = "run_tests"
    RUN_INTEGRATION_TESTS = "run_integration_tests"
    GENERATE_METADATA = "generate_metadata"
    GENERATE_DOCS = "generate_docs"
    SETUP_INSTRUCTIONS = "setup_instructions"
    COMPLIANCE_CHECK = "compliance_check"
    VERSION_UPGRADE = "version_upgrade"


class StepStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING_VERSION = "pending_version"


# ── Plan models ───────────────────────────────────────────────────────

class PlanStep(BaseModel):
    index: int
    type: StepType
    title: str
    description: str
    estimated_duration_s: int = 30
    config: Dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING


class PlanDocument(BaseModel):
    steps: List[PlanStep] = Field(default_factory=list)
    version: int = 1


# ── Comment ───────────────────────────────────────────────────────────

class StepComment(BaseModel):
    step_index: int
    comment: str
    author: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Execution ─────────────────────────────────────────────────────────

class StepExecutionResult(BaseModel):
    step_index: int
    status: str  # pass | fail | skipped
    output: str = ""
    duration_ms: float = 0.0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ── Generated files ───────────────────────────────────────────────────

class GeneratedFile(BaseModel):
    path: str
    size: int = 0
    language: str = "python"
    quality_score: Optional[float] = None
    content_hash: str = ""


# ── Test results ──────────────────────────────────────────────────────

class TestResults(BaseModel):
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coverage: Optional[float] = None
    details: List[Dict[str, Any]] = Field(default_factory=list)


# ── Method Identity models ───────────────────────────────────────────

class EntityFieldConfig(BaseModel):
    field_name: str
    data_type: str = "string"   # string | number | boolean | date | object | array
    required: bool = False
    default_value: Optional[str] = None
    description: str = ""


class FieldMapping(BaseModel):
    response_path: str          # JSONPath in API response (e.g. "data.transactions[].id")
    entity_field: str           # Target field name in entity
    transform: str = ""         # Optional transform expression


class EntityConfig(BaseModel):
    entity_id: str              # UUID
    collection_name: str
    database_name: str
    connection_string: str = "" # Encrypted/masked in responses
    fields: List[EntityFieldConfig] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MethodIdentityConfig(BaseModel):
    method_name: str
    identity: str = "api_response"   # MethodIdentity value
    auto_detected: bool = True
    entity_id: Optional[str] = None  # Links to EntityConfig when PERSISTENT
    field_mappings: List[FieldMapping] = Field(default_factory=list)
    expected_response_fields: List[Dict[str, Any]] = Field(default_factory=list)


class MongoProvisionConfig(BaseModel):
    connection_string: str
    database_name: str
    connection_tested: bool = False
    tested_at: Optional[datetime] = None


# ── Session (top-level document) ──────────────────────────────────────

class IntegrationSession(BaseModel):
    # ── Identity fields ───────────────────────────────────────────────────────
    # app_id  : stable per-install identifier from the Electron app (MAC-based hash).
    #           Primary key for pre-login sessions. Mapped to tenant post-login.
    # tenant_id / tenant_name : populated after user logs in (via /app/link-tenant).
    #           Optional pre-login so sessions can be created before authentication.
    app_id: Optional[str] = None
    tenant_id: Optional[str] = None
    tenant_name: str = ""   # R2 bucket path prefix — set after login
    provider: str
    service: str
    connector_name: str = ""   # human-readable name for this connector instance
    user_prompt: str = ""
    # ── Run lineage ───────────────────────────────────────────────────────────
    # A session is one "run" of the builder against a connector. The first run is
    # a "build"; subsequent "enhance" runs link back to the originating build via
    # parent_session_id. Each run owns its OWN scratch state (plan, stepper, exec
    # results, tests, draft docs) — runs never clobber each other. The PUBLISHED
    # artifact (generated_connectors/{tenant}/{name}_connector) is name-keyed and
    # shared, so an enhance run updates the same canonical connector.
    run_kind: str = "build"                       # "build" | "enhance"
    parent_session_id: Optional[str] = None       # set on enhance runs → originating build session id
    status: SessionStatus = SessionStatus.PLANNING
    plan: PlanDocument = Field(default_factory=PlanDocument)
    comments: List[StepComment] = Field(default_factory=list)
    execution_results: List[StepExecutionResult] = Field(default_factory=list)
    generated_files: List[GeneratedFile] = Field(default_factory=list)
    test_results: Optional[TestResults] = None
    version_upgrade_pending: Optional[Dict[str, Any]] = None  # set while waiting for user version input
    version_upgrade_at: Optional[datetime] = None  # when version was last upgraded
    version_upgraded_from: Optional[str] = None   # previous version (for "Upgraded to vX" badge)
    # Conversation history for Claude recall — each entry is {role: "user"|"assistant", content: str}
    # Persisted across initial plan generation and replans so Claude recalls prior context.
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)
    docs_urls: List[str] = Field(default_factory=list)   # provider doc URLs to fetch + synthesize
    custom_rules_md: str = ""                             # user-supplied Markdown rules
    default_config: List[Dict[str, Any]] = Field(default_factory=list)  # user binding decisions per field
    selected_features: List[str] = Field(default_factory=list)           # feature IDs the user has selected
    selected_config_keys: List[str] = Field(default_factory=list)        # config field keys user wants as install_fields (user-provided)
    test_type: str = "unit"                                               # "unit" | "both" — test mode chosen in Review Plan
    # Method identity + entity configuration
    method_identities: List[Dict[str, Any]] = Field(default_factory=list)
    entity_configs: List[Dict[str, Any]] = Field(default_factory=list)
    mongo_provision: Optional[Dict[str, Any]] = None
    llm_model: str = ""                                      # preferred Claude model ID for this session
    # Stepper UI progress — highest wizard tab index the user has reached.
    # Scoped per session (= per tenant + connector) so navigation state survives reloads.
    stepper_max_step: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── API request / response helpers ────────────────────────────────────

class CreateSessionRequest(BaseModel):
    provider: str
    service: str
    connector_name: str = ""
    user_prompt: str = ""
    docs_urls: List[str] = Field(default_factory=list)   # provider doc URLs to fetch + synthesize
    custom_rules_md: str = ""                             # user-supplied Markdown rules (ingested as-is)
    llm_model: str = ""                                   # preferred Claude model (e.g. claude-sonnet-4-5)


class ReplanRequest(BaseModel):
    step_index: int
    comment: str
