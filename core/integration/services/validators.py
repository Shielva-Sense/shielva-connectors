"""Integration Builder — Static code validators for generated connectors.

Security-first validation: blocks dangerous patterns, verifies BaseConnector
compliance, and checks for tenant isolation.
"""

import ast
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Dangerous patterns ───────────────────────────────────────────────

BLOCKED_IMPORTS = {
    "os.system",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.run",
    "eval",
    "exec",
    "compile",
    "__import__",
    "importlib.import_module",
}

BLOCKED_CALLS = {
    "os.system",
    "os.popen",
    "os.exec",
    "os.execvp",
    "os.execve",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.run",
    "subprocess.check_output",
    "eval",
    "exec",
    "compile",
    "open",  # file I/O outside sandbox should be flagged
}

DANGEROUS_PATTERNS = [
    r"os\.environ\[",  # direct env mutation
    r"shutil\.rmtree",  # mass deletion
    r"__builtins__",  # builtin manipulation
    r"ctypes\.",  # C interop
    r"pickle\.loads",  # deserialization
    r"yaml\.load\(",  # unsafe YAML
    r"socket\.",  # raw networking
]


# ── Validators ───────────────────────────────────────────────────────


def validate_syntax(code: str, filename: str = "<generated>") -> dict[str, Any]:
    """Check Python syntax validity.

    Returns: {valid: bool, error: str|None, line: int|None}
    """
    try:
        ast.parse(code, filename=filename)
        return {"valid": True, "error": None, "line": None}
    except SyntaxError as exc:
        return {"valid": False, "error": str(exc), "line": exc.lineno}


def validate_imports(code: str) -> dict[str, Any]:
    """Check for dangerous/blocked imports and function calls.

    Returns: {safe: bool, blocked: list[str], warnings: list[str]}
    """
    blocked = []
    warnings = []

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "safe": False,
            "blocked": [],
            "warnings": ["Syntax error — cannot analyze imports"],
        }

    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full in BLOCKED_IMPORTS or node.module in ("subprocess", "ctypes"):
                    blocked.append(f"Import blocked: {full} (line {node.lineno})")

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("subprocess", "ctypes", "pickle"):
                    blocked.append(f"Import blocked: {alias.name} (line {node.lineno})")

        # Check function calls
        if isinstance(node, ast.Call):
            func_name = _get_call_name(node)
            if func_name and func_name in BLOCKED_CALLS:
                blocked.append(f"Blocked call: {func_name}() (line {node.lineno})")

    # Regex pattern checks
    for pattern in DANGEROUS_PATTERNS:
        matches = re.finditer(pattern, code)
        for m in matches:
            line_num = code[: m.start()].count("\n") + 1
            warnings.append(f"Suspicious pattern: {m.group()} (line {line_num})")

    return {
        "safe": len(blocked) == 0,
        "blocked": blocked,
        "warnings": warnings,
    }


def _extract_auth_type(tree: ast.AST) -> str:
    """Extract AUTH_TYPE class attribute value from parsed AST."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id == "AUTH_TYPE":
                            if isinstance(item.value, ast.Constant):
                                return str(item.value.value)
    return ""


def validate_base_connector_compliance(code: str) -> dict[str, Any]:
    """Check that generated code inherits BaseConnector and implements required methods.

    authorize() is ONLY required for oauth2_code and oauth2_pkce auth types — the base
    class handles all other OAuth2 flows automatically.

    Returns: {compliant: bool, missing_methods: list[str], has_class: bool, class_name: str, auth_type: str}
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "compliant": False,
            "missing_methods": [],
            "has_class": False,
            "class_name": "",
            "auth_type": "",
        }

    # Always required
    required_methods = {"install", "health_check", "sync"}

    # authorize() only required for authorization-code flows
    auth_type = _extract_auth_type(tree)
    if auth_type in ("oauth2_code", "oauth2_pkce"):
        required_methods.add("authorize")

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            base_names = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.append(base.attr)

            if "BaseConnector" in base_names:
                methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                missing = required_methods - methods
                return {
                    "compliant": len(missing) == 0,
                    "missing_methods": sorted(missing),
                    "has_class": True,
                    "class_name": node.name,
                    "auth_type": auth_type,
                }

    return {
        "compliant": False,
        "missing_methods": sorted(required_methods),
        "has_class": False,
        "class_name": "",
        "auth_type": auth_type,
    }


