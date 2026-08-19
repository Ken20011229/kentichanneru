import copy
import json
import logging
import os
import re
from pathlib import Path
from groq import Groq, NotFoundError, RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential

from src.groq_models import call_kwargs

logger = logging.getLogger(__name__)

# ── Language / content sanity helpers ───────────────────────────────────────────
# Hangul (Korean) plus a curated set of simplified-Chinese-only characters that
# never appear in natural Japanese. Their presence means the LLM leaked another
# language into the script (e.g. writing 「这」 instead of 「これ」), which reads as a
# glaring defect on-screen. Each listed CJK char is a *simplified* form whose
# Japanese equivalent is a different codepoint, so false positives are near zero.
_HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_CJK_FOREIGN = set(
    "这么们说时过还现东车话问应该让见关开无优势结经网电脑术达边门间长对实样给变"
    "头儿从众华协单卖买龙岁报亚严丽举习书图页领风飞马鸟鱼鸡鸣顾颜顺给织级红纯纸"
)

# Generic, content-free keywords that make poor center graphics — they say nothing
# about the actual topic and look lazy when repeated across sections.
_BANNED_KEYWORDS = {
    "問題点", "生活", "影響", "変化", "新機能", "概要", "詳細", "まとめ",
    "ポイント", "背景", "課題", "可能性", "結論", "理由", "原因", "目的",
    "特徴", "解説", "内容", "情報", "話題", "注目", "今後", "現状", "重要",
}


# 台本の接地チェックに使うパターン
_PLACE_RE  = re.compile(r"(北海道|東京都|(?:京都|大阪)府|[一-龥]{2,3}県)")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|ミリ|km|キロ|度|℃|%|パーセント|人|名|億円|万円|円|回|件|年|か月|ヶ月|日間|時間|倍|台)")
_FILLER_RE = re.compile(r"(ご視聴ありがとう|視聴ありがとう|よろしくお願いします|次の動画も)")
_CTA_RE    = re.compile(r"(チャンネル登録|高評価)")


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _keeps_topic(original: str, expanded: str, threshold: float = 0.4) -> bool:
    """伸長後の段落が、元の段落と同じ内容を指しているか。

    リペアが段落数を変えたときに zip の対応がズレていないかを確かめる用途。
    伸長は元の文を残したまま背景や具体例を足す操作なので、対応が取れていれば
    元の文字 bigram の多くがそのまま残る。言い換えを含んでも半分は残るため、
    閾値は 0.4 で「別の段落を掴んでいる」ケースだけを弾く。
    """
    base = _bigrams(original)
    if not base:
        return False
    return len(base & _bigrams(expanded)) / len(base) >= threshold


# 段落数がプロンプトの下限をこれだけ下回るまでは、作り直さずに受け入れる。
# 下限ちょうどを1段落だけ外した台本を捨てて全文を再生成すると、1回あたり
# 6,000〜7,000トークンを消費する。Groq の on_demand は TPD 10万なので、
# この作り直しが数回重なるだけで日次予算を使い切り、以降の記事は 429 で
# 全滅する（実測: run 30356281642 は 12/13 段落の作り直しを起点に破綻）。
# 段落が1つ少なくても尺は約20秒縮むだけで、文字数・反復・接地の各チェックは
# そのまま効くため、品質より予算を守るほうが割に合う。
_SEGMENT_SHORTFALL_TOLERANCE = 1

# 本文平均文字数の合格ライン。通常はこれ、全滅時の救済（salvage）ではこれを
# 下回る台本も受け入れる。実測 08-03 の失敗は本文平均 111.8 字 / 112.7 字
# ——合格ラインに 2〜3 字届かないだけの台本を捨てて投稿ごと消していた。
_MIN_AVG_CHARS     = 115
_SALVAGE_MIN_AVG_CHARS = 95


class ScriptTooShort(ValueError):
    """段落が短いだけの不合格。作り直さず、伸ばすリペアで回復できる。

    実測では qwen3.6-27b が本文平均 104〜121 字、gpt-oss-120b が 88 字しか書かず、
    要求している 120〜170 字にはどちらもゼロから書かせる限り届かない。同じ
    プロンプトを投げ直しても同じ長さが返るだけなので、作り直しは 6,000 トークンを
    捨てるのと変わらない。既にある文を膨らませるほうが確実で、実測で 115→126 字。
    """


class TokenBudgetExhausted(RuntimeError):
    """この実行に割り当てたトークンを使い切った。"""


# ── 1 実行あたりのトークン予算 ────────────────────────────────────────────────
# Groq の on_demand は TPD 10 万（モデルごとに独立した枠）。1 日 2 投稿なので
# 1 回 5 万が公平な取り分で、安全側に 4.5 万を上限とする。
# これが無いと、失敗した 1 回が日次枠を全部焼き切って以降の回まで 429 で全滅する
# （実測: 07-27〜07-29 の 4 回連続失敗はこの連鎖。1 実行で 12 回の作り直しを
# 走らせて 67,000 トークンを消費していた）。上限に当たったら早く諦めて、
# 残りを次の回に残すほうが 1 日あたりの投稿数は増える。
_TOKEN_BUDGET_PER_RUN = 45_000
_tokens_used: dict[str, int] = {}


