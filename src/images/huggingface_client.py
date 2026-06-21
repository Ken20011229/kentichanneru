import logging
import os
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

MODEL_ID = "black-forest-labs/FLUX.1-schnell"
API_URL = f"https://api-inference.huggingface.co/models/{MODEL_ID}"


class HuggingFaceImageClient:
    def __init__(self, api_token: str):
        self.headers = {"Authorization": f"Bearer {api_token}"}

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=5, max=60))
    def generate(self, prompt: str, width: int = 1280, height: int = 720) -> bytes:
        payload = {
            "inputs": prompt,
            "parameters": {"width": width, "height": height},
        }
        resp = requests.post(API_URL, headers=self.headers, json=payload, timeout=120)

        if resp.status_code == 503:
            # モデルウォームアップ中
            wait_sec = resp.json().get("estimated_time", 20)
            logger.info(f"HuggingFace model loading, waiting {wait_sec:.0f}s...")
            time.sleep(min(wait_sec, 60))
            raise RuntimeError("Model was loading, retrying")

        resp.raise_for_status()
        return resp.content

    def fetch_images_for_keywords(self, keywords: list[str], output_dir: str, total: int = 1) -> list[str]:
        os.makedirs(output_dir, exist_ok=True)
        paths = []
        for i, kw in enumerate(keywords[:total]):
            prompt = f"high quality photograph, {kw}, news thumbnail background, cinematic, 4k"
            try:
                image_bytes = self.generate(prompt)
                path = os.path.join(output_dir, f"hf_{i:02d}.jpg")
                with open(path, "wb") as f:
                    f.write(image_bytes)
                paths.append(path)
                logger.info(f"HuggingFace image generated for '{kw}': {path}")
            except Exception as e:
                logger.warning(f"HuggingFace image generation failed for '{kw}': {e}")
        return paths[:total]