def validate_tenant_isolation(code: str) -> dict[str, Any]:
    """Check that generated code properly uses tenant_id for data isolation.

    Returns: {isolated: bool, warnings: list[str]}
    """
    warnings = []

    # Check for hardcoded tenant references
    hardcoded_patterns = [
        (r'tenant_id\s*=\s*["\'][^"\']+["\']', "Hardcoded tenant_id assignment"),
        (r'["\']shielva-platform["\']', "Hardcoded 'shielva-platform' reference"),
        (r'["\']Tenant-\w+["\']', "Hardcoded Tenant-xxx reference"),
    ]

    for pattern, desc in hardcoded_patterns:
        matches = re.finditer(pattern, code)
        for m in matches:
            line_num = code[: m.start()].count("\n") + 1
            warnings.append(f"{desc}: {m.group()[:50]} (line {line_num})")

    # Verify self.tenant_id usage
    uses_tenant = "self.tenant_id" in code
    if not uses_tenant:
        warnings.append("Code does not reference self.tenant_id — may lack tenant isolation")

    return {
        "isolated": len(warnings) == 0,
        "warnings": warnings,
    }


_OAUTH2_FLOW_TYPES = {
    "oauth2_code",
    "oauth2_pkce",
    "oauth2_client_credentials",
    "oauth2_password",
    "oauth2_device",
}

# Field names that Gemini commonly hallucinates
_WRONG_SYNC_FIELDS = [
    "docs_synced",
    "synced",
    "count",
    "sync_count",
    "num_synced",
    "docs_failed",
    "failed_count",
    "num_failed",
]
_WRONG_DOC_FIELDS = ["doc_id", "document_id", "docid", "uid", "uuid"]
_WRONG_INSTALL_PARAMS = re.compile(r"async def install\s*\(\s*self\s*,\s*config")
_WRONG_SYNC_PARAM = re.compile(r"def sync\s*\([^)]*full_sync\s*[=:]")
_CREDENTIAL_ENV = re.compile(
    r"os\.getenv\s*\(\s*['\"](?:client_id|client_secret|api_key|token|secret|password|credential)",
    re.IGNORECASE,
)


