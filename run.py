"""Start ClipForge:  python run.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from backend.config import cfg  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("backend.app:app", host=cfg.HOST, port=cfg.PORT, log_level="info", timeout_keep_alive=30)
