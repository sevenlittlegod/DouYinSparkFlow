"""Write only task settings to .env; never export credentials to later step logs."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.dotenv_writer import write_config

ALLOWED_SETTINGS = {
    "TASKS", "MESSAGE_TEMPLATE", "HITOKOTO_TYPES", "BROWSER_TIMEOUT",
    "FRIEND_LIST_WAIT_TIME", "TASK_RETRY_TIMES", "LOG_LEVEL",
}


def main():
    selected = {}
    for source in ("VARS_JSON", "SECRETS_JSON"):
        try:
            values = json.loads(os.environ.get(source, "{}"))
        except json.JSONDecodeError:
            raise SystemExit(f"{source} must be a JSON object") from None
        if not isinstance(values, dict):
            raise SystemExit(f"{source} must be a JSON object")
        for key, value in values.items():
            if key not in ALLOWED_SETTINGS and not key.startswith("COOKIES_"):
                continue
            if not key.replace("_", "").isalnum():
                raise SystemExit("Invalid configuration key")
            selected[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    write_config(Path(".env"), selected)
    print("Task configuration prepared. Credential values were not printed.")


if __name__ == "__main__":
    main()
