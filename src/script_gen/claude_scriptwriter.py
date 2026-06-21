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
あなたはYouTubeチャンネル「{channel_name}」のプロデューサーです。
ナレータースタイル: {script_style}

## 今回のニュース
タイトル: {title}
概要: {summary}
ソース: {source}

## まず考えてから書く
このニュースを動画にするとき、どんな切り口が最も面白いか・驚きがあるかを考えてください。
- 視聴者が「え、そうなの？」と前のめりになる角度はどこか
- 数字・固有名詞・具体的な事実のうち、一番インパクトが強いものはどれか
- タイトルはどんな言い回しが一番クリックしたくなるか（形式は自由）

## 品質基準（形式の縛りはないが、これだけは守る）
- 最初の1〜2文で視聴者を引き込む（疑問・驚き・共感のどれかで）
- 具体的な数字・固有名詞・事実を必ず入れる
- 「〜かもしれません」「〜と思われます」などの曖昧表現は使わない
- 視聴者が誰かに話したくなる「驚きの一点」を含める
- 最後は自然にまとめ＋チャンネル登録・高評価の呼びかけで締める

## 出力形式（JSON のみ、マークダウン不要）
{{
  "title": "動画タイトル（30文字以内、クリックしたくなる表現。形式は自由）",
  "description": "YouTube説明欄（400〜500文字）。冒頭にキーワードを自然に含め、内容のポイント・背景・呼びかけ・ハッシュタグ10個を含める。",
  "tags": ["タグ1", ...],
  "bgm_mood": "calm",
  "script_segments": [
    {{"text": "ナレーション1文。句点で終わる。", "keyword": "キーワード（10文字以内。なければ空文字）", "visual_type": "intro", "animation_style": "scale_intro"}},
    {{"text": "ナレーション1文。句点で終わる。", "keyword": "キーワード", "visual_type": "image", "animation_style": "fade", "image_prompt": "A detailed English prompt for FLUX image generation"}},
    ...
  ],
  "shorts_script_segments": [
    {{"text": "ナレーション文。句点で終わる。", "keyword": "キーワード（10文字以内。なければ空文字）"}},
    ...
  ],
  "shorts_hook": "Shorts冒頭で使う1〜2文（一瞬で引きつける内容）",
  "thumbnail_title": "サムネイル用テキスト（15文字以内、視覚的インパクト重視）",
  "image_search_keywords": ["英語キーワード1", "英語キーワード2", "英語キーワード3", "英語キーワード4", "英語キーワード5"]
}}

## script_segments について
- セグメント数は**最低8文**（目安 10〜20 文）。情報が少なくても背景・歴史・影響・今後の展望などで補完して必ず8文以上にすること
- 各セグメントは句点「。」で終わる1文
- 横動画は Shorts より深く掘り下げた内容にすること
- tags は合計 12〜15 個（チャンネルタグ「{channel_name}」「ずんだもん」「VOICEVOX」を含める）

## shorts_script_segments について
- Shorts だけで完結する独立した動画として構成する（合計 55 秒以内が目安）
- セグメント数は**最低5文**（目安 7〜12 文）。情報が少なくても必ず5文以上にすること
- 最後のセグメントは必ず「チャンネル登録と高評価をお願いします！」で締める

## bgm_mood の選び方（動画全体の雰囲気に合わせて1つ選ぶ）
| 値 | 雰囲気 |
|---|---|
| "upbeat" | 明るく前向き・テンポよい |
| "calm" | 穏やか・落ち着いた解説 |
| "tense" | 緊張感・サスペンス系 |
| "inspiring" | 感動的・希望ある未来 |
| "neutral" | 中立・汎用 |
| "epic" | 壮大・歴史・宇宙 |
| "tech" | テクノロジー・AI・未来 |
| "news" | ニュース・報道 |
| "science" | 科学・研究・発見 |

