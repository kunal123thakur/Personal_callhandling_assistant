
import inspect
import datetime
import os
import warnings

from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore")

# ── VoBiz credentials & routing ───────────────────────────────────────────────
VOBIZ_AUTH_ID      = os.environ["VOBIZ_AUTH_ID"]
VOBIZ_AUTH_TOKEN   = os.environ["VOBIZ_AUTH_TOKEN"]
VOBIZ_PHONE_NUMBER = os.environ["VOBIZ_PHONE_NUMBER"]   # without +, e.g. 918065481698
SERVER_URL         = os.environ["SERVER_URL"].rstrip("/")  # no trailing slash, no https://

# ── Cartesia TTS ──────────────────────────────────────────────────────────────
CARTESIA_API_KEY  = os.environ.get("CARTESIA_API_KEY", "")
CARTESIA_VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "a0e99841-438c-4a64-b679-ae501e7d6091")
CARTESIA_MODEL_ID = "sonic-2"

# ── AWS / Bedrock ─────────────────────────────────────────────────────────────
AWS_REGION       = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BEDROCK_MODEL_ID = "amazon.nova-2-sonic-v1:0"

# ── Audio sample rates ────────────────────────────────────────────────────────
# VoBiz → server  : base64(PCM 16 kHz) → Nova Sonic  (NO conversion needed)
# Cartesia → VoBiz: PCM 24 kHz → ratecv 24→16 kHz → base64 → VoBiz playAudio
NOVA_RATE     = 16_000   # Nova Sonic input: PCM 16 kHz
CARTESIA_RATE = 24_000   # Cartesia output: PCM 24 kHz
VOBIZ_RATE    = 16_000   # VoBiz stream: PCM 16 kHz (same as Nova — no conversion for input!)

# ── Misc ──────────────────────────────────────────────────────────────────────
DEBUG                = os.environ.get("DEBUG", "false").lower() == "true"
VOICE_CLONE          = os.environ.get("VOICE_CLONE", "false").lower() == "true"
FORWARD_PHONE_NUMBER = "+919220173002"
VOBIZ_API_BASE       = f"https://api.vobiz.ai/api/v1/Account/{VOBIZ_AUTH_ID}"


def debug_print(msg: str):
    """Print a timestamped debug message (only when DEBUG=true)."""
    if DEBUG:
        caller = inspect.stack()[1].function
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] [{caller}] {msg}")