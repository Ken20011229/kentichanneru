import json
import logging
import os
from pathlib import Path
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_HINTS_FILE = "data/prompt_hints.json"


def _load_hints() -> str:
    """Load self-improvement hints and inject into prompt."""
    if not Path(_HINTS_FILE).exists():
        return ""
    try:
        with open(_HINTS_FILE, "r", encoding="utf-8") as f:
            h = json.load(f)
        lines = []
        if h.get("style_notes"):
            lines.append("## 過去の実績から学んだ改善点（必ず反映）")
            for note in h["style_notes"]:
                lines.append(f"- {note}")
        if h.get("top_titles_by_view"):
            lines.append("## 高再生数タイトル例（このスタイルを参考に）")
            for t in h["top_titles_by_view"][:5]:
                lines.append(f"- {t}")
        if h.get("top_titles_by_retention"):
            lines.append("## 高視聴維持率タイトル例（内容の深さを参考に）")
            for t in h["top_titles_by_retention"][:3]:
                lines.append(f"- {t}")
        ctr = h.get("global_ctr_pct", 0)
        ret = h.get("global_retention_sec", 0)
        if ctr or ret:
            lines.append(f"## 現在のチャンネル平均指標: CTR={ctr}% / 平均視聴時間={ret}秒")
        return "\n".join(lines)
    except Exception:
        return ""

SCRIPT_PROMPT = """\
あなたはYouTubeチャンネル登録者数100万人を達成したバイラル動画の台本作家です。
ジャンル: 「{channel_name}」
ナレータースタイル: {script_style}

## バイラル動画の鉄板法則（必ず守ること）
- 最初の2文で「え？なんで？」という疑問か「信じられない！」という感情を引き出す
- 数字・固有名詞・具体的データを必ず入れる（「約〜」「複数の」は禁止）
- 視聴者が「これ友達に話したい」と思うような驚きの事実を1つ以上含める
- 「〜かもしれません」「〜と思われます」などの曖昧表現は絶対に使わない
- タイトルの【】内は視聴者の感情を動かす動詞系（判明・暴露・激変・崩壊・禁断・革命・衝撃など）

## 入力情報
タイトル: {title}
概要: {summary}
ソース: {source}

## 出力要件
以下のJSON形式のみで返答（マークダウン不要）:
{{
  "title": "【感情動詞】具体的内容『固有名詞』の形式。30文字以内。数字があれば必ず入れる（例:【判明】ChatGPT-5が2週間で開発費100億円を回収）",
  "description": "YouTube説明欄（400〜500文字）。構成: ①冒頭に視聴者が気になる一文 ②内容の要点3つを箇条書き ③関連情報・背景 ④チャンネル登録・高評価の呼びかけ ⑤ハッシュタグ10個（#で始まる）。SEOを意識した自然な文章で。",
  "tags": ["タグ1", ...],
  "script_segments": [
    {{"text": "ナレーション1文目", "keyword": "キーワード（10文字以内、なければ空文字）", "visual_type": "intro"}},
    {{"text": "ナレーション2文目", "keyword": "AI", "visual_type": "detail"}},
    {{"text": "以降も同様", "keyword": "キーワード", "visual_type": "point"}}
  ],
  "shorts_script_segments": [
    {{"text": "ナレーション文", "keyword": "キーワード（10文字以内、なければ空文字）"}},
    ...
  ],
  "shorts_hook": "YouTube Shorts用の冒頭15秒ナレーション（1〜2文、視聴者を一瞬で引きつける衝撃的な一言から始める）",
  "thumbnail_title": "サムネイル用（15文字以内、数字や記号を使って視覚的インパクト最大に）",
  "image_search_keywords": ["英語キーワード1", "英語キーワード2", "英語キーワード3", "英語キーワード4", "英語キーワード5"]
}}

## shorts_script_segments の構成（厳守・最低8セグメント必須）
- 必ず8〜12セグメント生成すること（7以下は絶対不可）
- 合計55秒以内（1文あたり目安25〜35文字）
- Shortsだけで完全に内容が伝わる独立した動画として構成する
- 構成（順番通りに）:
  ① 衝撃フック 1〜2文（視聴者を一瞬で引きつける）
  ② テーマの背景・状況説明 2〜3文（何が起きたか・誰が関係するか）
  ③ 具体的な詳細・数字・固有名詞を使った説明 3〜4文（しっかり内容を伝える）
  ④ 視聴者への影響・なぜ重要か 1〜2文
  ⑤ まとめ1文＋「チャンネル登録と高評価をお願いします！」1文（必ず最後）
- 各セグメントは句点「。」で終わる1文
- keyword: そのセグメントの最重要語（10文字以内）、ない場合は空文字

## visual_type の割り当てルール（厳守）
- 最初のセグメント（index 0）は必ず "intro" にすること
- 最後のセグメント（まとめ・CTA）は必ず "point" にすること
- キーコンセプトや重要な用語を初めて紹介するセグメントは "keyword" にすること
- 番号付き・構造化された情報のセグメントは "point" にすること
- 説明・背景・補足のセグメントは "detail" にすること

## keyword フィールドのルール
- そのセグメントで最も重要な1つの用語（人名・技術名・イベント名など）
- 10文字以内
- 特に重要な語がなければ空文字 "" にすること

## script_segments の構成（厳守）
- 合計16〜20セグメント、12未満は不可
- 1文あたり必ず40〜70文字（短すぎる文は内容が薄くなるため禁止）
- 各セグメントは句点「。」で終わる1文
- 必ず具体的な数字・固有名詞・事実を含める（「〜と思われます」「〜かもしれません」等の曖昧表現は禁止）
- 横動画はShortsとは独立した深い内容にすること（Shortsとの重複は可だが手抜きにならないこと）
- 構成（順番と文数を厳守）:
  【冒頭フック 2文】「え？本当に？」となる衝撃的な事実か問いかけで開始。「実は〜」「信じられないことに〜」から始める
  【予告 1文】「この動画では○○について徹底解説します。」で続きへの期待感を高める
  【詳細解説 8〜10文】以下を全て盛り込む:
    ・何が起きたか（Who/What/When/Where）2〜3文
    ・なぜ起きたか・経緯・背景 2〜3文
    ・具体的な数字・規模・金額・割合など 1〜2文
    ・関係する人物・組織・国の詳細説明 1〜2文
    ・専門用語・概念を視聴者に分かるよう噛み砕いた説明 1〜2文
  【背景・意義 3〜4文】なぜ今これが重要か・視聴者の生活にどう関係するか
  【まとめ・CTA 2文】要点を一言でまとめ＋「チャンネル登録と高評価をお願いします！」

## タグ戦略（厳守）
- 合計12〜15個
- 広域タグ3個（月間検索数の多い一般ワード）
- ニッチタグ5個（このトピック固有のワード）
- トレンドタグ4個（今の時事・関連ワード）
- チャンネルタグ（{channel_name}、VOICEVOX、ずんだもん）

## description SEO要件
- 冒頭100文字に主要キーワードを自然に含める
- ハッシュタグは最後にまとめて10個
- 視聴者が検索しそうな言葉を自然に散りばめる
"""


