"""
supa.py — ส่งข้อมูลบอทขึ้น Supabase (แดชบอร์ดหลังบ้าน) แบบ fire-and-forget

ออกแบบให้ "ไม่กระทบงานเดิม" (ตรวจซ้ำ/งานเงิน):
  - ถ้าไม่ได้ตั้ง env (SUPABASE_URL / SUPABASE_SECRET_KEY) -> ปิดตัวเอง (no-op)
  - เขียนแบบไม่รอผล (fire) ไม่บล็อก handler ; error ทั้งหมดถูกกลืน (log เฉย ๆ)
  - ใช้ service/secret key ฝั่งเซิร์ฟเวอร์เท่านั้น (บายพาส RLS) — ห้ามใส่ในหน้าเว็บ
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx  # มากับ python-telegram-bot อยู่แล้ว

log = logging.getLogger("supa")

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
enabled = bool(URL and KEY)

BOT_ID = os.environ.get("SUPA_BOT_ID", "telegram-dup")
BOT_NAME = os.environ.get("SUPA_BOT_NAME", "บอทตรวจข้อความซ้ำ (Telegram)")

_client: httpx.AsyncClient | None = None


def _c() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=8.0,
            headers={
                "apikey": KEY,
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
            },
        )
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _write(table: str, row: dict, *, upsert_on: str | None = None) -> None:
    if not enabled:
        return
    prefer = "return=minimal"
    params = None
    if upsert_on:
        prefer = "resolution=merge-duplicates,return=minimal"
        params = {"on_conflict": upsert_on}
    try:
        r = await _c().post(
            f"{URL}/rest/v1/{table}", json=row, params=params,
            headers={"Prefer": prefer},
        )
        if r.status_code >= 300:
            log.warning("supa %s -> %s %s", table, r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.info("supa write %s fail: %s", table, e)


def fire(coro) -> None:
    """ยิงงานเขียนแบบไม่รอผล (ไม่บล็อก handler) — ถ้าไม่มี event loop ก็ข้าม"""
    if not enabled:
        return
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        pass


# ---------- high level ----------
async def heartbeat() -> None:
    await _write(
        "bots",
        {"id": BOT_ID, "name": BOT_NAME, "platform": "telegram",
         "status": "online", "last_seen": _now()},
        upsert_on="id",
    )


async def heartbeat_job(context) -> None:
    """เรียกเป็นระยะโดย job_queue"""
    await heartbeat()


# จำกัดการอัปเดตกลุ่ม ไม่ให้ยิงทุกข้อความ (ประหยัด request)
_group_last: dict[str, float] = {}


def touch_group(chat_id, title) -> None:
    key = str(chat_id)
    now = time.time()
    if now - _group_last.get(key, 0) < 300:  # อัปเดตทุก 5 นาที/กลุ่ม พอ
        return
    _group_last[key] = now
    fire(_write(
        "groups",
        {"platform": "telegram", "chat_id": key, "title": title,
         "bot_id": BOT_ID, "last_activity": _now()},
        upsert_on="platform,chat_id",
    ))


def log_duplicate(chat_id, kind, *, text_hash=None, text_preview=None,
                  orig_chat_id=None, orig_chat_title=None, orig_sender=None) -> None:
    fire(_write("duplicates", {
        "bot_id": BOT_ID, "platform": "telegram", "chat_id": str(chat_id),
        "kind": kind, "text_hash": text_hash, "text_preview": text_preview,
        "orig_chat_id": (str(orig_chat_id) if orig_chat_id is not None else None),
        "orig_chat_title": orig_chat_title, "orig_sender": orig_sender,
    }))


def log_event(chat_id, kind, *, actor=None, target=None, detail=None) -> None:
    fire(_write("events", {
        "bot_id": BOT_ID, "platform": "telegram", "chat_id": str(chat_id),
        "kind": kind, "actor": actor, "target": target, "detail": detail,
    }))


def log_withdrawal(chat_id, raw_text, *, parse_ok=False, amount=None,
                   currency=None, bank=None, customer_name=None) -> None:
    fire(_write("withdrawals", {
        "platform": "telegram", "chat_id": str(chat_id), "raw_text": raw_text,
        "parse_ok": parse_ok, "amount": amount, "currency": currency,
        "bank": bank, "customer_name": customer_name,
    }))
