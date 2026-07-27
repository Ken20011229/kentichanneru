"""読み上げ用テキストの正規化。

TTS に渡す文字列と、字幕に出す文字列を分ける。字幕は原文のまま（読みやすい）、
音声だけカタカナ読みに直す。以前は前処理が1行も無く、「200mm」「HTML」「×」
「【衝撃】」などがそのまま VOICEVOX に入って読み崩れていた。
"""

import re

# ⚠ Python の `\b` は日本語も単語文字として扱うため、「200mm以上」「AIが」の
# ような並びでは境界が成立せず一切マッチしない。ASCII の英数字だけを単語文字
# とみなす明示的な前後読みを使うこと。
_L = r"(?<![A-Za-z0-9])"   # 直前がASCII英数字でない
_R = r"(?![A-Za-z0-9])"    # 直後がASCII英数字でない

# 長い表記から先に置換する（km/h を km より先に、macOS を OS より先に）
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[【】\[\]［］『』]"), ""),
    (re.compile(r"(\d+(?:\.\d+)?)\s*km/h", re.I), r"\1キロメートル毎時"),
    (re.compile(rf"(\d+(?:\.\d+)?)\s*mm{_R}", re.I), r"\1ミリ"),
    (re.compile(rf"(\d+(?:\.\d+)?)\s*cm{_R}", re.I), r"\1センチ"),
    (re.compile(rf"(\d+(?:\.\d+)?)\s*km{_R}", re.I), r"\1キロ"),
    (re.compile(rf"(\d+(?:\.\d+)?)\s*kg{_R}", re.I), r"\1キログラム"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*℃"), r"\1度"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[%％]"), r"\1パーセント"),
    (re.compile(rf"(\d+)\s*GB{_R}", re.I), r"\1ギガバイト"),
    (re.compile(rf"(\d+)\s*TB{_R}", re.I), r"\1テラバイト"),
    (re.compile(r"×"), "かける"),
    (re.compile(r"〜"), "から"),
    (re.compile(r"macOS"), "マックオーエス"),
    (re.compile(r"iOS"), "アイオーエス"),
    (re.compile(rf"{_L}HTML{_R}"), "エイチティーエムエル"),
    (re.compile(rf"{_L}CSS{_R}"), "シーエスエス"),
    (re.compile(rf"{_L}API{_R}"), "エーピーアイ"),
    (re.compile(rf"{_L}URL{_R}"), "ユーアールエル"),
    (re.compile(rf"{_L}SNS{_R}"), "エスエヌエス"),
    (re.compile(rf"{_L}CPU{_R}"), "シーピーユー"),
    (re.compile(rf"{_L}GPU{_R}"), "ジーピーユー"),
    (re.compile(rf"{_L}PC{_R}"), "パソコン"),
    (re.compile(rf"{_L}AI{_R}"), "エーアイ"),
    (re.compile(rf"{_L}EV{_R}"), "イーブイ"),
    (re.compile(rf"{_L}JR{_R}"), "ジェイアール"),
    (re.compile(r"\s+"), " "),
]


def to_speech(text: str) -> str:
    """字幕用の原文から、読み上げ用テキストを作る。"""
    if not text:
        return ""
    out = text
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out.strip()
