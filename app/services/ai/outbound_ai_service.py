"""
iConnect — Outbound AI Service
Three prompts specifically for cold outreach.
These are different from inbound prompts:
  - Initial cold email: short, human, ONE ask
  - Follow-up: reference previous, add value, re-engage
  - Subject line generator: curiosity-driven, not spammy
"""
import logging
from app.services.ai.ai_service import _call_llm

logger = logging.getLogger(__name__)

# ─── Cold Email Prompts ───────────────────────────────────────────────────────

COLD_EMAIL_SYSTEM = """You are writing a cold outreach email on behalf of {sender_name} at {company_name}.

About them: {value_proposition}
Target pain point: {pain_point_focus}
Social proof: {social_proof}
Call to action: {cta}

Rules for cold email (strict):
- Maximum 5 sentences total. Short wins.
- First sentence: reference something SPECIFIC to THEM (their company, recent post, or role)
- Second sentence: name the exact pain point they likely have
- Third sentence: one concrete result (not a vague claim)
- Fourth sentence: the CTA — one soft ask, no pressure
- No subject line in the body
- No "I hope this email finds you well"
- No "My name is..." (they can see the From field)
- No bullet points
- No mention of AI, automation software, or tools
- Sound like a human who did their homework, not a template blast"""

COLD_FOLLOWUP_SYSTEM = """You are writing cold follow-up #{step} on behalf of {sender_name} at {company_name}.

Context: {value_proposition}
Pain point: {pain_point_focus}
CTA: {cta}

Follow-up rules by step:
Step 2: Reference the first email briefly. Add one new piece of value or data point.
Step 3: Ask a single direct question about their current situation. Make it easy to say yes or no.
Step 4: Final email. Make it short, warm, leave door open. "Saving you from more emails."

Rules:
- Max 3 sentences
- Do NOT say "just following up" or "checking in"
- Each follow-up must feel like a new reason to respond, not a nudge
- No sign-off or subject line — body text only"""

SUBJECT_LINE_SYSTEM = """Generate ONE cold email subject line for {first_name} at {company}.

Context: {pain_point_focus}

Rules:
- Max 6 words
- No clickbait, no all-caps, no exclamation marks
- No "Quick question" or "Following up"
- Sound like it came from a real person who knows them
- Curiosity-driven but honest — they should actually want to open it
- Return ONLY the subject line text, nothing else"""


# ─── Public Functions ─────────────────────────────────────────────────────────

async def generate_cold_email(
    sender_name: str,
    company_name: str,
    value_proposition: str,
    pain_point_focus: str,
    social_proof: str,
    cta: str,
    prospect_first_name: str,
    prospect_company: str,
    prospect_title: str,
    personalisation_note: str = "",
) -> tuple[str, str, int, int]:
    """
    Generate a personalised cold email body.
    Returns: (email_body, provider, prompt_tokens, completion_tokens)
    """
    system = COLD_EMAIL_SYSTEM.format(
        sender_name=sender_name,
        company_name=company_name,
        value_proposition=value_proposition,
        pain_point_focus=pain_point_focus,
        social_proof=social_proof or "strong results for similar businesses",
        cta=cta or "open to a 15-minute chat?",
    )

    user_message = f"""Write the cold email for:
Name: {prospect_first_name}
Company: {prospect_company}
Title: {prospect_title}
Personalisation hook: {personalisation_note or f"They run {prospect_company}, a business in our target market"}"""

    body, provider, pt, ct = await _call_llm(system, user_message, max_tokens=300)
    return body.strip(), provider, pt, ct


async def generate_cold_followup(
    sender_name: str,
    company_name: str,
    value_proposition: str,
    pain_point_focus: str,
    cta: str,
    step_number: int,
    prospect_first_name: str,
    previous_emails_summary: str,
) -> tuple[str, str, int, int]:
    """
    Generate follow-up #2, #3, or #4 for a non-responding prospect.
    Returns: (followup_body, provider, prompt_tokens, completion_tokens)
    """
    system = COLD_FOLLOWUP_SYSTEM.format(
        step=step_number,
        sender_name=sender_name,
        company_name=company_name,
        value_proposition=value_proposition,
        pain_point_focus=pain_point_focus,
        cta=cta or "worth a quick chat?",
    )

    user_message = f"""Prospect: {prospect_first_name}
Previous emails sent to them:
{previous_emails_summary}"""

    body, provider, pt, ct = await _call_llm(system, user_message, max_tokens=200)
    return body.strip(), provider, pt, ct


async def generate_subject_line(
    first_name: str,
    company: str,
    pain_point_focus: str,
) -> str:
    """Generate a curiosity-driven subject line. Returns just the string."""
    system = SUBJECT_LINE_SYSTEM.format(
        first_name=first_name,
        company=company,
        pain_point_focus=pain_point_focus,
    )
    subject, _, _, _ = await _call_llm(system, "Generate the subject line.", max_tokens=30)
    # Strip quotes if model adds them
    return subject.strip().strip('"').strip("'")