def _spend_tokens(model: str, n: int):
    _tokens_used[model] = _tokens_used.get(model, 0) + n


def _budget_left(model: str) -> int:
    return _TOKEN_BUDGET_PER_RUN - _tokens_used.get(model, 0)


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def _parse_json_lenient(raw: str) -> dict:
    """gpt-oss は ```json フェンスを付けて返すことがあるので剥がしてから読む。"""
    text = _JSON_FENCE_RE.sub("", raw.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _segment_range(material_chars: int) -> tuple[int, int]:
    """素材の分量から、要求する段落数の範囲を決める。

    以前は素材の量に関係なく「最低12段落・合計2000文字以上」を固定で要求して
    いた。RSS の要約は実測 104〜149 文字しかないため、約16倍の水増しを強制する
    ことになり、モデルは捏造と同一文の反復でそれを埋めていた。
    """
    if material_chars >= 2000:
        return 13, 16
    if material_chars >= 900:
        return 11, 14
    if material_chars >= 400:
        return 9, 12
    return 8, 10


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[。！？])", text or "") if len(s.strip()) > 8]


_NOUNISH_RE = re.compile(r"[一-龥ァ-ヶーA-Za-z0-9]{2,8}")


def _keyword_from_text(text: str, used: set) -> str:
    """段落本文から画面中央に出せる具体語を1つ拾う。

    禁止語・既出語で keyword を空にすると、slide_gen が「巨大な黒箱に薄い数字と
    セクション名」だけの情報量ゼロのパネルに落ちる。空にする前に本文から拾い直す。
    """
    candidates = [
        w for w in _NOUNISH_RE.findall(text or "")
        if 2 <= len(w) <= 8 and w not in _BANNED_KEYWORDS and w not in used
    ]
    if not candidates:
        return ""
    # 長い語ほど具体的な傾向があるので最長を採る
    return max(candidates, key=len)


def _find_foreign_chars(text: str) -> set:
    """Return the set of non-Japanese (Chinese/Korean) characters found in text."""
    if not text:
        return set()
    found = {ch for ch in text if ch in _CJK_FOREIGN}
    m = _HANGUL_RE.search(text)
    if m:
        found.add(m.group())
    return found


def _clean_title(title: str) -> str:
    """Strip a leading emphasis tag whose inner text is pure ASCII/Latin
    (e.g. 【revolution】 or [buzz]) — those are LLM leaks, not real Japanese hooks.
    Japanese-content tags (【判明】 etc.) and inline brand names (Google) are kept."""
    if not title:
        return title
    m = re.match(r"^\s*[【\[［]([^】\]］]{1,20})[】\]］]\s*", title)
    if m and re.fullmatch(r"[A-Za-z0-9 ._\-!?&]+", m.group(1) or ""):
        stripped = title[m.end():].strip()
        if stripped:
            logger.warning(f"Stripped non-Japanese title tag: {m.group(0)!r}")
            return stripped
    return title

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
            # 再生数が一桁の動画のタイトルを「高再生数の例」として注入すると、
            # ノイズを鉄板パターンとして学習してしまう（実測: 15再生 vs 9再生
            # の差から【衝撃】【判明】【革命】を"勝ちパターン"にしていた）。
            if h.get("top_titles_by_view"):
                lines.append("## 高再生数タイトル例（このスタイルを参考に）")
                for t in h["top_titles_by_view"][:5]:
                    lines.append(f"- {t}")
            if h.get("top_titles_by_retention"):
                lines.append("## 高視聴維持率タイトル例（内容の深さを参考に）")
                for t in h["top_titles_by_retention"][:3]:
                    lines.append(f"- {t}")
            # CTR=0% を毎回申告するのは無意味かつ有害（Analytics が未取得でも0になる）
            ctr = h.get("global_ctr_pct", 0)
            ret = h.get("global_retention_sec", 0)
            if ctr and ctr > 0.5:
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
                # 他局の番組ブランド名や実在人物名（「スーパーJチャンネル」
                # 「神谷宗幣VS辻元清美」など）が【】パターンとして混ざるため、
                # 一般的な短い感情語だけを残す
                patterns = [
                    p for p in genre.get("top_bracket_patterns", [])
                    if len(p) <= 5 and not re.search(r"[A-Za-z]{3,}|公式|チャンネル|VS|vs|番組", p)
                ]
                if patterns:
                    lines.append(f"よく使われる【】パターン: {'、'.join(patterns)}")
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

重要: **確実に知っている事実だけを書く。** 年号・数値・人名が不確かな場合は書かない。
推測を事実として書くくらいなら、その項目を省略する。

最初の1行目に「テーマ:【抽出した教育的テーマ名】」と書き、その後にレポートを500〜700文字で書く。マークダウン不要。テキストのみ。
"""

SCRIPT_PROMPT = """\
あなたはYouTubeチャンネル「{channel_name}」のプロデューサーです。
ナレータースタイル: {script_style}

## 今回のニュース
タイトル: {title}
本文: {summary}
ソース: {source}

