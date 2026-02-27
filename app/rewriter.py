from openai import OpenAI

from app.config import NEBIUS_BASE_URL, NEBIUS_API_KEY, LLM_MODEL

TONE_INSTRUCTIONS = {
    "formal": "Rewrite in a professional, formal tone. Keep the same meaning.",
    "friendly": "Rewrite in a warm, friendly, casual tone. Keep the same meaning.",
    "shorter": "Make this much shorter and more concise. Keep the core meaning.",
    "funnier": "Rewrite to be witty and humorous. Keep the core meaning.",
}

SYSTEM = (
    "You are a message rewriting assistant. Rewrite the user's message "
    "according to the instruction. Return ONLY the rewritten message text, "
    "with no preamble, explanation, or quotes."
)


def rewrite(text: str, tone: str, language: str | None = None) -> str:
    """Rewrite text with a given tone or translate it."""
    client = OpenAI(base_url=NEBIUS_BASE_URL, api_key=NEBIUS_API_KEY)

    if tone == "translate" and language:
        instruction = f"Translate this message to {language}. Return only the translation."
    elif tone in TONE_INSTRUCTIONS:
        instruction = TONE_INSTRUCTIONS[tone]
    else:
        instruction = f"Rewrite this message to sound more {tone}."

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{instruction}\n\nMessage: {text}"},
        ],
        max_tokens=500,
    )
    return response.choices[0].message.content.strip()
