
import asyncio
import datetime
import hashlib
import json
import random
import uuid

import pytz

from config import FORWARD_PHONE_NUMBER
from prompts import PRODUCTS
from vobiz_api import vobiz_hangup, vobiz_transfer   # VoBiz REST helpers


# ══════════════════════════════════════════════════════════════════════════════
# ToolProcessor
# ══════════════════════════════════════════════════════════════════════════════

class ToolProcessor:
    def __init__(self):
        self._leads = []
        self.call_uuid = ""          # VoBiz call UUID — set by VoBizCallSession after handshake
        self._end_call_event     = asyncio.Event()
        self._forward_call_event = asyncio.Event()

    # ── Public entry point ────────────────────────────────────────────────────

    async def process_tool_async(self, tool_name: str, tool_content: dict):
        return await self._run_tool(tool_name, tool_content)

    # ── Internal dispatcher ───────────────────────────────────────────────────

    async def _run_tool(self, tool_name: str, tool_content: dict):
        tool = tool_name.lower()

        content_raw = tool_content.get("content", "{}")
        try:
            params = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except Exception:
            params = {}

        # ── getDateAndTimeTool ─────────────────────────────────────────────────
        if tool == "getdateandtimetool":
            ist = pytz.timezone("Asia/Kolkata")
            now = datetime.datetime.now(ist)
            return {
                "formattedTime": now.strftime("%I:%M %p"),
                "date":          now.strftime("%Y-%m-%d"),
                "year": now.year, "month": now.month, "day": now.day,
                "dayOfWeek":     now.strftime("%A").upper(),
                "timezone":      "IST",
            }

        # # ── trackOrderTool ─────────────────────────────────────────────────────
        # if tool == "trackordertool":
        #     await asyncio.sleep(5)
        #     order_id = str(params.get("orderId", ""))
        #     notify   = params.get("requestNotifications", False)
        #     seed = int(hashlib.md5(order_id.encode(), usedforsecurity=False).hexdigest(), 16) % 10_000
        #     random.seed(seed)
        #     statuses = ["Order received", "Processing", "Preparing for shipment", "Shipped",
        #                 "In transit", "Out for delivery", "Delivered", "Delayed"]
        #     weights  = [10, 15, 15, 20, 20, 10, 5, 3]
        #     status   = random.choices(statuses, weights=weights, k=1)[0]
        #     today    = datetime.datetime.now()
        #     if status == "Delivered":
        #         est = (today - datetime.timedelta(days=random.randint(0, 3))).strftime("%Y-%m-%d")
        #     elif status == "Out for delivery":
        #         est = today.strftime("%Y-%m-%d")
        #     else:
        #         est = (today + datetime.timedelta(days=random.randint(1, 10))).strftime("%Y-%m-%d")
        #     result = {"orderStatus": status, "orderNumber": order_id}
        #     if notify and status != "Delivered":
        #         result["notificationStatus"] = f"Notifications set for order {order_id}"
        #     if status == "Delivered":
        #         result["deliveredOn"] = est
        #     elif status == "Out for delivery":
        #         result["expectedDelivery"] = "Today"
        #     else:
        #         result["estimatedDelivery"] = est
        #     return result

        # ── getProductInfoTool ─────────────────────────────────────────────────
        if tool == "getproductinfotool":
            product_key = params.get("product", "").lower()
            match = None
            for k, v in PRODUCTS.items():
                if k in product_key or product_key in k or product_key in v["name"].lower():
                    match = v
                    break
            if match:
                return {"found": True, **match}
            return {
                "found": False,
                "available_products": [v["name"] for v in PRODUCTS.values()],
                "message": "Product not found.",
            }

        # ── scheduleDemoTool ───────────────────────────────────────────────────
        if tool == "scheduledemotool":
            ist = pytz.timezone("Asia/Kolkata")
            now = datetime.datetime.now(ist)
            date = params.get("preferredDate", "")
            if not date:
                days_ahead = 1 if now.weekday() < 4 else (7 - now.weekday())
                demo_date  = (now + datetime.timedelta(days=days_ahead)).strftime("%Y-%m-%d")
            else:
                demo_date = date
            return {
                "scheduled":    True,
                "customerName": params.get("customerName", "Customer"),
                "companyName":  params.get("companyName", ""),
                "product":      params.get("product", ""),
                "demoDate":     demo_date,
                "demoTime":     "11:00 AM IST",
                "meetingLink":  f"https://meet.epack.in/demo-{str(uuid.uuid4())[:8]}",
                "message":      f"Demo scheduled for {demo_date} at 11:00 AM IST.",
            }

        # ── createLeadTool ─────────────────────────────────────────────────────
        if tool == "createleadtool":
            lead = {
                "id":           str(uuid.uuid4())[:8],
                "name":         params.get("customerName", "Unknown"),
                "company":      params.get("companyName", "Unknown"),
                "phone":        params.get("phone", ""),
                "email":        params.get("email", ""),
                "interestedIn": params.get("interestedIn", ""),
                "budget":       params.get("budget", ""),
                "notes":        params.get("notes", ""),
                "createdAt":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status":       "New Lead",
            }
            self._leads.append(lead)
            print(f"[Lead] Saved: {lead['name']} / {lead['company']} — {lead['interestedIn']}")
            return {
                "saved":    True,
                "leadId":   lead["id"],
                "message":  f"Lead saved for {lead['name']} from {lead['company']}.",
                "nextStep": "Sales team will contact within 24 hours.",
            }

        # ── forwardCallTool  (VoBiz version) ───────────────────────────────────
        if tool == "forwardcalltool":
            department = params.get("department", "").lower().strip()
            if not department:
                return {"status": "error", "message": "department parameter is required"}

            valid_depts = ["hr", "sales", "support", "manager"]
            if department not in valid_depts:
                return {"status": "error", "message": f"Unknown department '{department}'",
                        "valid_depts": valid_depts}

            # Time restriction check
            ist = pytz.timezone("Asia/Kolkata")
            now = datetime.datetime.now(ist)
            is_sunday            = now.weekday() == 6
            is_saturday_after_4  = now.weekday() == 5 and now.hour >= 16

            if is_sunday or is_saturday_after_4:
                return {
                    "status":  "transfer_blocked",
                    "message": "cannot transfer the call rightnow we have processed your requirments sales team will reach you soon",
                }

            if self.call_uuid:
                try:
                    status_code = await vobiz_transfer(self.call_uuid, department)
                    if status_code and status_code < 400:
                        self._forward_call_event.set()
                        print(f"[Forward] VoBiz transfer → {department} | {FORWARD_PHONE_NUMBER}")
                        return {
                            "status":     "forwarding",
                            "department": department,
                            "phone":      FORWARD_PHONE_NUMBER,
                            "message":    f"Call is being transferred to {department} at {FORWARD_PHONE_NUMBER}.",
                        }
                    return {"status": "error", "message": f"VoBiz transfer returned {status_code}"}
                except Exception as e:
                    print(f"[Forward] Error: {e}")
                    return {"status": "error", "message": str(e)}
            return {"status": "error", "message": "call_uuid not set — cannot transfer"}

        # ── endCallTool  (VoBiz version) ───────────────────────────────────────
        if tool == "endcalltool":
            reason = params.get("reason", "conversation_complete")
            print(f"[EndCall] Ending call — reason: {reason}")
            if self.call_uuid:
                try:
                    await asyncio.sleep(3)   # let goodbye TTS finish
                    await vobiz_hangup(self.call_uuid)
                    self._end_call_event.set()
                    return {"status": "call_ended", "reason": reason,
                            "message": "Call has been ended successfully."}
                except Exception as e:
                    print(f"[EndCall] Error: {e}")
                    return {"status": "error", "message": str(e)}
            self._end_call_event.set()
            return {"status": "call_ended", "reason": reason, "message": "End signal sent."}

        # ── Unknown tool ───────────────────────────────────────────────────────
        return {"error": f"Unknown tool: {tool_name}"}