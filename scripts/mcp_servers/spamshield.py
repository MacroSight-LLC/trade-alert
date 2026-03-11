"""SpamShield Pro MCP — text classification for spam/bot filtering.

Tools: classify_text
Uses simple heuristic classifier. No external API required.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_NAME = "SpamShieldpro MCP"

# Spam indicator patterns
_SPAM_PATTERNS: list[re.Pattern] = [
    re.compile(r"(buy now|act fast|limited time|guaranteed profit)", re.IGNORECASE),
    re.compile(r"(click here|join.*telegram|join.*discord.*link)", re.IGNORECASE),
    re.compile(r"(100x|1000x|moonshot guaranteed|free money)", re.IGNORECASE),
    re.compile(r"(www\.\S+\.com/\S+|bit\.ly/|t\.me/)", re.IGNORECASE),
    re.compile(r"(airdrop.*claim|claim.*airdrop|free.*token)", re.IGNORECASE),
    re.compile(r"(\$\d+k?\s*(per|a)\s*(day|week|month))", re.IGNORECASE),
    re.compile(r"(dm me|send me|wire me|venmo|cashapp)", re.IGNORECASE),
]

# Bot-like patterns
_BOT_PATTERNS: list[re.Pattern] = [
    re.compile(r"(.)\1{10,}"),  # Repeated characters
    re.compile(r"(🚀|💰|💎|🔥){5,}"),  # Excessive emojis
    re.compile(r"^[A-Z\s!]{50,}$"),  # All caps screaming
]


async def classify_text(params: dict[str, Any]) -> dict:
    """Classify text as spam or not spam.

    Params:
        text: str — text to classify.

    Returns:
        {"is_spam": bool, "confidence": float, "label": str}
    """
    text: str = params.get("text", "")
    if not text or not text.strip():
        return {"is_spam": False, "confidence": 1.0, "label": "not_spam"}

    spam_score = 0.0
    total_patterns = len(_SPAM_PATTERNS) + len(_BOT_PATTERNS)

    for pattern in _SPAM_PATTERNS:
        if pattern.search(text):
            spam_score += 1.0

    for pattern in _BOT_PATTERNS:
        if pattern.search(text):
            spam_score += 0.5

    # Short text with lots of links is suspicious
    link_count = len(re.findall(r"https?://", text))
    word_count = len(text.split())
    if word_count > 0 and link_count / word_count > 0.3:
        spam_score += 1.0

    confidence = min(spam_score / (total_patterns * 0.4), 1.0)
    is_spam = confidence >= 0.5

    return {
        "is_spam": is_spam,
        "confidence": round(max(confidence, 1.0 - confidence), 4),
        "label": "spam" if is_spam else "not_spam",
    }


TOOLS: dict[str, Any] = {
    "classify_text": classify_text,
}