## visual_type の選び方（自由に選んでよい）
| 値 | 画面上での見え方 | 使うタイミングの例 |
|---|---|---|
| "intro" | アクセントカラーの大きな文字（動画冒頭向け） | 最初のセグメント |
| "keyword" | アクセントカラーの背景ボックス＋白文字 | キーワード・概念を強調したいとき |
| "point" | アクセントカラーの縁取り＋下からスライドイン | 重要ポイント・まとめ文 |
| "detail" | シンプルな白文字＋暗い縁取り | 通常の解説・背景説明 |
| "image" | AI生成イラストがフルフレームで表示、テキスト下部オーバーレイ | 視覚的に印象づけたい重要な場面（全体の20〜30%推奨） |

## image_prompt の書き方（visual_type が "image" のセグメントのみ必須）
- 必ず英語で書く
- FLUX画像生成AIへの具体的な指示文（シーン・構図・雰囲気・スタイルを含める）
- 例: "futuristic data center with glowing servers, cinematic blue lighting, photorealistic 4k"
- 例: "scientist examining DNA structure, laboratory setting, dramatic lighting, hyperrealistic"
- 抽象的すぎず、ニュースの内容を視覚的に表現するものにする

## animation_style の選び方（省略可。省略時は visual_type に応じて自動決定）
動画の雰囲気に合わせてセグメントごとに字幕テキストの登場アニメーションを指定できる。
バリエーションを豊富に使い、同じ animation_style が連続しないよう意識すること。

| 値 | 効果 | 向いている場面 |
|---|---|---|
| "fade" | シンプルなフェードイン | 落ち着いた解説、締め |
| "scale_in" | ゆっくりズームイン（82%→100%） | 標準的な強調 |
| "scale_intro" | 大きめズームイン（88%→100%） | 冒頭・タイトル |
| "pop" | 瞬間的に弾けて登場（60%→112%→100%） | 驚き・数字の発表 |
| "bounce" | 跳ねながら落ち着く（90%→106%→100%） | 勢いのある主張 |
| "blur_in" | ぼかしからシャープに | 謎・技術・調査結果 |
| "glow_in" | 光のにじみから登場 | 感動・発見・美しい事実 |
| "spin_in" | 軽く回転して正位置に収まる | 転換点・意外な展開 |
| "slide_up" | 画面下から上へスライド | 重要ポイント・強調 |
| "slide_down" | 画面上から下へ落下 | 警告・注意・落とし穴 |
| "slide_left" | 右端からスライドイン | テンポよく続く内容 |
| "slide_right" | 左端からスライドイン | 場面転換・視点の切り替え |
| "float_up" | ゆっくり浮かび上がる | 余韻・締め・感情的な場面 |
| "snap" | 瞬間的にパッと現れる | 短い情報・箇条書き的な説明 |
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
        if len(segments) < 8:
            raise ValueError(f"Too few script_segments: {len(segments)} (minimum 8 required)")
        if len(segments) < 12:
            logger.warning(f"script_segments below target: {len(segments)} (target 12-20)")
        valid_vtypes = {"intro", "point", "keyword", "detail", "image"}
        valid_moods  = {"upbeat","calm","tense","inspiring","neutral","sad","epic","tech","news","science"}
        if data.get("bgm_mood") not in valid_moods:
            data["bgm_mood"] = "neutral"

        valid_anims  = {
            "fade", "scale_in", "scale_intro", "pop", "bounce",
            "blur_in", "glow_in", "spin_in",
            "slide_up", "slide_down", "slide_left", "slide_right",
            "float_up", "snap",
        }
        for seg in segments:
            if "text" not in seg:
                raise ValueError("script_segment missing 'text' field")
            # Backfill visual metadata if LLM omitted them
            if "keyword" not in seg:
                seg["keyword"] = ""
            if "visual_type" not in seg or seg["visual_type"] not in valid_vtypes:
                seg["visual_type"] = "detail"
            # Remove invalid animation_style so subtitle_gen uses its default
            if seg.get("animation_style") not in valid_anims:
                seg.pop("animation_style", None)
            # image type requires image_prompt; strip empty ones
            if seg["visual_type"] == "image" and not seg.get("image_prompt", "").strip():
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
            if len(shorts_segs) < 4:
                raise ValueError(
                    f"shorts_script_segments too short: {len(shorts_segs)} segments "
                    f"(minimum 4 required). Retrying..."
                )
            logger.info(f"Shorts script: {len(shorts_segs)} segments")
