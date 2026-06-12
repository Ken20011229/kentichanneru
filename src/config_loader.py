import os
import re
import yaml


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    def replacer(match):
        key = match.group(1)
        val = os.environ.get(key)
        if val is None:
            raise EnvironmentError(f"Required environment variable '{key}' is not set. Check your .env file.")
        return val

    expanded = re.sub(r"\$\{([^}]+)\}", replacer, raw)
    return yaml.safe_load(expanded)