class ClaudeScriptWriter:
    def __init__(self, config: dict):
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY") or config.get("groq_api_key", ""))
        self.model = config.get("groq_model", "llama-3.3-70b-versatile")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
    def generate(self, item: dict, channel: dict = None) -> dict:
        ch    = channel or {}
        hints = _load_hints()
        prompt = SCRIPT_PROMPT.format(
            channel_name=ch.get("name", "ニュース"),
            script_style=ch.get("script_style", "ニュースキャスター。客観的・正確・簡潔に伝える。"),
            title=item.get("title", ""),
            summary=item.get("summary", ""),
            source=item.get("source", ""),
        )
        if hints:
            prompt += f"\n\n{hints}"
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
        if len(segments) < 12:
            raise ValueError(f"Too few script_segments: {len(segments)} (minimum 12 required)")
        if len(segments) < 15:
            logger.warning(f"script_segments below target: {len(segments)} (target 16-20)")
        valid_vtypes = {"intro", "point", "keyword", "detail"}
        for seg in segments:
            if "text" not in seg:
                raise ValueError("script_segment missing 'text' field")
            # Backfill visual metadata if LLM omitted them
            if "keyword" not in seg:
                seg["keyword"] = ""
            if "visual_type" not in seg or seg["visual_type"] not in valid_vtypes:
                seg["visual_type"] = "detail"
        # Force first segment to intro regardless of LLM output
        segments[0]["visual_type"] = "intro"

        total_chars = sum(len(seg["text"]) for seg in segments)
        logger.info(f"Script total chars: {total_chars} ({len(segments)} segments)")

        shorts_segs = data.get("shorts_script_segments", [])
        if not shorts_segs:
            logger.warning("shorts_script_segments missing — Shorts will use truncated main audio")
        else:
            for sseg in shorts_segs:
                if "keyword" not in sseg:
                    sseg["keyword"] = ""
                if "text" not in sseg:
                    raise ValueError("shorts_script_segment missing 'text' field")
            if len(shorts_segs) < 7:
                raise ValueError(
                    f"shorts_script_segments too short: {len(shorts_segs)} segments "
                    f"(minimum 8 required). Retrying..."
                )
            logger.info(f"Shorts script: {len(shorts_segs)} segments")
