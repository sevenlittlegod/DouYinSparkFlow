"""Write dotenv values without losing JSON escapes or expanding ${variables}."""
import os
from pathlib import Path
import re
import tempfile
from dotenv import dotenv_values


def write_config(path, values):
    path = Path(path)
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Invalid configuration key")
        if value is None:
            continue
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'\n")
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.writelines(lines)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def set_config_value(path, key, value):
    values = dict(dotenv_values(path, interpolate=False))
    values[key] = value
    write_config(path, values)
