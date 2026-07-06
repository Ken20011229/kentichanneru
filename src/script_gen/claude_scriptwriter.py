import json
import logging
import os
from pathlib import Path
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_HINTS_FILE = "data/prompt_hints.json"
_COMPETITOR_INSIGHTS_FILE = "data/competitor_insights.json"


def _load_hints(channel_id: str = None) -> str:
    """Load self-improvement hints and inject into prompt."""
    lines = []

    if Path(_HINTS_FILE).exists():
        try:
            with open(_HINTS_FILE, "r", encoding="utf-8") as f:
                h = json.load(f)
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
        except Exception:
            pass

    if channel_id and Path(_COMPETITOR_INSIGHTS_FILE).exists():
        try:
            with open(_COMPETITOR_INSIGHTS_FILE, "r", encoding="utf-8") as f:
                comp = json.load(f)
            genre = comp.get(channel_id)
            if genre:
                lines.append("## 同ジャンルで今伸びている他チャンネルのタイトル例（参考にする、コピーはしない）")
                for t in genre.get("top_titles", [])[:5]:
                    lines.append(f"- {t}")
                if genre.get("top_bracket_patterns"):
                    lines.append(f"よく使われる【】パターン: {'、'.join(genre['top_bracket_patterns'])}")
        except Exception:
            pass

    return "\n".join(lines)

_RESEARCH_PROMPT = """\
以下は最近のYouTube動画タイトルです。このタイトルから「視聴者が深く学べる一般的・教育的テーマ」を抽出し、そのテーマについて詳しい調査レポートを日本語で作成してください。

元タイトル: {original_title}

手順:
1. まずこのニュースが示す「より大きなテーマ・背景」を特定する（例: 「米イラン覚書」→「核不拡散条約と国際外交の仕組み」）
2. その一般的テーマについて、以下の視点で詳しく解説する:
   - 歴史的背景と重要な転換点（具体的な年・人物・事件）
   - 仕組み・メカニズム（なぜそうなるのか）
   - 具体的なデータ・統計（数字・規模）
   - 意外な事実・一般的な誤解
   - 日本と海外の比較
   - 社会・生活への影響
   - 今後の展望

最初の1行目に「テーマ:【抽出した教育的テーマ名】」と書き、その後にレポートを500〜700文字で書く。マークダウン不要。テキストのみ。
"""

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
  "script_segments": [
    {{"text": "4〜6文のナレーション段落。各文は句点で終わる。合計150〜200文字。", "keyword": "キーワード（10文字以内。なければ空文字）", "visual_type": "intro", "animation_style": "scale_intro"}},
    {{"text": "4〜6文のナレーション段落。各文は句点で終わる。合計150〜200文字。", "keyword": "キーワード", "visual_type": "image", "animation_style": "fade", "image_prompt": "A detailed English prompt for FLUX image generation"}},
    ...
  ],
  "shorts_script_segments": [
    {{"text": "ナレーション1文。句点で終わる。", "keyword": "キーワード（10文字以内）", "visual_type": "intro", "animation_style": "pop"}},
    {{"text": "ナレーション1文。句点で終わる。", "keyword": "キーワード", "visual_type": "keyword", "animation_style": "slide_up"}},
    {{"text": "ナレーション1文。句点で終わる。", "keyword": "キーワード", "visual_type": "image", "animation_style": "fade", "image_prompt": "English portrait prompt for FLUX (9:16 vertical)"}},
    ...
  ],
  "shorts_hook": "Shorts冒頭で使う1〜2文（一瞬で引きつける内容）",
  "thumbnail_title": "サムネイル用テキスト（15文字以内、視覚的インパクト重視。キーワードは「」で囲むと黄色ハイライトで強調表示される。例:「単語」は存在しない）",
  "reaction_text": "キャラクターの吹き出し反応文（8文字以内。例: えっ、マジ!? / それだけ!? / 嘘でしょ!）",
  "image_search_keywords": ["英語キーワード1", "英語キーワード2", "英語キーワード3", "英語キーワード4", "英語キーワード5"]
}}

