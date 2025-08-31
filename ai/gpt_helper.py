# ai/gpt_helper.py
import os
from typing import Optional

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def summarize_news(symbol: str, news_text: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = f"""You are a trading assistant. Summarize in Korean:
- Symbol: {symbol}
- News: {news_text}
- Extract: catalysts, risks, and likely price impact (bullish/bearish/neutral). Keep it under 5 bullet points."""
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return None
