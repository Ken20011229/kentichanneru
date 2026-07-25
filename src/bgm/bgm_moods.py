"""bgm_moods.py — Map channel genres and BGM tracks to mood tags for content-aware selection.

Mood tags for assets/bgm/*.mp3 are assigned heuristically from filename/theme
(no audio analysis). The library currently skews toward calm/ambient tracks —
add more upbeat/tense/epic tracks to assets/bgm/ for stronger differentiation.
"""
import os

# channel id -> primary mood
_CHANNEL_MOOD: dict[str, str] = {
    "news":         "tense",
    "technology":   "tech",
    "gaming":       "upbeat",
    "entertainment": "upbeat",
    "science":      "tech",
    "sports":       "epic",
    "cooking":      "calm",
    "music":        "upbeat",
    "travel":       "calm",
    "education":    "neutral",
    "movie":        "epic",
    "anime":        "upbeat",
    "pets":         "calm",
    "smartphone":   "tech",
    "pc_apps":      "tech",
    "region":       "calm",
    "saving":       "neutral",
    "senior":       "calm",
}

# BGM filename -> mood tag (heuristic, based on title/theme)
_BGM_MOOD: dict[str, str] = {
    "background.mp3":                          "neutral",
    "Hydrangea.mp3":                            "calm",
    "Lostwood_Reverie.mp3":                     "calm",
    "Oriental_Poppy.mp3":                       "calm",
    "Peaceful_rest.mp3":                        "calm",
    "Rain_Drop.mp3":                            "calm",
    "なでしこ.mp3":                              "calm",
    "ハイドランジア.mp3":                        "calm",
    "メンタルヘルス.mp3":                        "calm",
    "冒険への誘い.mp3":                          "epic",
    "無敵完璧限界凸サイコな誰彼彼女.mp3":         "upbeat",
}

# Fallback order per target mood. The current library has no genuinely "tense"
# or "tech" tracks, so energetic moods (news/tech/gaming/...) must reach for the
# epic/upbeat/neutral tracks first and only use the sleepy calm/ambient tracks as
# a genuine last resort — otherwise a news bulletin ends up over carefree music.
_MOOD_FALLBACK: dict[str, list[str]] = {
    "upbeat":  ["upbeat", "epic", "neutral", "calm"],
    "tense":   ["tense", "epic", "neutral", "calm"],
    "epic":    ["epic", "upbeat", "neutral", "calm"],
    "tech":    ["tech", "neutral", "epic", "calm"],
    "neutral": ["neutral", "epic", "calm"],
    "calm":    ["calm", "neutral"],
}


def mood_for_channel(channel_id: str) -> str:
    return _CHANNEL_MOOD.get(channel_id, "neutral")


def filter_by_mood(bgm_files: list[str], mood: str) -> list[str]:
    """Return the BGM candidates appropriate for ``mood``.

    Candidates are accumulated across the mood's fallback tiers (so a channel is
    not stuck on a single track for lack of an exact match), but for any non-calm
    target mood the ``calm`` tier is only pulled in when nothing else matched at
    all. That keeps energetic content (news, tech, gaming) off the sleepy ambient
    tracks while still preferring the closest available mood.

    Falls back to the full list if nothing is tagged."""
    tiers = _MOOD_FALLBACK.get(mood, [mood, "neutral", "calm"])
    pool: list[str] = []
    seen: set[str] = set()
    for candidate_mood in tiers:
        # Don't dilute an energetic pool with calm tracks unless it's empty.
        if candidate_mood == "calm" and mood != "calm" and pool:
            continue
        for f in bgm_files:
            if _BGM_MOOD.get(os.path.basename(f)) == candidate_mood and f not in seen:
                pool.append(f)
                seen.add(f)
    return pool or bgm_files