## ★事実の絶対ルール（最優先。違反したら作り直し）★
- **上の「本文」に書かれていない固有名詞（地名・人名・企業名・施設名）、数値、日付、被害状況を新しく作らない。**
- 本文に無い一般知識を書くときは「一般的には」「過去の例では」と明示し、**今回の出来事として断定しない。**
- 尺が足りないときに事実を増やして埋めるのは禁止。同じ事実の「意味・背景・視聴者への影響」を掘り下げて長さを作る。
- 同じ文・同じ言い回しを別の段落で繰り返さない（言い換えての再掲も不可）。
- 媒体名（NHK NEWS など）や「続きを読む」といった配信上の文字列をナレーションに入れない。

## まず考えてから書く
このニュースを動画にするとき、どんな切り口が最も面白いか・驚きがあるかを考えてください。
- 視聴者が「え、そうなの？」と前のめりになる角度はどこか
- 数字・固有名詞・具体的な事実のうち、一番インパクトが強いものはどれか
- タイトルはどんな言い回しが一番クリックしたくなるか（形式は自由）

## 品質基準（形式の縛りはないが、これだけは守る）
- 最初の1〜2文で視聴者を引き込む（疑問・驚き・共感のどれかで）
- 本文にある具体的な数字・固有名詞・事実を必ず入れる
- タイトルと冒頭は断定的に書く（本編中で本文に無い推測を断定するのは禁止）
- 視聴者が誰かに話したくなる「驚きの一点」を含める
- 最後は自然にまとめ＋チャンネル登録・高評価の呼びかけで締める

## ナレーション文体（AI音声で読み上げます）
- 同じ語尾（〜です／〜ます／〜ています／〜ですね）を3文以上続けない。体言止め・問いかけを混ぜる。
- 同じ主語（例:「山梨県では」）で始まる文を隣り合う段落で繰り返さない。
- 1文は45文字以内。読点は句の切れ目にだけ置く。
- アルファベットの略語を使うときは初出でカタカナ読みを併記する（例: HTML（エイチティーエムエル））。

## ★言語と文字の絶対ルール（違反すると動画が台無しになる）★
- **すべて日本語で書く。** 簡体字・繁体字などの中国語の文字、ハングル、その他の外国語の文字を絶対に混ぜない。
  - 例:「これ」を「这」、「説明」を「说明」、「時間」を「时间」などと書くのは厳禁。日本語の漢字・ひらがな・カタカナのみを使う。
- タイトルや強調に【】を使う場合、中身は必ず日本語にする（例:【判明】【衝撃】【緊急】）。**【revolution】のように英単語だけを【】で囲むのは禁止。**
- Google や Excel など、正式名称が英語の固有名詞はそのまま英語表記でよい。

## ★keyword（画面中央のグラフィック）の絶対ルール★
- 各セグメントの keyword は、その回の題材そのものを表す具体的な語にする（例: 複合グラフ / グラフの種類 / Excel互換 / データ可視化）。
- 「問題点」「生活」「影響」「変化」「新機能」「概要」「詳細」「まとめ」「ポイント」「背景」「課題」「可能性」などの抽象的で内容の薄い語は禁止。
- **全セグメントで keyword を重複させない。** 同じ語を2回以上使わない。ふさわしい語が無ければ空文字にする。

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
  "shorts_title": "Shorts用タイトル（20文字以内。本編とは別の切り口で、単語の途中で切れない自然な日本語）",
  "thumbnail_title": "サムネイル用テキスト（8〜15文字。必ず数字か固有名詞を1つ含める。強調したい語は必ず「」（かぎ括弧）で囲むとアクセント色でハイライトされる。[ ] や 【 】ではなく必ず「」を使うこと。例:「5日連続」で過去最長 / 「40度」超えの猛暑）",
  "reaction_text": "キャラクターの吹き出し反応文（8文字以内。例: えっ、マジ!? / それだけ!? / 嘘でしょ!）",
  "image_search_keywords": ["英語キーワード1", "英語キーワード2", "英語キーワード3", "英語キーワード4", "英語キーワード5"]
}}

## ★ 尺の指定 ★
- セグメント数: **{min_segments}〜{max_segments}段落**（この範囲を外れるのは不可。多すぎても作り直し）
- **1段落目（フック）だけは 1〜2文・50〜70文字と短くする。** ここが長いと冒頭で離脱される
- **2段落目以降の各 `text`: 3〜5文、120〜170文字**（120文字未満は不可）
- 素材が薄いときに段落を水増しして埋めるより、範囲の下限で密度を保つほうがよい

## 映像レイアウト（4レイヤー独立）
キャラ（固定）/ 記事画像（中央アニメ）/ テキスト（字幕アニメ）/ 背景 は独立して動く。
`visual_type: "image"` を40〜50%以上のセグメントに使用。image_promptは英語で具体的なシーン・構図を指定。

## コンテンツ設計
- 驚きの事実を5つ以上（「知らなかった！」感）
- 具体的な数字・比較・意外な切り口を必ず含める
- 「あなたの生活にも影響する」視点を1箇所入れる

