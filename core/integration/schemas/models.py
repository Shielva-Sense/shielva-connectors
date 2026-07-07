"""Integration Builder — Pydantic data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

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
    INACTIVE = "inactive"


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
    config: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING


class PlanDocument(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)
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
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ── Generated files ───────────────────────────────────────────────────


class GeneratedFile(BaseModel):
    path: str
    size: int = 0
    language: str = "python"
    quality_score: float | None = None
    content_hash: str = ""


# ── Test results ──────────────────────────────────────────────────────


class TestResults(BaseModel):
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coverage: float | None = None
    details: list[dict[str, Any]] = Field(default_factory=list)


# ── Method Identity models ───────────────────────────────────────────


class EntityFieldConfig(BaseModel):
    field_name: str
    data_type: str = "string"  # string | number | boolean | date | object | array
    required: bool = False
    default_value: str | None = None
    description: str = ""


class FieldMapping(BaseModel):
    response_path: str  # JSONPath in API response (e.g. "data.transactions[].id")
    entity_field: str  # Target field name in entity
    transform: str = ""  # Optional transform expression


class EntityConfig(BaseModel):
    entity_id: str  # UUID
    collection_name: str
    database_name: str
    connection_string: str = ""  # Encrypted/masked in responses
    fields: list[EntityFieldConfig] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MethodIdentityConfig(BaseModel):
    method_name: str
    identity: str = "api_response"  # MethodIdentity value
    auto_detected: bool = True
    entity_id: str | None = None  # Links to EntityConfig when PERSISTENT
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    expected_response_fields: list[dict[str, Any]] = Field(default_factory=list)


class MongoProvisionConfig(BaseModel):
    connection_string: str
    database_name: str
    connection_tested: bool = False
    tested_at: datetime | None = None


# ── Session (top-level document) ──────────────────────────────────────


class IntegrationSession(BaseModel):
    # ── Identity fields ───────────────────────────────────────────────────────
    # app_id  : stable per-install identifier from the Electron app (MAC-based hash).
    #           Primary key for pre-login sessions. Mapped to tenant post-login.
    # tenant_id / tenant_name : populated after user logs in (via /app/link-tenant).
    #           Optional pre-login so sessions can be created before authentication.
    # Fields tagged `enhance_inherit=True` are connector-IDENTITY / generation-INPUT
    # config (dynamic per connector). An enhance run is a fresh run of the SAME
    # connector, so it copies exactly these from its parent — driven by the tag, never
    # a hardcoded list in the route handler (see `enhance_inherited_fields()`). Run
    # SCRATCH (plan, exec results, tests, status, slug, …) is intentionally NOT tagged
    # so each run starts clean.
    app_id: str | None = Field(default=None, json_schema_extra={"enhance_inherit": True})
    tenant_id: str | None = Field(default=None, json_schema_extra={"enhance_inherit": True})
    tenant_name: str = Field(
        default="", json_schema_extra={"enhance_inherit": True}
    )  # R2 bucket path prefix — set after login
    provider: str = Field(json_schema_extra={"enhance_inherit": True})
    service: str = Field(json_schema_extra={"enhance_inherit": True})
    connector_name: str = Field(
        default="", json_schema_extra={"enhance_inherit": True}
    )  # human-readable name for this connector instance
    alias_name: str = Field(
        default="", json_schema_extra={"enhance_inherit": True}
    )  # user-editable display alias (defaults to connector_name; connector_name is never mutated)
    auth_type: str = Field(
        default="", json_schema_extra={"enhance_inherit": True}
    )  # oauth2_code | api_key | … — drives the CONNECTOR_GEN_SYSTEM overlay at codegen time
    user_prompt: str = ""
    # ── Run lineage ───────────────────────────────────────────────────────────
    # A session is one "run" of the builder against a connector. The first run is
    # a "build"; subsequent "enhance" runs link back to the originating build via
    # parent_session_id. Each run owns its OWN scratch state (plan, stepper, exec
    # results, tests, draft docs) — runs never clobber each other. The PUBLISHED
    # artifact (generated_connectors/{tenant}/{name}_connector) is name-keyed and
    # shared, so an enhance run updates the same canonical connector.
    run_kind: str = "build"  # "build" | "enhance"
    parent_session_id: str | None = None  # set on enhance runs → originating build session id
    status: SessionStatus = SessionStatus.PLANNING
    plan: PlanDocument = Field(default_factory=PlanDocument)
    comments: list[StepComment] = Field(default_factory=list)
    execution_results: list[StepExecutionResult] = Field(default_factory=list)
    generated_files: list[GeneratedFile] = Field(default_factory=list)
    test_results: TestResults | None = None
    version_upgrade_pending: dict[str, Any] | None = None  # set while waiting for user version input
    version_upgrade_at: datetime | None = None  # when version was last upgraded
    version_upgraded_from: str | None = None  # previous version (for "Upgraded to vX" badge)
    # Conversation history for Claude recall — each entry is {role: "user"|"assistant", content: str}
    # Persisted across initial plan generation and replans so Claude recalls prior context.
    conversation_history: list[dict[str, str]] = Field(default_factory=list)
    docs_urls: list[str] = Field(
        default_factory=list, json_schema_extra={"enhance_inherit": True}
    )  # provider doc URLs to fetch + synthesize
    custom_rules_md: str = Field(
        default="", json_schema_extra={"enhance_inherit": True}
    )  # user-supplied Markdown rules
    default_config: list[dict[str, Any]] = Field(
        default_factory=list, json_schema_extra={"enhance_inherit": True}
    )  # user binding decisions per field
    selected_features: list[str] = Field(
        default_factory=list
    )  # feature IDs the user has selected — fresh per run (enhance picks new ones)
    selected_config_keys: list[str] = Field(
        default_factory=list, json_schema_extra={"enhance_inherit": True}
    )  # config field keys user wants as install_fields (user-provided) — drives the install_fields/constant split
    test_type: str = Field(
        default="unit", json_schema_extra={"enhance_inherit": True}
    )  # "unit" | "both" — test mode chosen in Review Plan
    # Method identity + entity configuration
    method_identities: list[dict[str, Any]] = Field(default_factory=list, json_schema_extra={"enhance_inherit": True})
    entity_configs: list[dict[str, Any]] = Field(default_factory=list, json_schema_extra={"enhance_inherit": True})
    mongo_provision: dict[str, Any] | None = None
    llm_model: str = Field(
        default="", json_schema_extra={"enhance_inherit": True}
    )  # preferred Claude model ID for this session
    # Stepper UI progress — highest wizard tab index the user has reached.
    # Scoped per session (= per tenant + connector) so navigation state survives reloads.
    stepper_max_step: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def enhance_inherited_fields(cls) -> list[str]:
        """Field names an enhance run copies from its parent — derived from the
        per-field `enhance_inherit` tag, NOT a hardcoded list. Add a new
        connector-config field with `json_schema_extra={"enhance_inherit": True}`
        and it is inherited automatically; no change to the enhance handler."""
        return [
            name
            for name, f in cls.model_fields.items()
            if isinstance(f.json_schema_extra, dict) and f.json_schema_extra.get("enhance_inherit")
        ]


# ── API request / response helpers ────────────────────────────────────


class CreateSessionRequest(BaseModel):
    provider: str
    service: str
    connector_name: str = ""
    user_prompt: str = ""
    docs_urls: list[str] = Field(default_factory=list)  # provider doc URLs to fetch + synthesize
    custom_rules_md: str = ""  # user-supplied Markdown rules (ingested as-is)
    llm_model: str = ""  # preferred Claude model (e.g. claude-sonnet-4-5)


class ReplanRequest(BaseModel):
    step_index: int
    comment: str
