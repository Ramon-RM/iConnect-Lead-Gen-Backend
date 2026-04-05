"""
iConnect — AI Service
Auto-detects which provider to use based on available API keys:

  Priority:
    1. ANTHROPIC_API_KEY present  → Claude (always, even in dev)
    2. GROQ_API_KEY present       → Groq  (free, great for testing)
    3. Neither                    → raises clear error at startup

  On Render (production):
    - Set ANTHROPIC_API_KEY in environment variables → Claude is used
    - Don't set it → falls back to Groq if GROQ_API_KEY is present

  Locally:
    - Only set GROQ_API_KEY → free Groq for all development
    - Set ANTHROPIC_API_KEY → Claude used immediately

  You never need to change AI_PROVIDER manually.
"""
import json
import logging
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.schemas.schemas import ClassificationResult

logger = logging.getLogger(__name__)


def _detect_provider() -> str:
    """
    Auto-detect which AI provider to use based on available keys.
    Claude wins if its key is present. Groq is the fallback.
    """
    if settings.anthropic_api_key and settings.anthropic_api_key.startswith("sk-ant-"):
        return "claude"
    if settings.groq_api_key:
        return "groq"
    raise RuntimeError(
        "No AI provider configured. "
        "Set ANTHROPIC_API_KEY (for Claude) or GROQ_API_KEY (for Groq) in your .env"
    )


# Resolved once at import time — logged clearly on startup
_ACTIVE_PROVIDER = _detect_provider()
logger.info(f"AI provider: {_ACTIVE_PROVIDER}")

# ─── Prompts ──────────────────────────────────────────────────────────────────

CLASSIFIER_SYSTEM = """You are a lead qualification AI. Analyze inbound emails and classify them.
Respond ONLY in valid JSON — no explanation, no markdown, no code fences.

JSON schema (use exactly these keys):
{
  "classification": "hot" | "warm" | "cold" | "spam" | "existing_client",
  "intent": "<one sentence — what do they actually want>",
  "urgency": "high" | "medium" | "low",
  "budget_signal": "<any price, budget, or timeline clues, or 'none'>",
  "reply_priority": <integer 1-5>
}

Definitions:
- hot = ready to buy, asking price, requesting a call
- warm = genuinely interested, asking a specific question, researching
- cold = vague, no clear intent, just browsing
- spam = automated, irrelevant, or solicitation
- existing_client = mentions previous work together"""

REPLY_SYSTEM = """You are writing a sales email reply on behalf of {client_name}, a {niche}.

Tone: {tone_notes}
Proof point to use when relevant: {proof_point}

Write ONE reply that:
1. Opens by acknowledging their specific situation using their own words
2. Drops one specific result or proof point if relevant to their intent
3. Ends with exactly ONE clear next step (book a call / see a case study / answer the question)

Hard rules:
- Maximum 5 sentences
- No bullet points
- Never start with "I hope this finds you well" or any generic opener
- Sound like a real person, not a template
- Do not mention AI, automation, or software
- Do not repeat yourself
- Reply only with the email body — no subject line, no sign-off"""

FOLLOWUP_SYSTEM = """You are writing a follow-up email on behalf of {client_name}, a {niche}.

Tone per follow-up number:
1 = Helpful nudge, add one new piece of value
2 = Reference a specific result or social proof
3 = Ask one direct re-engagement question (short, honest)
4 = Graceful goodbye — final value hook, no guilt, door left open

Hard rules:
- Maximum 3 sentences
- Never repeat what was already said (previous messages provided below)
- If follow-up #4, make it feel final but warm
- Reply only with the email body — no subject line, no sign-off"""


# ─── Provider Clients ─────────────────────────────────────────────────────────

def _get_claude_client():
    import anthropic
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _get_groq_client():
    from groq import Groq
    return Groq(api_key=settings.groq_api_key)