## 構成（段落ごとに必ず「役割」を持たせる）
段落数は上の指定に従うので、役割は**全体に対する位置**で決めること。
- **最初の1段落（フック）**: 結論の一部だけを提示し「なぜそうなるのか」は伏せる（オープンループ）。50〜70文字と短く
- **次の1段落（約束）**: この動画で分かることを3点宣言する（「3つ目が一番意外です」のように引きを作る）
- **前半（全体の 20〜45% 付近）**: 前提と経緯。各段落の最後は次の段落への疑問で終える
- **★中盤の山（全体の 45〜65% 付近）★**: 最大の驚き（本文にある数字）をここに置き、冒頭のオープンループを回収する
- **後半（65〜90% 付近）**: 影響・比較・視聴者の生活との接続
- **終盤（90% 付近）**: 展望（新しい問いを1つ残す）
- **最終段落**: まとめ＋チャンネル登録・高評価のCTA。**CTAの後ろに段落を足さない**

## script_segments の規則
- 1段落目は50〜70文字、2段落目以降は3〜5文・120〜170文字
- 横動画は Shorts より深く掘り下げること
- tags は12〜15個（「{channel_name}」を含める。1個30文字以内）

## shorts_script_segments について（縦画面 9:16 専用）
- Shorts だけで完結する独立した動画として構成する（合計 55 秒以内）
- セグメント数は**最低7文**（目安 9〜12 文）。必ず7文以上にすること
- **最初のセグメントは `shorts_hook` と同じ内容にする。タイトルをそのまま読ませない。**
- 各テキストは**20文字以内**に抑える（縦画面で2行以内に収まるよう短く）
- **末尾にチャンネル登録のお願いを入れない。** Shorts はループ再生で維持率が決まるので、
  最後の1文は最初の1文に自然につながる内容にしてループさせる（CTAは説明欄で行う）

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


_EXPAND_PROMPT = """\
あなたは日本語YouTube台本の編集者です。
以下の台本は各段落が短すぎて尺が足りません。**内容を変えずに各段落を膨らませて**ください。

## 絶対条件
- 段落の数・順序・話の流れは変えない（{n}個のまま返す）
- 2段落目以降は必ず **120〜170文字（3〜5文）** にする。今より短くしない
- 膨らませ方は「その段落で述べた事実の背景・理由・具体例・視聴者にとっての意味」を足す
- **元記事に無い地名・固有名詞・数値を新しく作らない**（下の元記事の範囲だけで書く）
- 1段落目（フック）は 50〜70文字のまま短く保つ
- 同じ文を他の段落で繰り返さない

## 元記事
{source}

## 現在の段落
{current}

## 出力
JSONのみを返す。形式: {{"segments": ["1段落目の全文", "2段落目の全文", ...]}}
"""


