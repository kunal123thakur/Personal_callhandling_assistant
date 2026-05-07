
import json

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

import db
from session_pool import session_pool
from config import (
    VOBIZ_AUTH_ID,
    VOBIZ_AUTH_TOKEN,
    VOBIZ_PHONE_NUMBER,
    VOBIZ_API_BASE,
    SERVER_URL,
    FORWARD_PHONE_NUMBER,
    DEBUG,
)
from session import VoBizCallSession

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# Outbound call
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/call/{phone_number:path}")
async def make_call(phone_number: str):
    """
    Initiate an outbound call via VoBiz.

    Example:
        POST https://voice-agent.miatibro.art/call/+917428587525
    """
    to_number   = phone_number.lstrip("+")
    from_number = VOBIZ_PHONE_NUMBER.lstrip("+")

    payload = {
        "from":          from_number,
        "to":            to_number,
        "answer_url":    f"https://{SERVER_URL}/answer",
        "answer_method": "POST",
        "hangup_url":    f"https://{SERVER_URL}/hangup",
        "hangup_method": "POST",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{VOBIZ_API_BASE}/Call/",
                headers={
                    "X-Auth-ID":    VOBIZ_AUTH_ID,
                    "X-Auth-Token": VOBIZ_AUTH_TOKEN,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            data = resp.json()

        if resp.status_code == 201:
            call_uuid = data.get("call_uuid", "")
            
            # 🔥 Start warming immediately — phone is ringing, use that time
            session_pool.warm_for_call(call_uuid)
            
            print(f"[VoBiz] Outbound call → +{to_number} | UUID: {call_uuid}")
            return JSONResponse({
                "status": "calling",
                "to": f"+{to_number}",
                "call_uuid": call_uuid,
            })

        return JSONResponse(
            {"status": "error", "code": resp.status_code, "detail": data},
            status_code=500,
        )

    except Exception as e:
        print(f"[VoBiz] Call failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════════════════════════════
# Answer webhook — returns Stream XML
# ══════════════════════════════════════════════════════════════════════════════

@router.api_route("/answer", methods=["GET", "POST"])
async def answer_handler(request: Request):
    """
    VoBiz calls this when the callee picks up.
    We return XML that opens a bidirectional media stream to our WebSocket.
    """
    # VoBiz sends CallUUID in POST form or GET params
    call_uuid = ""
    if request.method == "POST":
        form = await request.form()
        call_uuid = form.get("CallUUID", "")
    else:
        call_uuid = request.query_params.get("CallUUID", "")

    if call_uuid:
        # 🔥 Kick off specific warm for incoming call too
        # (head start is small but every ms counts)
        session_pool.warm_for_call(call_uuid)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream
        bidirectional="true"
        keepCallAlive="true"
        contentType="audio/x-l16;rate=16000"
        statusCallbackUrl="https://{SERVER_URL}/stream-status"
        statusCallbackMethod="POST">
        wss://{SERVER_URL}/media-stream
    </Stream>
</Response>"""
    return Response(content=xml, media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# Stream status callback (logging only)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/stream-status")
async def stream_status(request: Request):
    form = await request.form()
    print(
        f"[VoBiz] Stream event: {form.get('Event', '')} "
        f"| call={form.get('CallUUID', '')} "
        f"| stream={form.get('StreamID', '')}"
    )
    return Response(content="OK", status_code=200)


# ══════════════════════════════════════════════════════════════════════════════
# Transfer XML — returned when forwardCallTool redirects a call
# ══════════════════════════════════════════════════════════════════════════════

@router.api_route("/transfer-xml", methods=["GET", "POST"])
async def transfer_xml(request: Request):
    """
    VoBiz fetches this URL when forwardCallTool redirects a call.
    Returns <Dial> XML to connect to the department number.
    """
    dept      = request.query_params.get("dept", "sales")
    to_number = request.query_params.get("to", FORWARD_PHONE_NUMBER.lstrip("+"))
    print(f"[VoBiz] Transfer XML | dept={dept} | to=+{to_number}")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Transferring your call to {dept} department. Please hold.</Speak>
    <Dial callerId="+{VOBIZ_PHONE_NUMBER.lstrip('+')}">
        <Number>+{to_number.lstrip('+')}</Number>
    </Dial>
    <Speak>Sorry, the team is not available. We will call you back shortly.</Speak>
    <Hangup/>
</Response>"""
    return Response(content=xml, media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# Hangup callback (logging only)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/hangup")
async def hangup_callback(request: Request):
    form = await request.form()
    print(
        f"[VoBiz] Call ended "
        f"| UUID: {form.get('CallUUID', '')} "
        f"| Duration: {form.get('Duration', '')}s"
    )
    return Response(content="OK", status_code=200)


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket — VoBiz Media Stream (one per call)
# ══════════════════════════════════════════════════════════════════════════════

@router.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    """
    VoBiz connects here for bidirectional audio streaming.
    Each call gets its own VoBizCallSession instance.
    """
    await ws.accept()
    print("[VoBiz] WebSocket connected")

    session = VoBizCallSession(ws)
    try:
        await session.run()
    except Exception as e:
        print(f"[VoBiz] Session error: {e}")
        if DEBUG:
            import traceback
            traceback.print_exc()
    finally:
        print("[VoBiz] WebSocket closed")


# ══════════════════════════════════════════════════════════════════════════════
# Health check
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    return {
        "status":  "ok",
        "agent":   "Sakshi",
        "company": "EPACK Prefab",
        "server":  SERVER_URL,
    }