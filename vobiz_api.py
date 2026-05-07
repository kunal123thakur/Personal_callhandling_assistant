
import httpx

from config import (
    VOBIZ_AUTH_ID,
    VOBIZ_AUTH_TOKEN,
    VOBIZ_PHONE_NUMBER,
    VOBIZ_API_BASE,
    SERVER_URL,
    FORWARD_PHONE_NUMBER,
)


async def vobiz_hangup(call_uuid: str):
    """Hang up a VoBiz call via REST API (DELETE /Call/{uuid}/)."""
    url = f"{VOBIZ_API_BASE}/Call/{call_uuid}/"
    headers = {
        "X-Auth-ID":    VOBIZ_AUTH_ID,
        "X-Auth-Token": VOBIZ_AUTH_TOKEN,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, headers=headers, timeout=10)
        print(f"[VoBiz] Hangup {call_uuid} → {resp.status_code}")
        return resp.status_code
    except Exception as e:
        print(f"[VoBiz] Hangup error: {e}")
        return None


async def vobiz_transfer(call_uuid: str, department: str):
    """
    Transfer a VoBiz call by redirecting it to the /transfer-xml endpoint,
    which returns <Dial> XML pointing to the department number.
    """
    redirect_url = (
        f"https://{SERVER_URL}/transfer-xml"
        f"?dept={department}&to={FORWARD_PHONE_NUMBER.lstrip('+')}"
    )
    url = f"{VOBIZ_API_BASE}/Call/{call_uuid}/"
    headers = {
        "X-Auth-ID":    VOBIZ_AUTH_ID,
        "X-Auth-Token": VOBIZ_AUTH_TOKEN,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"action": "redirect", "redirect_url": redirect_url},
                timeout=10,
            )
        print(f"[VoBiz] Transfer {call_uuid} → {department} | status={resp.status_code}")
        return resp.status_code
    except Exception as e:
        print(f"[VoBiz] Transfer error: {e}")
        return None