class ClaudeScriptWriter:
    def __init__(self, config: dict):
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY") or config.get("groq_api_key", ""))
        self.model = config.get("groq_model", "qwen/qwen3.6-27b")
        # 本編モデルの日次枠が尽きたとき・本編モデルが廃止されたときの逃げ先。
        # 枠はモデルごとに独立しているので、片方が 429 でも投稿を落とさずに済む。
        # 実測の文章量は qwen3.6-27b(120.6/136.8字) のほうが gpt-oss-120b
        # (100.9〜125.5字) より上なので、本編は qwen を主・こちらを従とする。
        self.fallback_model = config.get("groq_fallback_model", "openai/gpt-oss-120b")
        # Groq のレート上限はモデルごとに独立した枠。リサーチを本編台本と同じ
        # モデルで回すと、フォールバックが本編用の日次予算を削ってしまう。
        # 別モデルに逃がすぶんには、そもそも別の枠から引くので本編に影響しない。
        self.research_model = config.get("groq_research_model", "openai/gpt-oss-120b")
        # 長さ不足で落とした台本のうち最良の1本。全アイテムが全滅したときに
        # salvage() が緩めた基準で拾い直す（_avg, data, min_seg, max_seg, source）。
        self._salvage: tuple[float, dict, int, int | None, str] | None = None

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=4, max=30))
    def generate(self, item: dict, channel: dict = None) -> dict:
        ch    = channel or {}
        hints = _load_hints(channel_id=ch.get("id"))

        # 素材は本文を優先。要約しか無い記事に長尺を要求すると、埋めるために
        # 元記事に無い事実を創作する。素材の量に応じて段落数の要求を変える。
        material = (item.get("body") or "").strip() or item.get("summary", "")
        min_segments, max_segments = _segment_range(len(material))

        prompt = SCRIPT_PROMPT.format(
            channel_name=ch.get("name", "ニュース"),
            script_style=ch.get("script_style", "ニュースキャスター。客観的・正確・簡潔に伝える。"),
            title=item.get("title", ""),
            summary=material,
            source=item.get("source", ""),
            min_segments=min_segments,
            max_segments=max_segments,
        )
        if hints:
            prompt += f"\n\n{hints}"
        data = self._chat_json(prompt)

        source_text = f"{item.get('title', '')}\n{material}"
        try:
            self._validate(data, min_segments=min_segments, max_segments=max_segments,
                           source_text=source_text)
        except ScriptTooShort as e:
            # 同じプロンプトを投げ直しても同じ長さが返るだけなので作り直さない。
            # 既にある文を膨らませるほうがトークンも半分で済み、実測でも通る。
            logger.warning(f"{e} — 作り直さず、既存の段落を伸ばすリペアに回す")
            self._remember_salvage(data, min_segments, max_segments, source_text)
            data = self._expand_segments(data, source_text=source_text)
            try:
                self._validate(data, min_segments=min_segments, max_segments=max_segments,
                               source_text=source_text)
            except ScriptTooShort:
                self._remember_salvage(data, min_segments, max_segments, source_text)
                raise
        return data

    def _remember_salvage(self, data: dict, min_segments: int,
                          max_segments: int | None, source_text: str):
        """長さ不足で落ちた台本を、本文平均が最良のものだけ取っておく。

        投稿を1回まるごと落とすより、少し短い台本で出すほうが確実に良い。
        ここでは記録だけして、実際に使うかどうかは salvage() が判断する。
        """
        segments = (data or {}).get("script_segments") or []
        if not segments:
            return
        texts = [s.get("text", "") for s in segments]
        body  = texts[1:] or texts
        avg   = sum(len(t) for t in body) / max(len(body), 1)
        if self._salvage is None or avg > self._salvage[0]:
            self._salvage = (avg, copy.deepcopy(data), min_segments, max_segments, source_text)

    def salvage(self) -> dict | None:
        """全滅時の最終手段。落とした台本のうち最良の1本を緩い基準で通す。

        下げるのは長さの基準だけで、反復・接地（元記事に無い固有名詞や数値）・
        CTA・外国語混入の各チェックはそのまま効かせる。品質の下限は守りつつ、
        「あと数文字足りない」だけで投稿がゼロになるのを防ぐ。
        """
        if self._salvage is None:
            return None
        avg, data, min_segments, max_segments, source_text = self._salvage
        try:
            self._validate(data, min_segments=min_segments, max_segments=max_segments,
                           source_text=source_text, min_avg_chars=_SALVAGE_MIN_AVG_CHARS)
        except Exception as e:
            logger.warning(f"救済候補も緩めた基準で不合格: {e!r}")
            return None
        logger.warning(
            f"救済台本を採用: 本文平均 {avg:.1f} 字 "
            f"(通常の合格ラインは {_MIN_AVG_CHARS} 字、救済ラインは {_SALVAGE_MIN_AVG_CHARS} 字)"
        )
        return data

    def _chat_json(self, prompt: str, purpose: str = "script") -> dict:
        """JSON を返させる。日次枠が尽きているモデルは飛ばして次のモデルへ回す。

        Groq のレート上限はモデルごとに独立した枠なので、本編モデルが 429 でも
        フォールバックモデルなら通る。ここで諦めないことが投稿の欠落を直接減らす。
        """
        last_exc = None
        for model in (self.model, self.fallback_model):
            if not model:
                continue
            if _budget_left(model) <= 0:
                logger.warning(
                    f"[{purpose}] {model}: この実行のトークン予算 "
                    f"{_TOKEN_BUDGET_PER_RUN} を使い切ったので次のモデルへ"
                )
                continue

            kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}],
                      **call_kwargs(model, want_json=True)}

            try:
                response = self.client.chat.completions.create(**kwargs)
            except RateLimitError as e:
                logger.warning(f"[{purpose}] {model}: レート上限に到達、次のモデルへ — {e}")
                last_exc = e
                continue
            except NotFoundError as e:
                # モデルの廃止。Groq は llama-3.3-70b-versatile を予告なく落とし、
                # ここで諦めていたせいで 2026-08-17〜08-19 の 5 回が全滅した
                # （フォールバックは生きていたのに一度も呼ばれなかった）。
                logger.error(
                    f"[{purpose}] {model}: モデルが存在しない（廃止か権限なし）、"
                    f"次のモデルへ — {e}"
                )
                last_exc = e
                continue

            usage = getattr(response, "usage", None)
            if usage:
                _spend_tokens(model, usage.total_tokens)
                logger.info(
                    f"[{purpose}] Groq tokens ({model}): prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens} total={usage.total_tokens} "
                    f"| この実行の残り予算 {_budget_left(model)}"
                )

            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                # 推論モデルが content を空で返すのは推論に completion 上限を
                # 使い切ったとき。reasoning_effort=low で防いでいるが、念のため。
                logger.warning(
                    f"[{purpose}] {model}: content が空 "
                    f"(finish_reason={response.choices[0].finish_reason}) — 次のモデルへ"
                )
                last_exc = ValueError(f"{model} returned empty content")
                continue

            try:
                return _parse_json_lenient(raw)
            except json.JSONDecodeError as e:
                logger.error(f"[{purpose}] {model}: JSON parse 失敗: {e}\nRaw:\n{raw[:500]}")
                last_exc = e
                continue

        if last_exc:
            raise last_exc
        raise TokenBudgetExhausted(
            f"[{purpose}] 全モデルがこの実行のトークン予算 {_TOKEN_BUDGET_PER_RUN} を使い切った"
        )

    def _expand_segments(self, data: dict, source_text: str) -> dict:
        """短い段落を作り直さずに膨らませる。

        丸ごと再生成は 6,000〜7,000 トークンを使ったうえで同じ長さが返るだけだが、
        リペアなら本文ぶん（実測 2,800 トークン）で済み、実測で平均 115→126 字。
        """
        segments = data.get("script_segments", [])
        texts = [s.get("text", "") for s in segments]
        if not texts:
            raise ScriptTooShort("script_segments が空でリペアできない")

        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        rep = self._chat_json(
            _EXPAND_PROMPT.format(n=len(texts), source=source_text, current=numbered),
            purpose="expand",
        )
        new_texts = rep.get("segments") or []
        if not new_texts:
            raise ScriptTooShort("リペアが段落を1つも返さなかった")

        # 段落数がズレても丸ごと捨てない。捨てると 1 記事ぶんのトークンが無駄に
        # なったうえで投稿ごと消える（実測 08-03: 9→10 と 11→10 の 2 件が
        # これで全滅）。モデルは末尾を落としたり 2 段落を統合したりするが、
        # 先頭からの対応は概ね保たれるので、対応が取れたペアだけ差し替える。
        misaligned = len(new_texts) != len(texts)
        if misaligned:
            logger.warning(
                f"リペアが段落数を変えた（{len(texts)} → {len(new_texts)}）— "
                f"元の段落と内容が対応するものだけ差し替える"
            )

        # 「今より短くしない」はプロンプトの指示に任せず機械的に保証する。
        grown = 0
        for seg, new in zip(segments, new_texts):
            new  = (new or "").strip()
            orig = seg.get("text", "")
            if len(new) <= len(orig):
                continue
            # 段落数が合っているときは実績どおり無条件に採用する。ズレている
            # ときだけ、別の段落を掴んでいないかを内容の重なりで確かめる。
            if misaligned and not _keeps_topic(orig, new):
                continue
            seg["text"] = new
            grown += 1
        body = [s.get("text", "") for s in segments][1:] or [s.get("text", "") for s in segments]
        logger.info(
            f"リペア完了: {grown}/{len(segments)} 段落を伸長、"
            f"本文平均 {sum(len(t) for t in body) / max(len(body), 1):.0f} 字"
        )
        return data

    def _research_topic(self, original_title: str) -> str:
        """Step 1: Generate a detailed research summary for the topic (plain text)."""
        prompt = _RESEARCH_PROMPT.format(original_title=original_title)
        kwargs = {"model": self.research_model,
                  "messages": [{"role": "user", "content": prompt}],
                  **call_kwargs(self.research_model)}
        response = self.client.chat.completions.create(**kwargs)
        text = (response.choices[0].message.content or "").strip()
        usage = getattr(response, "usage", None)
        if usage:
            _spend_tokens(self.research_model, usage.total_tokens)
        logger.info(
            f"Research summary generated ({len(text)} chars) via {self.research_model}"
            + (f", {usage.total_tokens} tokens" if usage else "")
            + f" for: {original_title}"
        )
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

        # 文の途中で切らない（機械的な [:300] だと語中で切れて意味が壊れる）
        summary_excerpt = research_body[:600]
        if "。" in summary_excerpt:
            summary_excerpt = summary_excerpt.rsplit("。", 1)[0] + "。"

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

    def _validate(self, data: dict, min_segments: int = 12,
                  max_segments: int | None = None, source_text: str = "",
                  min_avg_chars: int = _MIN_AVG_CHARS):
        required = ["title", "description", "tags", "script_segments", "thumbnail_title", "image_search_keywords"]
        for key in required:
            if key not in data:
                raise ValueError(f"Script response missing required key: '{key}'")

        # Strip leaked English-only emphasis tags from the title (e.g. 【revolution】)
        data["title"] = _clean_title(data.get("title", ""))

        segments = data["script_segments"]
        if not segments:
            raise ValueError("script_segments is empty")

        # Reject any non-Japanese (Chinese/Korean) character leaked into on-screen
        # text. One stray simplified-Chinese char (e.g. 这) reads as a glaring
        # defect, so fail here to trigger a full regeneration via @retry.
        scan_targets = [data.get("title", ""), data.get("thumbnail_title", "")]
        scan_targets += [s.get("text", "") for s in segments]
        scan_targets += [s.get("text", "") for s in data.get("shorts_script_segments", [])]
        foreign = set()
        for t in scan_targets:
            foreign |= _find_foreign_chars(t)
        if foreign:
            raise ValueError(
                f"Non-Japanese characters detected in script (likely Chinese/Korean "
                f"leak): {sorted(foreign)}. Regenerating in pure Japanese."
            )
        # プロンプトでの要求は min_segments のまま据え置き、受け入れ判定だけ
        # 1段落ぶん緩める。要求を下げると出力もそのぶん下振れするため。
        floor = min_segments - _SEGMENT_SHORTFALL_TOLERANCE
        if len(segments) < floor:
            raise ValueError(
                f"Too few script_segments: {len(segments)} "
                f"(minimum {floor} required; {min_segments} requested)"
            )
        if len(segments) < min_segments:
            logger.warning(
                f"script_segments below the requested range: {len(segments)} < {min_segments} "
                f"— accepting within tolerance ({_SEGMENT_SHORTFALL_TOLERANCE}) instead of "
                f"regenerating, to conserve the daily token budget"
            )
        if max_segments and len(segments) > max_segments:
            logger.warning(
                f"script_segments above range: {len(segments)} > {max_segments} — trimming"
            )
            del segments[max_segments:]

        texts = [seg.get("text", "") for seg in segments]
        # ⚠ 1段落目(フック)は文字数判定から外す。
        # 「フックは1〜2文・60文字程度」とプロンプトで指示しているのに、
        # バリデータが全段落に150字以上を要求していたため、モデルは冒頭にも
        # 145字前後を書かざるを得なかった。実測の第1セグメントは約24.5秒あり、
        # 「最初の5秒で核心を言い切る」という自前のヒントと真逆の動画に
        # なっていた（平均視聴率の実測は26.7〜52.6%）。
        hook_text  = texts[0] if texts else ""
        body_texts = texts[1:] if len(texts) > 1 else texts
        total_chars = sum(len(t) for t in texts)
        avg_chars   = sum(len(t) for t in body_texts) / max(len(body_texts), 1)
        # 段落単位の下限は平均の下限に連動させる。救済時は平均ごと下げているのに
        # ここだけ固定にすると、平均を通した台本が必ずこちらで落ちる。
        short_floor = min_avg_chars - 15
        short_segs  = sum(1 for t in body_texts if len(t) < short_floor)
        if len(hook_text) > 110:
            logger.warning(
                f"Hook segment is {len(hook_text)} chars (~{len(hook_text)/6.1:.0f}s) — "
                f"viewers decide within the first few seconds; 50-70 chars is the target"
            )
        # 長さ不足は ScriptTooShort で投げる。generate 側がこれだけを捕まえて、
        # 作り直しではなく伸ばすリペアに回す。
        if avg_chars < min_avg_chars:
            raise ScriptTooShort(
                f"Average segment too short: {avg_chars:.1f} chars "
                f"(minimum {min_avg_chars}; segments after the hook must be "
                f"3-5 sentences, 120-170 chars)"
            )
        if short_segs > max(1, len(body_texts) * 0.15):
            raise ScriptTooShort(
                f"Too many short segments: {short_segs}/{len(body_texts)} under "
                f"{short_floor} chars. Segments after the hook must be 120-170 "
                f"chars (3-5 sentences)."
            )

        # ── 反復チェック ─────────────────────────────────────────────────
        # 本番動画では同じ文が3回流れ、5分20秒のうち約50秒が実質再放送だった。
        all_sents = [s for t in texts for s in _sentences(t)]
        if all_sents:
            uniq_ratio = len(set(all_sents)) / len(all_sents)
            if uniq_ratio < 0.85:
                raise ValueError(
                    f"Repetitive script: only {uniq_ratio:.0%} of sentences are unique "
                    f"({len(all_sents) - len(set(all_sents))} duplicated). Rewrite without repeating."
                )

        # ── CTA の後ろに中身のない段落を積んでいないか ────────────────────
        cta_at = next((i for i, t in enumerate(texts) if _CTA_RE.search(t)), None)
        if cta_at is not None and cta_at < len(texts) - 1:
            trailing = texts[cta_at + 1:]
            if any(_FILLER_RE.search(t) or len(t) < 60 for t in trailing):
                raise ValueError(
                    "Filler segments after the CTA — the CTA must be the last segment."
                )

        # ── 接地チェック（元記事に無い固有名詞・数値を作っていないか）───────
        if source_text:
            body = "".join(texts)
            src_places = set(_PLACE_RE.findall(source_text))
            leaked = {p for p in _PLACE_RE.findall(body) if p not in src_places}
            if leaked:
                raise ValueError(
                    f"Hallucinated place names not present in the source article: "
                    f"{sorted(leaked)}. Rewrite using only facts from the article."
                )
            src_nums = set(_NUMBER_RE.findall(source_text))
            new_nums = [n for n in _NUMBER_RE.findall(body) if n not in src_nums]
            if len(new_nums) >= 4:
                raise ValueError(
                    f"Too many invented figures not present in the source article: "
                    f"{new_nums[:6]}. Use only figures from the article."
                )

        # ── タイトルと本編の整合 ────────────────────────────────────────
        # タイトルが約束したことを本編で回収していないと、サムネの答えが動画内に
        # 無い状態になる（本番では「酷暑日」のタイトルで後半3分が別ニュースだった）。
        core = re.sub(r"[【\[［].*?[】\]］]", "", data.get("title", ""))
        key_terms = [w for w in re.findall(r"[一-龥ァ-ヶA-Za-z0-9]{2,}", core)]
        whole = "".join(texts)
        if key_terms and not any(k in whole for k in key_terms):
            raise ValueError(
                f"Title '{data['title']}' does not appear anywhere in the script — "
                f"the video must deliver what the title promises."
            )
        if key_terms and not any(k in "".join(texts[:5]) for k in key_terms):
            logger.warning(
                f"Title '{data['title']}' is not addressed in the first 5 segments — "
                f"viewers may leave before the payoff"
            )

        # ── サムネイル文言（体裁なので再生成せず自動補正）──────────────
        tt = (data.get("thumbnail_title") or "").strip()
        tt_len = len(re.sub(r"[「」\[\]［］]", "", tt))
        if not (6 <= tt_len <= 16):
            fixed = _clean_title(data.get("title", ""))
            fixed = re.sub(r"[【\[［].*?[】\]］]", "", fixed).strip()[:16]
            logger.warning(
                f"thumbnail_title was {tt_len} chars ({tt!r}) — a 3-character thumbnail "
                f"gives no reason to click; falling back to {fixed!r}"
            )
            data["thumbnail_title"] = fixed or tt

        # ── タグ（体裁なので自動補正）──────────────────────────────────
        tags = [str(t).strip()[:30] for t in data.get("tags", []) if str(t).strip()]
        seen_tags, kept, total_len = set(), [], 0
        for t in tags:
            if t in seen_tags or total_len + len(t) + 1 > 480:
                continue
            seen_tags.add(t)
            kept.append(t)
            total_len += len(t) + 1
        for kw in (s.get("keyword") for s in segments):
            if len(kept) >= 8:
                break
            kw = (kw or "").strip()[:30]
            if kw and kw not in seen_tags:
                seen_tags.add(kw)
                kept.append(kw)
        if len(kept) < 4:
            logger.warning(f"Only {len(kept)} usable tags after cleanup")
        data["tags"] = kept
        valid_vtypes = {"intro", "point", "keyword", "detail", "image"}
        valid_anims  = {
            "fade", "scale_in", "scale_intro", "pop", "bounce",
            "blur_in", "glow_in", "spin_in",
            "slide_up", "slide_down", "slide_left", "slide_right",
            "float_up", "snap",
        }
        used_keywords: set[str] = set()
        for seg in segments:
            if "text" not in seg:
                raise ValueError("script_segment missing 'text' field")
            # Keyword: drop content-free generic words and cross-section duplicates
            # so the center graphic never repeats the same abstract term. Blanked
            # keywords fall back to the neutral section graphic in slide_gen.
            kw = (seg.get("keyword") or "").strip()
            if kw and (kw in _BANNED_KEYWORDS or kw in used_keywords):
                # 空文字にするだけだと slide_gen が情報量ゼロのセクション板に
                # 落ちる。本文から具体語を拾い直す。
                kw = _keyword_from_text(seg.get("text", ""), used_keywords)
            seg["keyword"] = kw
            if kw:
                used_keywords.add(kw)
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
        # 実測の読み上げ速度は 5.9〜6.6 文字/秒（VOICEVOX speed 1.1）。以前は
        # 9 文字/秒で計算していたため、実際は5.3分の台本を「3.5分」と出力し、
        # 「まだ短い」という誤った圧力をかけ続けていた。
        logger.info(
            f"Script: {len(segments)} segments, total {total_chars} chars, "
            f"avg {avg_chars:.0f} chars/seg, est. {total_chars/6.1/60:.1f} min"
        )

        shorts_segs = data.get("shorts_script_segments", [])
        if not shorts_segs:
            logger.warning("shorts_script_segments missing — Shorts will use truncated main audio")
        else:
            _shorts_valid_vtypes = {"intro", "keyword", "point", "detail", "image"}
            used_shorts_keywords: set[str] = set()
            for i, sseg in enumerate(shorts_segs):
                if "text" not in sseg:
                    raise ValueError("shorts_script_segment missing 'text' field")
                # Same keyword hygiene as the main video (dedupe + drop generics)
                skw = (sseg.get("keyword") or "").strip()
                if skw and (skw in _BANNED_KEYWORDS or skw in used_shorts_keywords):
                    skw = _keyword_from_text(sseg.get("text", ""), used_shorts_keywords)
                sseg["keyword"] = skw
                if skw:
                    used_shorts_keywords.add(skw)
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
                # 第1セグメントがタイトルの読み上げになっていたら shorts_hook で
                # 置き換える。Shorts は最初の1秒でスワイプされる。
                hook = (data.get("shorts_hook") or "").strip()
                first = (shorts_segs[0].get("text") or "").strip()
                title_core = re.sub(r"[【\[［].*?[】\]］]", "", data.get("title", "")).strip()
                if hook and title_core and title_core[:10] and title_core[:10] in first:
                    logger.info("Shorts opener was the title — replacing with shorts_hook")
                    shorts_segs[0]["text"] = hook
            # 末尾の定型CTAは Shorts のループを断ち切るので落とす（説明欄で行う）
            while len(shorts_segs) > 5 and (
                _CTA_RE.search(shorts_segs[-1].get("text", ""))
                or _FILLER_RE.search(shorts_segs[-1].get("text", ""))
            ):
                dropped = shorts_segs.pop()
                logger.info(f"Dropped trailing Shorts CTA segment: {dropped.get('text','')[:30]}")
            if len(shorts_segs) < 5:
                raise ValueError(
                    f"shorts_script_segments too short: {len(shorts_segs)} segments "
                    f"(minimum 5 required). Retrying..."
                )
            logger.info(f"Shorts script: {len(shorts_segs)} segments")
