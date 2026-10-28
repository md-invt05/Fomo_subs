"""Configuration loaded from the project .env file."""

import os
from pathlib import Path


ENV_KEYS = {
    "WORKERS", "MAX_RPM", "FOLLOW_ACCOUNT", "FOLLOW_WORKERS", "FOLLOW_RPM",
    "FOLLOW_HEADLESS", "FOLLOW_MAX_PROFILES", "FOLLOW_SOURCE", "FOLLOW_DB",
    "FOLLOW_PROFILE_DIR", "CHROME_PATH",
}


def load_env_file(path):
    """Load simple KEY=VALUE settings without overriding the process environment."""
    path = Path(path)
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path.name}:{number}: expected KEY=VALUE")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in ENV_KEYS:
            raise ValueError(f"{path.name}:{number}: unknown setting {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
