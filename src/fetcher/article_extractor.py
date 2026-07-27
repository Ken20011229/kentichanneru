"""記事URLから本文を取り出す。

RSS の `summary` は NHK で実測 104〜149 文字しかない。その百数十文字から
「合計2000文字以上・12〜16段落」を書かせていたため、LLM は尺を埋めるために
元記事に存在しない県名・降水量・交通影響を創作していた（本番動画で確認）。
台本の素材は必ず本文から取る。
"""

import logging
import re

logger = logging.getLogger(__name__)

# RSS の description 末尾に付く媒体名フッター。そのまま台本に混ざると
# ナレーションが「…警戒してください。NHK NEWS」と読み上げてしまう。
_SOURCE_FOOTER = re.compile(
    r"\s*(NHK\s*NEWS(\s*WEB)?|ITmedia(\s*\w+)?|GIGAZINE|Yahoo!ニュース|"
    r"続きを読む|関連記事|\[記事全文\])\s*$",
    re.IGNORECASE,
)


def strip_source_footer(text: str) -> str:
    """媒体名フッター・HTMLタグ・過剰な空白を落とす。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    prev = None
    while prev != text:
        prev = text
        text = _SOURCE_FOOTER.sub("", text.strip())
    return re.sub(r"[ \t　]+", " ", text).strip()


def fetch_article_body(url: str, max_chars: int = 4000, timeout: int = 12) -> str:
    """記事本文を返す。取得できなければ空文字（呼び出し側で summary にフォールバック）。"""
    if not url or not url.startswith("http"):
        return ""
    try:
        import trafilatura
    except ImportError:
        logger.warning("trafilatura not installed — falling back to RSS summary only")
        return ""

    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            logger.info(f"Article body fetch returned nothing: {url}")
            return ""
        body = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        ) or ""
    except Exception as e:
        logger.warning(f"Article body extraction failed for {url}: {e}")
        return ""

    body = strip_source_footer(body)
    if len(body) < 120:
        return ""
    logger.info(f"Article body extracted: {len(body)} chars from {url}")
    return body[:max_chars]
