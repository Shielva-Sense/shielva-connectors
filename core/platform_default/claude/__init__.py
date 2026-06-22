"""platform_default.claude — Canonical Claude LLM skill for Shielva services.

Import the skill:
    from platform_default.claude.skill import ClaudeSkill

Quick-start (integration service):
    skill = ClaudeSkill.from_integration_settings()
    response, history = await skill.chat(
        user_message="Build a connector plan for Slack.",
        system=system_prompt,
        prior_history=session.get("conversation_history", []),
        parse_json=True,
    )
"""

from platform_default.claude.skill import ClaudeSkill

__all__ = ["ClaudeSkill"]
