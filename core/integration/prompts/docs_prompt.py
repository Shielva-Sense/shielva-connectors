"""Integration Builder — LLM prompts for connector documentation generation.

Generates structured JSON documentation matching the SiteRenderer schema.
The frontend renders this JSON as navigable documentation pages.
"""

# ── Documentation generation prompt ─────────────────────────────────

DOCS_GENERATION_PROMPT = """\
You are a senior technical writer generating production-quality connector documentation for the Shielva platform.

## Output Format
A single raw JSON object — no text before or after, no markdown fences.
Structure: {{"title": "<ConnectorName> Documentation", "sections": [...]}}
Each section: {{"id": "kebab-id", "title": "Title", "content": "Markdown", "children": [...optional]}}

## Connector: {connector_name}

### connector.py
```python
{connector_code}
```

### tests
```python
{test_code}
```

### config.py
```python
{config_code}
```

### metadata/connector.json
```json
{connector_json}
```

### requirements.txt
```
{requirements}
```

## What to write

Generate these sections — every word must be specific to THIS connector, not generic:

1. **Overview** — What this connector does, what data it syncs or sends, who uses it. Use the actual service name and real capabilities from the code above.

2. **Quick Start** — Step-by-step: install → authenticate → first sync. Use the real config field names from `connector.json` install_fields.

3. **Authentication** — Exact flow based on AUTH_TYPE in connector.py. Real scope names, real provider dashboard URLs if determinable from AUTH_URI/TOKEN_URI class attributes.

4. **Configuration** — One row per config field found in `self.config.get(...)` calls in connector.py. Column: Field | Type | Required | Description.

5. **API Methods** — One child section per public method in connector.py (install, sync, health_check + any custom methods). Include: what it does, key parameters, what it returns, example code using the real class name.

6. **Error Handling** — List the specific HTTP status codes the connector handles (read from connector.py). What each means and what the connector does about it.

7. **Troubleshooting** — Real failure modes: expired token, wrong scopes, rate limiting. Concrete fix for each based on the actual connector implementation.

## Quality rules
- NEVER write placeholder text like "[Describe...]", "Contact your administrator", "See documentation"
- NEVER invent API details that aren't in the source code or metadata
- Every example must use the actual class name from connector.py
- Every config field must match what connector.py reads from self.config

{user_prompt}

Output the JSON now.
"""

# ── Documentation update prompt ──────────────────────────────────────

DOCS_UPDATE_PROMPT = """\
You are updating existing connector documentation for the Shielva platform.

The documentation is a JSON object with a `title` string and a `sections` array.
Each section has `id`, `title`, `content` (Markdown), and optional `children` array.

## Current Documentation JSON

{current_docs_json}

## User's Update Request

{user_prompt}

## Rules

1. Output ONLY the complete updated JSON object — no text before or after.
2. Do NOT wrap the output in markdown code fences.
3. Preserve all sections the user did not ask to change.
4. Keep all `id` values stable unless the user explicitly asks to rename sections.
5. If the user asks to add a new section, insert it at a logical position.
6. If the user asks to remove a section, remove it from the array.
7. Maintain valid JSON structure at all times.
8. Content fields use Markdown formatting.

Now output the complete updated documentation JSON.
"""
