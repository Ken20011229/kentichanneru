import json
import logging
import os
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

SCRIPT_PROMPT = """\
あなたはYouTubeニュース・科学動画のナレーション台本作家です。
以下の情報を元に、視聴者が聞きやすい日本語ナレーション台本と動画メタデータを作成してください。

## 入力情報
タイトル: {title}
概要: {summary}
ソース: {source}

## 出力要件
以下のJSON形式のみで返答してください（マークダウンコードブロックは不要）:
{{
  "title": "動画タイトル（30文字以内、インパクトのある表現、視聴者の興味を引く内容）",
  "description": "YouTube説明欄テキスト（300〜500文字。内容の要約、関連情報、ハッシュタグ5個を含める）",
  "tags": ["タグ1", "タグ2", "タグ3"],
  "script_segments": [
    {{"text": "ナレーション1文目（例：今回ご紹介するのは、AIの世界に大きな変化をもたらす注目のニュースです。）"}},
    {{"text": "ナレーション2文目（例：近年、人工知能の分野では目覚ましい進化が続いており、私たちの生活にも影響を与え始めています。）"}},
    {{"text": "ナレーション3文目（例：今回の発表は、その中でも特に重要な転換点となる可能性があります。）"}},
    {{"text": "... 以降も同様に続ける ..."}}
  ],
  "thumbnail_title": "サムネイル用短縮タイトル（15文字以内、強いインパクト）",
  "image_search_keywords": ["英語キーワード1", "英語キーワード2", "英語キーワード3"]
}}

## 制約（厳守）
- script_segments は必ず16〜20セグメント生成すること。15未満は絶対に不可
- 各セグメントは1文のナレーション（句点「。」で終わること）
- ナレーション構成：導入（2〜3文）→ 詳細説明（6〜8文）→ 背景・意義（3〜4文）→ まとめ・展望（2〜3文）
- タグは10〜15個
- image_search_keywords は画像検索に使う英語キーワード（具体的で視覚的なもの）
- ナレーションは丁寧でわかりやすい口語体
- 専門用語は噛み砕いて説明する
"""


class ClaudeScriptWriter:
    """Groq (Llama) を使った台本・メタデータ生成"""

    def __init__(self, config: dict):
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY") or config.get("groq_api_key", ""))
        self.model = config.get("groq_model", "llama-3.3-70b-versatile")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
    def generate(self, item: dict) -> dict:
        prompt = SCRIPT_PROMPT.format(
            title=item.get("title", ""),
            summary=item.get("summary", ""),
            source=item.get("source", ""),
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Script JSON parse failed: {e}\nRaw:\n{raw[:500]}")
            raise

        self._validate(data)
        return data

    def _validate(self, data: dict):
        required = ["title", "description", "tags", "script_segments", "thumbnail_title", "image_search_keywords"]
        for key in required:
            if key not in data:
                raise ValueError(f"Script response missing required key: '{key}'")
        segments = data["script_segments"]
        if not segments:
            raise ValueError("script_segments is empty")
        if len(segments) < 15:
            raise ValueError(f"Too few script_segments: {len(segments)} (minimum 15 required for sufficient video length)")
        for seg in segments:
            if "text" not in seg:
                raise ValueError("script_segment missing 'text' field")
        total_chars = sum(len(seg["text"]) for seg in segments)
        logger.info(f"Script total chars: {total_chars} ({len(segments)} segments)")
