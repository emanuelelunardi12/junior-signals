"""Central settings, read from environment variables (.env in development)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_FILE_FOUND = ENV_FILE.exists()
load_dotenv(ENV_FILE)


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.split(",") if x.strip()}


def _prices(raw: str) -> dict[str, tuple[float, float]]:
    """Parse 'model=in/out,model=in/out' into {model: (in_per_mtok, out_per_mtok)}."""
    out = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        model, pair = part.split("=", 1)
        i, o = pair.split("/")
        out[model.strip()] = (float(i), float(o))
    return out


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-secret")
_DEV_LOGIN_SET = os.getenv("DEV_LOGIN", "") != ""
DEV_LOGIN = os.getenv("DEV_LOGIN", "0") == "1"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL_CLASSIFY = os.getenv("MODEL_CLASSIFY", "claude-haiku-4-5-20251001")
MODEL_EXTRACT = os.getenv("MODEL_EXTRACT", "claude-sonnet-5")
MODEL_GRADE = os.getenv("MODEL_GRADE", "claude-sonnet-5")
MODEL_PRICES = _prices(os.getenv("MODEL_PRICES", ""))

PRICE_SOURCE = os.getenv("PRICE_SOURCE", "none")          # none | stooq
RUN_SCHEDULER = os.getenv("RUN_SCHEDULER", "0") == "1"
PIPELINE_INTERVAL_HOURS = float(os.getenv("PIPELINE_INTERVAL_HOURS", "0.5"))
DIGEST_DAY = int(os.getenv("DIGEST_DAY", "0"))            # 0 = Monday
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))

_LOCAL = BASE_URL.startswith(("http://localhost", "http://127.0.0.1"))
if not _DEV_LOGIN_SET and _LOCAL and not TELEGRAM_BOT_TOKEN:
    # No settings file and nothing configured: this can only be a local machine, so allow the test login
    # rather than showing a page nobody can get past. Any real deployment has a bot token and a real BASE_URL.
    DEV_LOGIN = True

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "JuniorSignals student project contact@example.com")
DATABASE_PATH = ROOT / os.getenv("DATABASE_PATH", "data/signals.db")
UNIVERSE_PATH = ROOT / "config" / "universe.yaml"

# Max characters of a document sent to the models (keeps cost predictable)
MAX_DOC_CHARS = 40_000


def describe() -> str:
    """One line printed at startup, so a misread .env is obvious straight away."""
    lines = [f"[config] DEV_LOGIN={'on' if DEV_LOGIN else 'off'}  bot="
             f"{TELEGRAM_BOT_USERNAME or 'not set'}  BASE_URL={BASE_URL}  db={DATABASE_PATH}"]
    if not ENV_FILE_FOUND:
        lines.append(f"[config] No .env file at {ENV_FILE}. Run: cp .env.example .env "
                     "to set your own options; the test login is on meanwhile because nothing is configured.")
    return "\n".join(lines)
