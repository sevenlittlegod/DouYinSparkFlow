"""Import a browser Cookie-Editor JSON export into the mounted configuration."""
import argparse
import json
import os
from pathlib import Path
from dotenv import dotenv_values
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.dotenv_writer import set_config_value


def main():
    parser = argparse.ArgumentParser(description="Import Cookie JSON without displaying credential values")
    parser.add_argument("cookie_file", type=Path)
    parser.add_argument("--config", type=Path, default=Path("/config/.env"))
    parser.add_argument("--account", help="TASKS unique_id; optional for one account")
    args = parser.parse_args()
    values = dotenv_values(args.config, interpolate=False)
    tasks = json.loads(values.get("TASKS", "[]"))
    candidates = [t for t in tasks if not args.account or t.get("unique_id") == args.account]
    if len(candidates) != 1:
        raise SystemExit("Select exactly one configured account using --account")
    try:
        cookies = json.loads(args.cookie_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raise SystemExit("Cannot read Cookie JSON. Export in JSON array format.") from None
    if not isinstance(cookies, list) or not cookies:
        raise SystemExit("Cookie JSON must be a non-empty array")
    if not all(isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)
               and (c.get("url") or (c.get("domain") and c.get("path"))) for c in cookies):
        raise SystemExit("Cookie JSON has invalid fields")
    key = "COOKIES_" + str(candidates[0]["unique_id"]).upper()
    set_config_value(args.config, key, json.dumps(cookies, ensure_ascii=True, separators=(",", ":")))
    os.chmod(args.config, 0o600)
    print("Cookies imported into the local configuration. No message has been sent.")


if __name__ == "__main__":
    main()
