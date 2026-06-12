import logging
import os
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class ClaudeTranslator:
    """Groq (Llama) を使った英→日翻訳"""

    def __init__(self, config: dict):
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY") or config.get("groq_api_key", ""))
        self.model = config.get("groq_model", "llama-3.3-70b-versatile")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "以下の英語テキストをすべて自然な日本語に翻訳してください。\n"
                        "番号付きリスト形式で、元の番号を保持して返答してください。\n"
                        "翻訳以外の説明文は一切不要です。\n\n"
                        f"{numbered}"
                    ),
                }
            ],
        )
        raw = response.choices[0].message.content.strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        results = []
        for line in lines:
            if line and line[0].isdigit() and ". " in line:
                results.append(line.split(". ", 1)[1])
        if len(results) != len(texts):
            logger.warning(f"Translation count mismatch: expected {len(texts)}, got {len(results)}. Using originals.")
            return texts
        return results
