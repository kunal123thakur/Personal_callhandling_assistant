
import os
import argparse

import uvicorn
from fastapi import FastAPI

import db
from config import SERVER_URL, DEBUG
from routes import router
from session_pool import session_pool


# ══════════════════════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="VoBiz AI Voice Agent — EPACK Prefab")


@app.on_event("startup")
async def startup_event():
    await db.init_db()
    session_pool.start()


@app.on_event("shutdown")
async def shutdown_event():
    await db.close_pool()
    await session_pool.stop()


app.include_router(router)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VoBiz + Nova Sonic + Cartesia Voice Agent")
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--port",  type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        os.environ["DEBUG"] = "true"

    print("=" * 60)
    print(" VoBiz AI Voice Agent — EPACK Prefab")
    print("=" * 60)
    print(f"  Server      : http://{args.host}:{args.port}")
    print(f"  Public URL  : https://{SERVER_URL}")
    print(f"  Answer URL  : https://{SERVER_URL}/answer")
    print(f"  WS stream   : wss://{SERVER_URL}/media-stream")
    print()
    print("  To make a call:")
    print(f"  POST https://{SERVER_URL}/call/+917XXXXXXXXX")
    print("=" * 60)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")