import logging
import os
import wave
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class VoicevoxClient:
    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.speaker_id = config["speaker_id"]
        self.speed_scale = config.get("speed_scale", 1.0)
        self.pitch_scale = config.get("pitch_scale", 0.0)
        self.intonation_scale = config.get("intonation_scale", 1.0)
        self.volume_scale = config.get("volume_scale", 1.0)
        self.timeout = config.get("timeout", 60)

    def check_server(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/version", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def synthesize(self, text: str, output_path: str) -> str:
        if not self.check_server():
            raise ConnectionError(
                f"VOICEVOX server is not reachable at {self.base_url}. "
                "Please start VOICEVOX Engine before running the pipeline."
            )

        query_resp = requests.post(
            f"{self.base_url}/audio_query",
            params={"text": text, "speaker": self.speaker_id},
            timeout=self.timeout,
        )
        query_resp.raise_for_status()
        query = query_resp.json()

        query["speedScale"] = self.speed_scale
        query["pitchScale"] = self.pitch_scale
        query["intonationScale"] = self.intonation_scale
        query["volumeScale"] = self.volume_scale

        synth_resp = requests.post(
            f"{self.base_url}/synthesis",
            params={"speaker": self.speaker_id},
            json=query,
            timeout=self.timeout,
        )
        synth_resp.raise_for_status()

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(synth_resp.content)
        return output_path

    def _get_wav_duration(self, path: str) -> float:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())

    def synthesize_segments(self, segments: list[dict], output_dir: str) -> list[dict]:
        os.makedirs(output_dir, exist_ok=True)
        results = []
        for i, seg in enumerate(segments):
            out_path = os.path.join(output_dir, f"seg_{i:03d}.wav")
            logger.debug(f"Synthesizing segment {i}: {seg['text'][:40]}...")
            self.synthesize(seg["text"], out_path)
            duration = self._get_wav_duration(out_path)
            results.append({**seg, "audio_path": out_path, "duration_sec": duration})
            logger.debug(f"  -> {duration:.2f}s")
        total = sum(r["duration_sec"] for r in results)
        logger.info(f"TTS complete: {len(results)} segments, total {total:.1f}s")
        return results
