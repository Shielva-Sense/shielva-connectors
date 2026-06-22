"""Integration Builder — Instruction Setup Guidelines service.

Manages the INSTRUCTION_SETUP_GUIDELINES document stored in R2 at:
  {R2_COLLECTION_PREFIX}/INSTRUCTION_SETUP_GUIDELINES  (e.g. "integration-plans/INSTRUCTION_SETUP_GUIDELINES")

This is the generic structure/format guideline that Gemini uses when
generating connector-specific setup instructions. It defines the expected
markdown structure, tone, and content requirements.

Connector-specific customization happens at runtime via Gemini researching
the actual provider portal and credentials.
"""

from typing import Any, Dict, Optional

import structlog

from integration.services import r2_service

logger = structlog.get_logger(__name__)

# ── R2 key for the guidelines document ────────────────────────────────

_GUIDELINES_KEY = "INSTRUCTION_SETUP_GUIDELINES"

# ── Default guidelines content ────────────────────────────────────────

DEFAULT_INSTRUCTION_SETUP_GUIDELINES = """# Shielva Connector Setup Instructions — Structure Guidelines

## Purpose
These guidelines define the **mandatory structure and content standards** for
`instructions/setup.md` files generated for every Shielva connector.

The setup instructions file is shown to users in the Deployment Form Builder
panel so they know exactly where to find each credential in the provider's
portal. Every connector MUST have one.

---

## Mandatory Document Structure

```markdown
# {ConnectorName} Setup Guide

## Overview
One paragraph (3-5 sentences) describing:
- What this connector does
- What service/API it integrates with
- What level of access/account is required (e.g. "requires a Business or Developer account")

## Prerequisites
Bullet list of requirements before starting:
- Account type needed (Business/Developer/Enterprise)
- Any subscriptions or tier requirements
- Any existing setup the user must complete first

## Step-by-Step Configuration

### 1. {Field Label} (`{field_key}`)
**What it is:** One sentence explaining what this credential is and its purpose.

**Where to find it:**
1. Go to [Portal Name](https://exact-url.example.com)
2. Navigate to: Settings > Developer > API Keys (use exact menu path)
3. Click "[Exact Button Name]"
4. [Describe what appears on screen]
5. Copy the value

**Important:** Any critical warnings (e.g., "this is only shown once", "never share this")

**Tip:** Optional helpful hint

### 2. {Next Field Label} (`{next_field_key}`)
[Same structure as above]

[... one section per install_field ...]

## Testing the Connection
After entering all credentials:
1. Click the **Check Connection** button
2. A green ✓ confirms the connection is working
3. If you see an error, check [common issue]

## Troubleshooting

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| "Invalid credentials" | Wrong API key | Double-check you copied the full key |
| "403 Forbidden" | Insufficient permissions | Ensure your account has API access enabled |
| "Timeout" | Network issue | Check your firewall/proxy settings |

## Support
- Provider documentation: [Official Docs URL]
- Support contact: [Support URL or email if known]
```

---

## Content Requirements

### MUST include
- Exact portal URL for each credential field
- Step-by-step numbered navigation (e.g. "Settings → Developer → API Keys")
- The exact button/link name the user needs to click
- What the user will see on screen after navigating there
- Any "copy" instructions (some keys are only shown once!)

### MUST NOT include
- Generic/vague instructions like "go to settings"
- Instructions that don't match this specific provider's actual portal
- Made-up URLs — if unsure, use descriptive navigation path instead

### Tone & style
- Write for a non-developer business admin
- Use plain English, avoid jargon
- Use **bold** for button names and UI elements
- Use `code formatting` for field keys and values the user must type exactly

---

## Connector-Specific Research Guidelines

When generating instructions for a specific connector, Gemini MUST:

1. **Read `connector.py`** — identify every `self.config.get(key)` call to know
   which credential fields need documentation

2. **Read `metadata/connector.json`** — use `install_fields` array for field
   labels, help text, and field types (password fields = sensitive = warn user)

3. **Identify the provider** from `CONNECTOR_TYPE` or the connector class name
   and research that provider's actual portal:
   - Paytm → business.paytm.com → Dashboard → Developer Settings
   - Stripe → dashboard.stripe.com → Developers → API keys
   - Razorpay → dashboard.razorpay.com → Settings → API Keys
   - Twilio → console.twilio.com → Account → API Keys
   - SendGrid → app.sendgrid.com → Settings → API Keys
   - AWS → aws.amazon.com → IAM → Users → Security credentials
   - Google → console.cloud.google.com → APIs & Services → Credentials

4. **Include the actual portal URL** for each step — do not use placeholder URLs

5. **Write a dedicated section for each install_field** — even if the field
   seems obvious (like "email"), still document where to confirm/find it

---

## Version
v1.0 — Initial structure guidelines for Shielva connector setup instructions
"""


# ── Service functions ──────────────────────────────────────────────────

async def get_instruction_guidelines() -> str:
    """Fetch the INSTRUCTION_SETUP_GUIDELINES from R2/local cache.

    Falls back to the hardcoded default if not found in R2.
    """
    content = await r2_service.get_step_prompt(
        _GUIDELINES_KEY,
        DEFAULT_INSTRUCTION_SETUP_GUIDELINES,
    )
    return content


async def seed_instruction_guidelines() -> None:
    """Upload INSTRUCTION_SETUP_GUIDELINES to R2/local if not already present
    or if the local constant has changed (same pattern as sync_all_step_prompts_to_r2).

    Called on service startup from main.py lifespan.
    """
    key = r2_service._step_prompt_key(_GUIDELINES_KEY)
    try:
        existing: Optional[str] = None
        if r2_service._use_local():
            lp = r2_service._local_path(key)
            existing = lp.read_text(encoding="utf-8") if lp.exists() else None
        else:
            try:
                obj = r2_service._get_client().get_object(
                    Bucket=r2_service._get_shared_bucket(),
                    Key=key,
                )
                existing = obj["Body"].read().decode("utf-8")
            except Exception:
                existing = None

        if existing is None or existing.strip() != DEFAULT_INSTRUCTION_SETUP_GUIDELINES.strip():
            await r2_service.save_step_prompt(_GUIDELINES_KEY, DEFAULT_INSTRUCTION_SETUP_GUIDELINES)
            logger.info("instructions_guidelines.seeded", key=_GUIDELINES_KEY)
        else:
            logger.debug("instructions_guidelines.unchanged", key=_GUIDELINES_KEY)
    except Exception as exc:
        logger.warning("instructions_guidelines.seed_failed", error=str(exc))
