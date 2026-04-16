"""Integration Builder — Method identity detection + persistence code generation.

Provides:
1. auto_detect_identities() — AST-based heuristic classification
2. predict_response_fields() — AI-powered response field prediction
3. generate_persistence_code() — AI-powered code generation for DB persistence
"""

import ast
from pathlib import Path
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger(__name__)


async def predict_response_fields(method_source: str, method_name: str) -> List[Dict[str, Any]]:
    """Analyze a method's source to predict response field structure.

    Uses AST analysis first, then falls back to LLM for ambiguous cases.
    Returns a list of {path, inferred_type, description} dicts.
    """
    fields: List[Dict[str, Any]] = []
    seen_paths: set = set()

    def _add_field(path: str, description: str) -> None:
        if path not in seen_paths:
            seen_paths.add(path)
            fields.append({"path": path, "inferred_type": "string", "description": description})

    # Try AST-based extraction from return type hints and docstring
    try:
        # Parse method body for dict key patterns
        tree = ast.parse(method_source)
        for node in ast.walk(tree):
            # Look for dict literal returns: return {"key": value, ...}
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        _add_field(key.value, f"Response field: {key.value}")

            # Look for dict subscript access: response["key"]
            if isinstance(node, ast.Subscript):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    _add_field(node.slice.value, f"Accessed field: {node.slice.value}")

            # Look for .get("key") calls
            if isinstance(node, ast.Call):
                try:
                    call_str = ast.unparse(node.func)
                    if ".get(" in call_str and node.args:
                        arg = node.args[0]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            _add_field(arg.value, f"Accessed field: {arg.value}")
                except Exception:
                    pass
    except SyntaxError:
        pass

    # If AST didn't find much, try LLM
    if len(fields) < 2:
        try:
            from integration.services.llm_client import call_llm

            prompt = f"""Analyze this Python connector method and predict the response fields it returns.
Return a JSON array of objects with: path (string), inferred_type (string/number/boolean/date/object/array), description (string).

Method name: {method_name}
Source code:
```python
{method_source}
```

Return ONLY the JSON array, no other text."""

            llm_response = await call_llm(
                messages=[{"role": "user", "content": prompt}],
                model="gemini-2.0-flash-lite",
                temperature=0.1,
            )

            import json
            content = llm_response.get("content", "")
            # Extract JSON from response
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            parsed = json.loads(content.strip())
            if isinstance(parsed, list):
                fields = parsed
        except Exception as e:
            logger.warning("identity.llm_predict_failed", method=method_name, error=str(e))

    return fields


async def generate_persistence_code(
    method_source: str,
    entity_config: Dict[str, Any],
    field_mappings: List[Dict[str, Any]],
) -> str:
    """Generate persistence code for a method + entity pair.

    Returns Python code string that:
    1. Defines a _persist_{method}_result helper
    2. Shows the modified method body that calls the helper
    """
    collection_name = entity_config.get("collection_name", "results")
    database_name = entity_config.get("database_name", "connector_data")

    # Build mapping lines
    mapping_lines = []
    for fm in field_mappings:
        resp_path = fm.get("response_path", "")
        entity_field = fm.get("entity_field", "")
        transform = fm.get("transform", "")
        if transform:
            mapping_lines.append(f'        "{entity_field}": {transform},')
        else:
            mapping_lines.append(f'        "{entity_field}": response.get("{resp_path}"),')

    mappings_str = "\n".join(mapping_lines) if mapping_lines else '        # Add field mappings here'

    code = f'''# ── Auto-generated persistence code ──────────────────────────────────
# Entity: {collection_name} in {database_name}

from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime


async def _persist_result(self, response: dict) -> str:
    """Persist API response to MongoDB entity."""
    client = AsyncIOMotorClient(self.config.get("mongo_connection_string"))
    db = client["{database_name}"]
    collection = db["{collection_name}"]

    document = {{
{mappings_str}
        "tenant_id": self.tenant_id,
        "connector_id": self.connector_id,
        "created_at": datetime.utcnow(),
    }}

    result = await collection.insert_one(document)
    client.close()
    return str(result.inserted_id)


# Usage in your method:
# result = await self.client.some_api_call(...)
# doc_id = await self._persist_result(result)
# return {{**result, "_persisted_id": doc_id}}
'''

    # If we have enough context, try LLM for better code
    if field_mappings and len(field_mappings) > 0:
        try:
            from integration.services.llm_client import call_llm

            prompt = f"""Generate a Python async helper method that persists API response data to MongoDB.

Collection: {collection_name}
Database: {database_name}
Field mappings: {json.dumps(field_mappings, indent=2)}

Original method source:
```python
{method_source}
```

Requirements:
- Use Motor async driver
- Include tenant_id and connector_id from self
- Include created_at timestamp
- Return the inserted document ID
- Handle errors gracefully with logging

Return ONLY the Python code, no markdown or explanation."""

            import json
            llm_response = await call_llm(
                messages=[{"role": "user", "content": prompt}],
                model="gemini-2.0-flash-lite",
                temperature=0.2,
            )
            llm_code = llm_response.get("content", "")
            if llm_code and "async def" in llm_code:
                # Clean markdown fences
                if "```python" in llm_code:
                    llm_code = llm_code.split("```python")[1].split("```")[0]
                elif "```" in llm_code:
                    llm_code = llm_code.split("```")[1].split("```")[0]
                return llm_code.strip()
        except Exception as e:
            logger.warning("identity.codegen_llm_failed", error=str(e))

    return code