## ★ 厳守：動画5〜7分 ★ セグメントは最低12個、各150〜200文字
- セグメント数: **最低12段落、目安14〜16段落**
- 各 `text`: **4〜6文、150〜200文字**（短いと動画が1分未満になる。150文字未満は絶対NG）
- 合計2000文字以上のナレーションが必要

## 映像レイアウト（4レイヤー独立）
キャラ（固定）/ 記事画像（中央アニメ）/ テキスト（字幕アニメ）/ 背景 は独立して動く。
`visual_type: "image"` を40〜50%以上のセグメントに使用。image_promptは英語で具体的なシーン・構図を指定。

## コンテンツ設計
- 驚きの事実を5つ以上（「知らなかった！」感）
- 具体的な数字・比較・意外な切り口を必ず含める
- 「あなたの生活にも影響する」視点を1箇所入れる

## 推奨構成（14〜16段落目安）
つかみ(1)→概要(2)→背景・歴史(2)→詳細A(2)→詳細B(2)→データ(2)→影響(2)→見解(1)→展望(1)→まとめCTA(1)

## script_segments の規則
- 最低12段落（目安14〜16段落）。各textは4〜6文・150〜200文字
- 横動画は Shorts より深く掘り下げること
- tags は12〜15個（「{channel_name}」「ずんだもん」「VOICEVOX」を含める）

## shorts_script_segments について（縦画面 9:16 専用）
- Shorts だけで完結する独立した動画として構成する（合計 55 秒以内）
- セグメント数は**最低7文**（目安 9〜12 文）。必ず7文以上にすること
- 最後のセグメントは必ず「チャンネル登録と高評価をお願いします！」で締める
- 各テキストは**20文字以内**に抑える（縦画面で2行以内に収まるよう短く）
- 最初の1〜2文で視聴者を絶対に引き込む（スワイプされないフック）

## Shorts visual_type の選び方（縦画面向けレイアウト）
| 値 | 縦画面での見え方 | 使うタイミング |
|---|---|---|
| "intro" | アクセントカラーのグラデーション背景＋巨大キーワード＋テキストカード | 最初の1〜2文（フック） |
| "keyword" | 全幅アクセントバンドにキーワード、下にテキストカード | 重要な概念・数字・転換点 |
| "point" | アクセントラインとバッジ＋大きめテキストカード | 強調したいポイント |
| "detail" | キーワードヒーローボックス＋通常テキストカード（デフォルト） | 説明・解説 |
| "image" | AI生成イラストがフルフレーム表示（キャラなし）、テキストオーバーレイ | 視覚的に印象づけたい場面（1〜2回まで） |

