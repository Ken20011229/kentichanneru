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

# If no BGM matches the target mood, fall back to these in order
_MOOD_FALLBACK: dict[str, list[str]] = {
    "upbeat":  ["upbeat", "epic", "neutral", "calm"],
    "tense":   ["tense", "neutral", "calm"],
    "epic":    ["epic", "upbeat", "neutral", "calm"],
    "tech":    ["tech", "neutral", "calm"],
    "neutral": ["neutral", "calm"],
    "calm":    ["calm", "neutral"],
}


def mood_for_channel(channel_id: str) -> str:
    return _CHANNEL_MOOD.get(channel_id, "neutral")


def filter_by_mood(bgm_files: list[str], mood: str) -> list[str]:
    """Narrow bgm_files down to those matching mood, falling back to related
    moods if nothing matches. Returns bgm_files unfiltered if nothing matches
    even after fallback (e.g. untagged files)."""
    for candidate_mood in _MOOD_FALLBACK.get(mood, [mood, "neutral", "calm"]):
        matched = [
            f for f in bgm_files
            if _BGM_MOOD.get(os.path.basename(f)) == candidate_mood
        ]
        if matched:
            return matched
    return bgm_files
