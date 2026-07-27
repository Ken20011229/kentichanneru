import asyncio
import logging
import os
import subprocess
import wave
from pathlib import Path

import edge_tts

from src.tts.reading import to_speech

logger = logging.getLogger(__name__)


class EdgeTTSClient:
    def __init__(self, config: dict, ffmpeg: str = "ffmpeg"):
        self.voice = config.get("voice", "ja-JP-NanamiNeural")
        self.rate = config.get("rate", "+10%")
        self.ffmpeg = ffmpeg

    def synthesize(self, text: str, output_path: str) -> str:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        mp3_path = output_path + ".tmp.mp3"
        asyncio.run(self._async_synthesize(text, mp3_path))
        subprocess.run(
            [self.ffmpeg, "-y", "-i", mp3_path, output_path],
            check=True, capture_output=True,
        )
        os.remove(mp3_path)
        return output_path

    async def _async_synthesize(self, text: str, output_path: str):
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
        await communicate.save(output_path)

    def _get_wav_duration(self, path: str) -> float:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())

    def synthesize_segments(self, segments: list[dict], output_dir: str) -> list[dict]:
        os.makedirs(output_dir, exist_ok=True)
        results = []
        for i, seg in enumerate(segments):
            out_path = os.path.join(output_dir, f"seg_{i:03d}.wav")
            logger.debug(f"Synthesizing segment {i}: {seg['text'][:40]}...")
            # 音声だけ読み上げ用に正規化する（字幕は原文のまま）
            self.synthesize(to_speech(seg["text"]), out_path)
            duration = self._get_wav_duration(out_path)
            results.append({**seg, "audio_path": out_path, "duration_sec": duration})
            logger.debug(f"  -> {duration:.2f}s")
        total = sum(r["duration_sec"] for r in results)
        logger.info(f"TTS complete: {len(results)} segments, total {total:.1f}s")
        return results