def validate_oauth_constants(code: str) -> dict[str, Any]:
    """For OAuth2 connectors, verify AUTH_URI and TOKEN_URI class attributes are set.

    Missing these causes a silent runtime failure: BaseConnector.get_oauth_url() raises
    'auth_uri is not set' without them.

    Returns: {valid: bool, warnings: list[str]}
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"valid": True, "warnings": []}  # syntax validator handles this

    auth_type = _extract_auth_type(tree)
    if auth_type not in _OAUTH2_FLOW_TYPES:
        return {"valid": True, "warnings": []}

    # Collect class-level attribute names
    attr_names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            attr_names.add(t.id)

    warnings = []
    if "AUTH_URI" not in attr_names:
        warnings.append(
            f"AUTH_URI not set — required for {auth_type}. BaseConnector.get_oauth_url() will raise 'auth_uri is not set'."
        )
    if "TOKEN_URI" not in attr_names:
        warnings.append(f"TOKEN_URI not set — required for {auth_type}. Token exchange will fail.")

    return {"valid": len(warnings) == 0, "warnings": warnings}


def validate_field_names(code: str) -> dict[str, Any]:
    """Check for wrong SyncResult / NormalizedDocument field names that cause TypeError at runtime.

    Returns: {valid: bool, warnings: list[str]}
    """
    warnings = []
    for wrong in _WRONG_SYNC_FIELDS:
        if re.search(rf"\b{re.escape(wrong)}\s*=", code):
            warnings.append(
                f"Wrong SyncResult field '{wrong}' — use documents_synced / documents_failed / documents_found"
            )
    for wrong in _WRONG_DOC_FIELDS:
        if re.search(rf"\b{re.escape(wrong)}\s*=", code):
            warnings.append(f"Wrong NormalizedDocument field '{wrong}' — use 'id' (not doc_id / document_id)")
    return {"valid": len(warnings) == 0, "warnings": warnings}


def validate_method_signatures(code: str) -> dict[str, Any]:
    """Check install() and sync() have the correct signatures.

    install(self, config) → silent bug: config is always None, credentials are ignored
    sync(... full_sync=...) → silent TypeError: unexpected keyword argument

    Returns: {valid: bool, warnings: list[str]}
    """
    warnings = []
    if _WRONG_INSTALL_PARAMS.search(code):
        warnings.append(
            "install(self, config) detected — MUST be install(self). Config comes from self.config; passing config param means it is always None at runtime."
        )
    if _WRONG_SYNC_PARAM.search(code):
        warnings.append(
            "sync() uses 'full_sync' parameter — MUST be 'full'. Gateway calls sync(full=True); wrong name causes a silent TypeError."
        )
    return {"valid": len(warnings) == 0, "warnings": warnings}


def validate_credential_sourcing(code: str) -> dict[str, Any]:
    """Warn when credentials are read from os.getenv() instead of self.config.

    All credentials must come from self.config (populated by the gateway via install()).
    os.getenv() credentials are invisible to the multi-tenant gateway and will be empty
    in production.

    Returns: {valid: bool, warnings: list[str]}
    """
    warnings = []
    for m in _CREDENTIAL_ENV.finditer(code):
        line_num = code[: m.start()].count("\n") + 1
        warnings.append(f"os.getenv() for credential detected (line {line_num}) — read from self.config instead.")
    return {"valid": len(warnings) == 0, "warnings": warnings}


def validate_all(code: str, filename: str = "<generated>") -> dict[str, Any]:
    """Run all validators and return aggregated results."""
    syntax = validate_syntax(code, filename)
    if not syntax["valid"]:
        return {
            "valid": False,
            "syntax": syntax,
            "imports": {
                "safe": False,
                "blocked": [],
                "warnings": ["Cannot analyze — syntax error"],
            },
            "compliance": {
                "compliant": False,
                "missing_methods": [],
                "has_class": False,
            },
            "isolation": {
                "isolated": False,
                "warnings": ["Cannot analyze — syntax error"],
            },
        }

    imports = validate_imports(code)
    compliance = validate_base_connector_compliance(code)
    isolation = validate_tenant_isolation(code)
    oauth_constants = validate_oauth_constants(code)
    field_names = validate_field_names(code)
    signatures = validate_method_signatures(code)
    credentials = validate_credential_sourcing(code)

    # Hard gates: syntax, dangerous imports, missing required methods, OAuth2 missing constants.
    # Field names, signatures, and credential sourcing are warnings — the test suite is the
    # real enforcement gate; blocking here would reject connectors with minor cosmetic issues.
    overall = (
        syntax["valid"]
        and imports["safe"]
        and compliance["compliant"]
        and isolation["isolated"]
        and oauth_constants["valid"]
    )

    # Aggregate all non-blocking warnings for UI display
    all_warnings: list[str] = (
        imports.get("warnings", [])
        + isolation.get("warnings", [])
        + field_names.get("warnings", [])
        + signatures.get("warnings", [])
        + credentials.get("warnings", [])
    )

    return {
        "valid": overall,
        "syntax": syntax,
        "imports": imports,
        "compliance": compliance,
        "isolation": isolation,
        "oauth_constants": oauth_constants,
        "field_names": field_names,
        "signatures": signatures,
        "credentials": credentials,
        "warnings": all_warnings,
    }


# ── Helpers ──────────────────────────────────────────────────────────


def _get_call_name(node: ast.Call) -> str:
    """Extract the dotted name of a function call."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        parts = []
        current = node.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""
