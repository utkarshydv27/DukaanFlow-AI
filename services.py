"""Intent extraction for Hindi, Hinglish, and English tiffin-service messages.

A local Ollama model is used when enabled. Conservative deterministic rules remain
available as a fallback and always keep unclear/permanent changes out of auto-confirm.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

import requests

VALID_INTENTS = {"skip_today", "cancel_subscription", "other", "unknown"}
VALID_DATES = {"today", "tomorrow", "unknown"}

# Roman Hindi varies greatly between people. Normalize common spellings before
# applying rules; the original message is still retained by app.py in the database.
_REPLACEMENTS = (
    (r"\b(aaj|aj|aajh)\b", "aaj"),
    (r"\b(nahi|nahin|nhi|nai|nehi|nahee|ni)\b", "nahi"),
    (r"\b(mt|mtt)\b", "mat"),
    (r"\b(tifin|tifen|tiffen|tiffan)\b", "tiffin"),
    (r"\b(khaana|khana)\b", "khana"),
    (r"\b(dabbe|dabbay|dabba|dabbaa)\b", "dabba"),
    (r"\b(krdo|kardo|krdo|karna hai|krna hai)\b", "kar do"),
    (r"\b(please|plz|pls|plss)\b", "please"),
    (r"\b(bhej dena|bhejdena|bhejdo|bhej do|bhej dijiyega|bhejiyega|bhejiye|bhejna|bhejo|bheje)\b", "bhejna"),
    (r"\b(laana|lana|leke aana|le kar aana|la do|le aana)\b", "lana"),
)

SERVICE_RE = re.compile(
    r"\b(tiffin|dabba|dabbon|lunch|khana|meal|food|roti|thali|nashta|nasta|dinner|breakfast|paratha|parathe|sabzi)\b"
    r"|टिफिन|डब्बा|डब्बे|खाना|भोजन|लंच|रोटी|थाली|नाश्ता|खाने",
    re.IGNORECASE,
)
TODAY_RE = re.compile(
    r"\b(aaj|today|tonight|for today|only today|sirf aaj|aaj ke liye|aaj hi|aaj ka|aaj ki|aaj ke)\b"
    r"|आज|आजकेलिए|आज ही",
    re.IGNORECASE,
)
TOMORROW_RE = re.compile(r"\b(kal|tomorrow|next day|from tomorrow)\b|कल|आने वाले कल", re.IGNORECASE)
NEGATIVE_RE = re.compile(
    r"\b(mat|nahi|not|no|dont|don't|do not|never|skip|cancel|cancelled|canceled|band|rok|ruko|stop|hold|off|rehne do|chhutti|chhuti)\b"
    r"|मत|नहीं|नही|ना भेज|न भेज|रद्द|बंद|रोक|छुट्टी|मत भेज|मत लाना|नहीं चाहिए",
    re.IGNORECASE,
)
DELIVERY_ACTION_RE = re.compile(
    r"\b(bhej\w*|bhejna|lana|dena|deliver\w*|send\w*|bring\w*|provide\w*|chahiye|chahie|chahye|needed|need|aana|lao|bhej)\b"
    r"|भेज\w*|लाना|देना|चाहिए|आना|पहुंचाना|पहुँचाना",
    re.IGNORECASE,
)
EXPLICIT_SKIP_RE = re.compile(
    r"\b(skip|cancel|cancelled|canceled|band|rok|ruko|stop|hold|off|rehne do|chhutti|chhuti)\b"
    r"|स्किप|रद्द|बंद|रोक|छुट्टी|रहने दो",
    re.IGNORECASE,
)
PERMANENT_RE = re.compile(
    r"\b(subscription|subscribe|plan|permanent\w*|permanently|ab se|aage se|hamesha|roz|daily|every day|har din|regularly|forever|always|band kar do|band kar dena|stop my tiffin|cancel my plan)\b"
    r"|सब्सक्रिप्शन|सदस्यता|अब से|आगे से|हमेशा|रोज|हर दिन|स्थायी रूप से|हमेशा के लिए",
    re.IGNORECASE,
)
EXPLICIT_PERMANENT_RE = re.compile(
    r"\b(subscription|subscribe|plan|permanent\w*|permanently|ab se|aage se|hamesha|roz|daily|every day|har din|regularly|forever|always|stop my tiffin|cancel my plan)\b"
    r"|सब्सक्रिप्शन|सदस्यता|अब से|आगे से|हमेशा|रोज|हर दिन|स्थायी रूप से|हमेशा के लिए",
    re.IGNORECASE,
)
INGREDIENT_RE = re.compile(
    r"\b(pyaaz|pyaj|onion|mirchi|chilli|chili|namak|salt|cheeni|sugar|tel|oil|masala|spicy|less spicy|extra spicy|without onion|no onion|kam namak|less salt|no salt)\b"
    r"|प्याज|प्याज़|मिर्च|नमक|चीनी|तेल|मसाला",
    re.IGNORECASE,
)


def _normalise(message: str) -> str:
    text = unicodedata.normalize("NFKC", message or "").casefold()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[\t\r\n]+", " ", text)
    for pattern, replacement in _REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _rule_classify(message: str) -> dict:
    """Understand common Roman-Hindi/Hinglish and Devanagari skip phrases.

    Rules deliberately distinguish delivery skipping from food-preference changes.
    Less clear messages are returned at confidence < 0.75, so app.py puts them in
    the seller-review queue rather than allowing one-click confirmation.
    """
    text = _normalise(message)
    has_service = bool(SERVICE_RE.search(text))
    has_today = bool(TODAY_RE.search(text))
    has_tomorrow = bool(TOMORROW_RE.search(text))
    has_negative = bool(NEGATIVE_RE.search(text))
    has_action = bool(DELIVERY_ACTION_RE.search(text))
    explicit_skip = bool(EXPLICIT_SKIP_RE.search(text))
    has_permanent_cue = bool(EXPLICIT_PERMANENT_RE.search(text))

    if not has_service:
        return {"intent": "unknown", "date": "unknown", "confidence": 0.30,
                "reason": "No clear tiffin or food-delivery item was identified; seller review is required."}

    # Don't confuse a food customization with a delivery cancellation, e.g.
    # "tiffin mein pyaaz mat daalna". An explicit delivery action still wins.
    ingredient_change = bool(INGREDIENT_RE.search(text)) and not has_action and not explicit_skip
    if ingredient_change:
        return {"intent": "other", "date": "unknown", "confidence": 0.88,
                "reason": "This looks like a food preference or ingredient instruction, not a delivery skip."}

    # A specific permanent cue is never auto-treated as a one-day skip, even if
    # the same message also mentions today. It stays in the manual-review flow.
    if has_permanent_cue and (has_negative or explicit_skip):
        return {"intent": "cancel_subscription", "date": "unknown", "confidence": 0.94,
                "reason": "Message may request a recurring service or subscription change; manual review is required."}

    # Phrases that normally mean "don't bring/send/give today's food".
    clear_delivery_skip = (
        explicit_skip
        or (has_negative and has_action)
        or (has_negative and re.search(r"\b(tiffin|dabba|lunch|khana|meal|food)\b.*\b(chahiye|needed|need)\b", text))
        or bool(re.search(r"\b(aaj|today)\b.*\b(ka|ki|ke)?\s*(tiffin|dabba|lunch|khana)\b.*\b(rehne do|mat|nahi)\b", text))
    )

    # A message like "tiffin nahi" may be a skip, but it is underspecified.
    if has_negative and not clear_delivery_skip:
        unclear_negative = True
    else:
        unclear_negative = False

    if has_service and clear_delivery_skip:
        if has_today:
            return {"intent": "skip_today", "date": "today", "confidence": 0.90,
                    "reason": "Hinglish/Hindi message appears to request skipping today's tiffin delivery; seller confirmation is still required."}
        if has_tomorrow:
            return {"intent": "skip_today", "date": "tomorrow", "confidence": 0.74,
                    "reason": "Message appears to request skipping a future delivery; date review is required."}
        return {"intent": "skip_today", "date": "unknown", "confidence": 0.62,
                "reason": "A tiffin skip appears likely, but the requested date is not explicit; manual review is required."}

    if has_service and unclear_negative:
        if has_today:
            return {"intent": "skip_today", "date": "today", "confidence": 0.68,
                    "reason": "Message may mean today's tiffin is not wanted, but the action is unclear; manual review is required."}
        return {"intent": "skip_today", "date": "unknown", "confidence": 0.55,
                "reason": "Message may relate to a tiffin skip, but intent/date are unclear; manual review is required."}

    # A request to take a day off from tiffin is common in conversational Hinglish.
    if has_service and has_today and re.search(r"\b(chhutti|chhuti|off|leave|break)\b|छुट्टी", text):
        return {"intent": "skip_today", "date": "today", "confidence": 0.86,
                "reason": "Message appears to request a one-day tiffin break; seller confirmation is required."}

    return {"intent": "unknown", "date": "unknown", "confidence": 0.35,
            "reason": "The message could not be safely classified as a one-day tiffin skip."}


def classify_message(message: str) -> dict:
    """Use local Ollama for broader Hinglish interpretation when enabled.

    The model is not trusted to overrule a clear subscription-cancellation cue.
    If it is offline, rules provide a local fallback. One-click confirmation is
    still gated by explicit same-day intent and the seller's action in app.py.
    """
    fallback = _rule_classify(message)
    if os.getenv("OLLAMA_ENABLED", "false").lower() != "true":
        return fallback

    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
    schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["skip_today", "cancel_subscription", "other", "unknown"]},
            "date": {"type": "string", "enum": ["today", "tomorrow", "unknown"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
        "required": ["intent", "date", "confidence", "reason"],
    }
    prompt = """You classify food/tiffin delivery customer messages. The message may be