## Shorts image_prompt について（visual_type が "image" のセグメントのみ）
- 必ず英語で書く（FLUX画像生成AI向け）
- **縦画面（9:16）** を意識した構図・被写体を指定する
- 例: "close-up portrait of scientist with glowing blue data streams, vertical composition, cinematic lighting"
- 例: "vertical shot of futuristic city skyline at night, neon reflections, dramatic sky, portrait orientation"

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
        hints = _load_hints(channel_id=ch.get("id"))
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

    def _research_topic(self, original_title: str) -> str:
        """Step 1: Generate a detailed research summary for the topic (plain text)."""
        prompt = _RESEARCH_PROMPT.format(original_title=original_title)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content.strip()
        logger.info(f"Research summary generated ({len(text)} chars) for: {original_title}")
        return text

    def generate_deep_dive(self, original_title: str, channel: dict = None) -> dict:
        """2-step deep dive: research → script.

        Step 1 extracts a general educational topic from the specific news title
        and generates a research summary. Step 2 feeds a concise version into
        the proven SCRIPT_PROMPT.
        """
        research_text = self._research_topic(original_title)

        # Extract the "テーマ:【...】" header if the model wrote one
        lines = research_text.splitlines()
        extracted_theme = original_title
        body_start = 0
        if lines and lines[0].startswith("テーマ:"):
            extracted_theme = lines[0].replace("テーマ:", "").strip()
            body_start = 1
        research_body = "\n".join(lines[body_start:]).strip()

        # Keep summary ≤ 300 chars so total prompt stays within the range
        # where the model reliably produces 65+ segments.
        summary_excerpt = research_body[:300]

        item = {
            "title": extracted_theme,
            "summary": (
                f"【深掘り解説動画】このテーマをゼロから丁寧に解説する決定版。"
                f"起源・仕組み・意外な事実・日本との比較・社会への影響・展望を多角的に深掘りする。\n"
                f"調査概要: {summary_excerpt}"
            ),
            "source": f"deep_dive（元ネタ: {original_title}）",
        }
        logger.info(f"Deep-dive extracted theme: {extracted_theme}")
        return self.generate(item, channel=channel)

    def _validate(self, data: dict, min_segments: int = 12):
        required = ["title", "description", "tags", "script_segments", "thumbnail_title", "image_search_keywords"]
        for key in required:
            if key not in data:
                raise ValueError(f"Script response missing required key: '{key}'")
        segments = data["script_segments"]
        if not segments:
            raise ValueError("script_segments is empty")
        if len(segments) < min_segments:
            raise ValueError(
                f"Too few script_segments: {len(segments)} (minimum {min_segments} required for 5-7 min video)"
            )
        if len(segments) < 14:
            logger.warning(f"script_segments below target: {len(segments)} (target 14-16 for 5-7 min video)")
        # Each segment must be a paragraph (150+ chars) — short segments make < 2 min videos
        texts = [seg.get("text", "") for seg in segments]
        total_chars = sum(len(t) for t in texts)
        avg_chars = total_chars / max(len(texts), 1)
        short_segs = sum(1 for t in texts if len(t) < 120)
        if avg_chars < 120:
            raise ValueError(
                f"Average segment too short: {avg_chars:.1f} chars "
                f"(need avg 150+ chars per segment — each segment must be 4-6 sentences, 150-200 chars)"
            )
        if short_segs > len(segments) * 0.3:
            logger.warning(
                f"{short_segs}/{len(segments)} segments are under 120 chars — "
                f"video may be shorter than 5 minutes. Retrying..."
            )
            raise ValueError(
                f"Too many short segments: {short_segs}/{len(segments)} under 120 chars. "
                f"Each segment must be 150-200 chars (4-6 sentences)."
            )
        valid_vtypes = {"intro", "point", "keyword", "detail", "image"}
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

        total_chars = sum(len(t) for t in texts)
        logger.info(
            f"Script: {len(segments)} segments, total {total_chars} chars, "
            f"avg {avg_chars:.0f} chars/seg, est. {total_chars/9/60:.1f} min"
        )

        shorts_segs = data.get("shorts_script_segments", [])
        if not shorts_segs:
            logger.warning("shorts_script_segments missing — Shorts will use truncated main audio")
        else:
            _shorts_valid_vtypes = {"intro", "keyword", "point", "detail", "image"}
            for i, sseg in enumerate(shorts_segs):
                if "text" not in sseg:
                    raise ValueError("shorts_script_segment missing 'text' field")
                if "keyword" not in sseg:
                    sseg["keyword"] = ""
                # Backfill visual_type
                if sseg.get("visual_type") not in _shorts_valid_vtypes:
                    sseg["visual_type"] = "detail"
                # Backfill animation_style
                if sseg.get("animation_style") not in valid_anims:
                    sseg.pop("animation_style", None)
                # image type requires image_prompt
                if sseg["visual_type"] == "image" and not sseg.get("image_prompt", "").strip():
                    sseg["visual_type"] = "detail"
            # Force first shorts segment to intro
            if shorts_segs:
                shorts_segs[0]["visual_type"] = "intro"
            if len(shorts_segs) < 5:
                raise ValueError(
                    f"shorts_script_segments too short: {len(shorts_segs)} segments "
                    f"(minimum 5 required). Retrying..."
                )
            logger.info(f"Shorts script: {len(shorts_segs)} segments")
