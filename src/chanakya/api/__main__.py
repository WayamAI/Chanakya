"""python -m chanakya.api [--host 127.0.0.1] [--port 8010]"""
import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env", override=False)

from chanakya.api.app import create_app  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.getenv("CHANAKYA_API_PORT", "8010")))
    a = ap.parse_args()
    uvicorn.run(create_app(), host=a.host, port=a.port)