Hindi in Devanagari, conversational Hinglish/Roman Hindi, spelling variations,
shortened chat typing, or English. Interpret meaning, not exact spelling. Return
only JSON matching the schema.

Intents:
- skip_today: customer clearly asks to skip/cancel only one delivery, not their plan.
- cancel_subscription: stop/pause recurring service, e.g. 'ab se roz tiffin mat bhejna'.
- other: food preference or unrelated message, e.g. 'tiffin mein pyaaz mat daalna'.
- unknown: not enough information.

Examples:
'bhaiya aaj tiffin mt bhejna' => skip_today, today, 0.95
'aj ka dabba ni chahiye' => skip_today, today, 0.90
'aaj mera tiffin mat bhejna kal se normal bhejna' => skip_today, today, 0.96
'aaj ka lunch cancel krdo plz' => skip_today, today, 0.93
'aaj main bahar khaunga, dabba mat bhejna' => skip_today, today, 0.94
'kal mere tiffin mat bhejna' => skip_today, tomorrow, 0.85
'ab se roz tiffin mat bhejna' => cancel_subscription, unknown, 0.96
'meri tiffin subscription band kar do' => cancel_subscription, unknown, 0.96
'tiffin mein pyaaz mat daalna' => other, unknown, 0.92
'आज मेरा टिफिन मत भेजना' => skip_today, today, 0.96
'Good morning bhaiya' => unknown, unknown, 0.90