# ─── Core LLM call ────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _call_llm(system: str, user_message: str, max_tokens: int = 600) -> tuple[str, str, int, int]:
    """
    Returns: (text_response, provider_name, prompt_tokens, completion_tokens)
    Uses _ACTIVE_PROVIDER, which is auto-detected at startup from available API keys.
    """
    provider = _ACTIVE_PROVIDER

    if provider == "claude":
        client = _get_claude_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        text = response.content[0].text
        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens
        return text, "claude", prompt_tokens, completion_tokens

    else:  # groq
        client = _get_groq_client()
        response = client.chat.completions.create(
            model=settings.groq_model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message}
            ]
        )
        text = response.choices[0].message.content
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens
        return text, "groq", prompt_tokens, completion_tokens


# ─── Public Functions ─────────────────────────────────────────────────────────

async def classify_email(
    sender_email: str,
    subject: str,
    body: str,
    client_name: str,
    niche: str,
) -> tuple[ClassificationResult, str, int, int]:
    """
    Classify an inbound email.
    Returns: (ClassificationResult, provider, prompt_tokens, completion_tokens)
    """
    user_message = f"""Client: {client_name} ({niche})

Email to classify:
From: {sender_email}
Subject: {subject}
Body:
{body}"""

    raw, provider, pt, ct = await _call_llm(CLASSIFIER_SYSTEM, user_message, max_tokens=300)

    # Strip markdown fences if model adds them
    clean = raw.strip().strip("```json").strip("```").strip()

    try:
        data = json.loads(clean)
        result = ClassificationResult(**data)
    except Exception as e:
        logger.error(f"Classification parse error: {e} | raw: {raw}")
        # Safe fallback — treat as warm, human reviews
        result = ClassificationResult(
            classification="warm",
            intent="Could not parse — manual review needed",
            urgency="medium",
            budget_signal="none",
            reply_priority=3,
        )

    return result, provider, pt, ct


async def generate_reply(
    sender_email: str,
    email_body: str,
    thread_history: str,
    classification_result: ClassificationResult,
    client_name: str,
    niche: str,
    tone_notes: str,
    proof_point: str,
) -> tuple[str, str, int, int]:
    """
    Generate a personalized reply to a lead email.
    Returns: (reply_body, provider, prompt_tokens, completion_tokens)
    """
    system = REPLY_SYSTEM.format(
        client_name=client_name,
        niche=niche,
        tone_notes=tone_notes or "direct, warm, professional",
        proof_point=proof_point or "strong results for similar businesses",
    )

    user_message = f"""Lead classification: {classification_result.classification}
What they want: {classification_result.intent}
Urgency: {classification_result.urgency}
Budget signal: {classification_result.budget_signal}

Their email:
From: {sender_email}
Body:
{email_body}

Previous thread (if any):
{thread_history or "None — this is the first message"}"""

    reply, provider, pt, ct = await _call_llm(system, user_message, max_tokens=400)
    return reply.strip(), provider, pt, ct


async def generate_followup(
    lead_name: str,
    company: str,
    original_intent: str,
    followup_number: int,
    days_since_contact: int,
    message_history: str,
    client_name: str,
    niche: str,
    tone_notes: str,
) -> tuple[str, str, int, int]:
    """
    Generate follow-up #1–4.
    Returns: (followup_body, provider, prompt_tokens, completion_tokens)
    """
    system = FOLLOWUP_SYSTEM.format(
        client_name=client_name,
        niche=niche,
    )

    user_message = f"""Follow-up number: {followup_number} of 4
Lead: {lead_name or "there"} at {company or "their company"}
Original interest: {original_intent}
Days since last contact: {days_since_contact}
Tone: {tone_notes or "direct, warm, professional"}

Previous messages sent to this lead:
{message_history or "No previous messages recorded"}"""

    followup, provider, pt, ct = await _call_llm(system, user_message, max_tokens=250)
    return followup.strip(), provider, pt, ct