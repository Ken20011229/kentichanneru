import json
import os
from datetime import datetime, timedelta


class Deduplicator:
    def __init__(self, config: dict):
        self.store_file = config["store_file"]
        self.ttl_days = config["ttl_days"]
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.store_file):
            with open(self.store_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.store_file), exist_ok=True)
        with open(self.store_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def _prune(self):
        cutoff = (datetime.now() - timedelta(days=self.ttl_days)).isoformat()
        self._data = {k: v for k, v in self._data.items() if v > cutoff}

    def is_seen(self, item_id: str) -> bool:
        self._prune()
        return item_id in self._data

    def mark_seen(self, item_id: str):
        self._data[item_id] = datetime.now().isoformat()
        self._save()