Rules: Do not treat an ingredient/recipe change as a delivery skip. Do not infer a
customer identity. If a date is missing, return unknown for date. Explicit 'aaj'
or 'today' means today. 'kal' in a future delivery request usually means tomorrow;
if ambiguous, choose unknown. 'kal se normal bhejna' does not cancel the subscription
when the same message explicitly says to skip only today. A permanent stop request
must never be converted to skip_today. Only use confidence >= 0.75 when intent and
date are clear. Seller approval remains required.

Customer message: """ + json.dumps(message[:2000], ensure_ascii=False)
    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "format": schema, "stream": False,
                  "options": {"temperature": 0}},
            timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "12")),
        )
        response.raise_for_status()
        data = json.loads(response.json()["message"]["content"])
        intent = data.get("intent") if data.get("intent") in VALID_INTENTS else "unknown"
        date = data.get("date") if data.get("date") in VALID_DATES else "unknown"
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0))))
        reason = str(data.get("reason", "Local model classified the message."))[:400]

        # Strong local-rule warnings about permanent changes and ingredient edits win.
        if fallback["intent"] in {"cancel_subscription", "other"}:
            return fallback
        # The model may interpret unusual Hinglish wording that the rules do not
        # know. However, it cannot invent a date: a same-day auto-confirm candidate
        # must contain an explicit today marker in the original message.
        explicit_today = bool(TODAY_RE.search(_normalise(message)))
        if intent == "skip_today" and confidence >= 0.75 and date == "today" and explicit_today:
            return {"intent": intent, "date": date, "confidence": confidence, "reason": reason}
        if intent == "skip_today" and date == "today" and not explicit_today:
            return {"intent": intent, "date": "unknown", "confidence": min(confidence, 0.74),
                    "reason": "The message may request a skip, but the requested date is not explicit; manual review is required."}
        if intent == "skip_today" and date == "tomorrow":
            return {"intent": intent, "date": date, "confidence": min(confidence, 0.74), "reason": reason}
        if intent == "cancel_subscription":
            return {"intent": intent, "date": "unknown", "confidence": confidence, "reason": reason}
        if intent == "other":
            return {"intent": intent, "date": "unknown", "confidence": confidence, "reason": reason}
        # When model confidence is low or date is unclear, use the safer rule result.
        if fallback["intent"] != "unknown":
            return fallback
        return {"intent": "unknown", "date": date if date in VALID_DATES else "unknown",
                "confidence": min(confidence, 0.74), "reason": reason or fallback["reason"]}
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        return fallback
