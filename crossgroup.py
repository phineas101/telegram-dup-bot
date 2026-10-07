"""
crossgroup.py — ตรวจข้อความซ้ำ "ข้ามกลุ่ม" (เสริม bot.py เดิม โดยไม่กระทบการตรวจซ้ำในกลุ่มเดียว)

ไอเดีย: บอทตัวเดียวอยู่หลายกลุ่ม -> เห็นข้อความทุกกลุ่มในสตรีมเดียว
  ถ้าข้อความ (เหมือนกันเป๊ะ) เคยถูกส่งที่ "กลุ่มอื่น" มาก่อนภายในกรอบเวลา
  -> เตือนในกลุ่มที่เพิ่งโผล่ซ้ำ ว่า "เคยส่งที่กลุ่ม X โดย A แล้ว โปรดตรวจสอบ"

ออกแบบให้ "ห้ามเตือนมั่ว" (false positive ต่ำสุด) สำหรับกลุ่มทำงานเงิน:
  - ฐานข้อมูลแยกไฟล์ (ไม่ยุ่งกับ DB ตรวจซ้ำในกลุ่มเดิม 100%)
  - กันมั่วหลายชั้น: ความยาวขั้นต่ำ + ต้องมีตัวเลข(ยอด) + ข้ามเลขบัญชีล้วน
    + ข้าม VIA_BOT/คำสั่ง + จับคู่เป๊ะเท่านั้น + รายการคำที่ข้าม (CROSS_IGNORE)
  - เตือน "ครั้งเดียว" ต่อ (ข้อความ+กลุ่ม) ภายในกรอบเวลา — ไม่สแปมเตือนซ้ำ
  - ดีฟอลต์เป็น "โหมดสังเกตการณ์" (ส่งเข้าห้อง Log เงียบ ๆ) เมื่อมี LOG_CHAT_ID
    ให้ดูก่อนว่าไม่มีเตือนมั่ว แล้วค่อยตั้ง CROSS_LOG_ONLY=off เปิดเตือนในกลุ่มจริง
  - handler อยู่ group 5 (คนละ group กับ bot.py เดิมและ guard.py: -1,0,1,2,3,4)
"""

import hashlib
import html
import logging
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("crossgroup")


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "on" if default else "off").strip().lower() in {
        "1", "on", "true", "yes", "y",
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# ---------- การตั้งค่า (env) ----------
CROSS_ENABLED = _env_bool("CROSS_GROUP_ENABLED", True)         # สวิตช์
CROSS_WINDOW_MINUTES = _env_int("CROSS_WINDOW_MINUTES", 1440)  # กรอบเวลา (24 ชม.)
CROSS_MIN_LENGTH = _env_int("CROSS_MIN_LENGTH", 10)            # ข้อความสั้นกว่านี้ไม่ตรวจ
CROSS_REQUIRE_DIGITS = _env_bool("CROSS_REQUIRE_DIGITS", True) # ต้องมีตัวเลข(ยอด) ถึงตรวจ
CROSS_MIN_DIGITS = _env_int("CROSS_MIN_DIGITS", 2)             # จำนวนหลักตัวเลขขั้นต่ำ
CROSS_IGNORE = [
    w.strip().lower()
    for w in os.environ.get("CROSS_IGNORE", "").split(",")
    if w.strip()
]  # ถ้าข้อความมีคำพวกนี้ -> ข้าม (ใส่ข้อความ broadcast ประจำที่โพสต์ทุกกลุ่ม)
CROSS_DB_PATH = os.environ.get("CROSS_DB_PATH", "/data/cross_bot.db")

# ใช้ร่วมกับ guard.py: ห้อง Log ส่วนตัว (0 = ไม่มี)
LOG_CHAT_ID = _env_int("LOG_CHAT_ID", 0)
LOG_THREAD_ID = _env_int("LOG_THREAD_ID", 0)
# โหมดสังเกตการณ์: ส่งเตือนเข้าห้อง Log อย่างเดียว (เงียบในกลุ่ม) — ดีฟอลต์เปิดถ้ามี LOG_CHAT_ID
CROSS_LOG_ONLY = _env_bool("CROSS_LOG_ONLY", bool(LOG_CHAT_ID))

_TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Bangkok"))

# ---------- helper (ก๊อปจาก bot.py ให้เทียบ "เหมือนกัน" แบบเดียวกัน — ตั้งใจ self-contained) ----------
_NUMBER_SEPARATORS = str.maketrans("", "", " -/.,()+฿")


def normalize(text: str) -> str:
    """ตัดช่องว่างหัวท้าย + ยุบช่องว่างซ้อน (เหมือน bot.py)"""
    return " ".join(text.split())


def is_number_only(text: str) -> bool:
    """ตัวเลขล้วน (เช่น เลขบัญชี) -> ไม่ตรวจ (ลูกค้าคนเดียวถอนได้หลายครั้ง)"""
    return text.translate(_NUMBER_SEPARATORS).isdigit()


def make_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count_digits(text: str) -> int:
    return sum(1 for c in text if c.isdigit())


def _fmt(ts: float) -> str:
    """เวลาไทยอ่านง่าย เช่น '07/10 15:51 น.'"""
    return datetime.fromtimestamp(ts, _TZ).strftime("%d/%m %H:%M น.")


def _mention(uid, name: str) -> str:
    nm = html.escape((name or "").strip() or (str(uid) if uid else "ใครบางคน"))
    return f'<a href="tg://user?id={uid}">{nm}</a>' if uid else nm


def _msg_link(chat_id: int, msg_id):
    """ลิงก์ไปข้อความเดิม (ใช้ได้เฉพาะ supergroup -100... และต้องเป็นสมาชิกกลุ่มนั้น)"""
    s = str(chat_id)
    if s.startswith("-100") and msg_id:
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


# ---------- ฐานข้อมูล (SQLite แยกไฟล์จาก DB ตรวจซ้ำในกลุ่มเดิม) ----------
_cdb: sqlite3.Connection | None = None


def get_cross_db() -> sqlite3.Connection:
    global _cdb, CROSS_DB_PATH
    if _cdb is not None:
        return _cdb
    path = CROSS_DB_PATH
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    except (OSError, sqlite3.OperationalError) as e:
        # เขียน /data ไม่ได้ (ยังไม่ต่อ Volume) -> ใช้ไฟล์ในโฟลเดอร์ปัจจุบัน (เหมือน bot.py)
        log.warning(
            "เปิด cross DB ที่ %s ไม่ได้ (%s) — ใช้ไฟล์ cross_bot.db ชั่วคราว "
            "(ข้อมูลหายเมื่อ restart ถ้ายังไม่ต่อ Volume)",
            path, e,
        )
        path = "cross_bot.db"
        CROSS_DB_PATH = path
        conn = sqlite3.connect(path, check_same_thread=False)

    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cross_msgs (
            hash        TEXT    NOT NULL,
            chat_id     INTEGER NOT NULL,
            chat_title  TEXT,
            sender_id   INTEGER,
            sender_name TEXT,
            msg_id      INTEGER,
            ts          REAL    NOT NULL,
            PRIMARY KEY (hash, chat_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cross_ts ON cross_msgs(ts)")
    conn.commit()
    _cdb = conn
    log.info("cross-group DB: %s", path)
    return _cdb


# ---------- การแจ้งเตือน ----------
async def _emit_warning(context, message, row) -> None:
    o_chat_id, o_title, o_sid, o_sname, o_msg_id, o_ts = row
    chat = message.chat
    sender = _mention(o_sid, o_sname)
    old_link = _msg_link(o_chat_id, o_msg_id)

    base = (
        "⚠️ <b>ข้อความนี้เคยถูกส่งที่กลุ่มอื่นแล้ว!</b>\n"
        f"📍 กลุ่มต้นทาง: {html.escape(o_title or '')}\n"
        f"🙋 โดย: {sender}\n"
        f"🕐 เมื่อ {_fmt(o_ts)}"
    )
    if old_link:
        base += f'\n👉 <a href="{old_link}">กดดูข้อความเดิม</a>'
    base += "\nโปรดตรวจสอบก่อนทำรายการซ้ำ 🔁"

    # โหมดสังเกตการณ์: ส่งเข้าห้อง Log อย่างเดียว (เงียบในกลุ่มจริง)
    if CROSS_LOG_ONLY and LOG_CHAT_ID:
        new_link = _msg_link(chat.id, message.message_id)
        extra = f"\n\n— โหมดสังเกตการณ์ —\n📥 โผล่ซ้ำที่กลุ่ม: {html.escape(chat.title or '')}"
        if new_link:
            extra += f'\n👉 <a href="{new_link}">กดดูข้อความใหม่</a>'
        await _send_log(context, base + extra)
        return

    # เตือนในกลุ่มที่โผล่ซ้ำ (reply ไปที่ข้อความนั้น)
    try:
        await message.reply_text(base, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        log.warning("เตือน cross-group ในกลุ่มไม่สำเร็จ: %s", e)

    # สำเนาเข้าห้อง Log ด้วย (ถ้าตั้งไว้ และไม่ใช่ห้องเดียวกัน) — คนในกลุ่มลบไม่ได้
    if LOG_CHAT_ID and LOG_CHAT_ID != chat.id:
        new_link = _msg_link(chat.id, message.message_id)
        copy = base + f"\n\n📥 โผล่ซ้ำที่กลุ่ม: {html.escape(chat.title or '')}"
        if new_link:
            copy += f' 👉 <a href="{new_link}">ข้อความใหม่</a>'
        await _send_log(context, copy)


async def _send_log(context, text: str) -> None:
    kwargs = {"parse_mode": "HTML", "disable_web_page_preview": True}
    if LOG_THREAD_ID:
        kwargs["message_thread_id"] = LOG_THREAD_ID
    try:
        await context.bot.send_message(LOG_CHAT_ID, text, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("ส่ง log cross-group ไม่สำเร็จ: %s", e)


# ---------- handler หลัก ----------
async def on_message_cross(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CROSS_ENABLED:
        return
    message = update.effective_message
    if message is None:
        return
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return

    raw = message.text or message.caption
    if not raw:
        return
    text = normalize(raw)

    # ---- กันมั่วหลายชั้น ----
    if len(text) < CROSS_MIN_LENGTH:
        return
    if is_number_only(text):          # เลขบัญชีล้วน — ส่งซ้ำได้ปกติ
        return
    low = text.lower()
    if CROSS_IGNORE and any(w in low for w in CROSS_IGNORE):
        return
    if CROSS_REQUIRE_DIGITS and _count_digits(text) < CROSS_MIN_DIGITS:
        # ไม่มีตัวเลข(ยอด) -> น่าจะเป็นข้อความทักทาย/ประกาศ ไม่ใช่รายการเงิน
        return

    h = make_hash(text)
    now = time.time()
    cutoff = now - CROSS_WINDOW_MINUTES * 60
    db = get_cross_db()

    # ลบที่เก่ากว่ากรอบเวลาออกก่อน
    db.execute("DELETE FROM cross_msgs WHERE ts < ?", (cutoff,))

    # หา "ครั้งแรกสุดในกลุ่มอื่น" ที่ยังอยู่ในกรอบเวลา
    row = db.execute(
        "SELECT chat_id, chat_title, sender_id, sender_name, msg_id, ts "
        "FROM cross_msgs WHERE hash = ? AND chat_id != ? ORDER BY ts ASC LIMIT 1",
        (h, chat.id),
    ).fetchone()

    # เคยเห็นข้อความนี้ในกลุ่มนี้แล้วหรือยัง (ภายในกรอบเวลา — ไม่ใช่ "ตลอดกาล";
    # แถวจะถูกลบเมื่อพ้นกรอบ จึงเตือนใหม่ได้ถ้าข้อความกลับมาอีกหลังพ้นกรอบ) ใช้ตัดสินว่าเตือนไหม
    seen_here = db.execute(
        "SELECT 1 FROM cross_msgs WHERE hash = ? AND chat_id = ?", (h, chat.id)
    ).fetchone()

    # บันทึกข้อความนี้ (เก็บ "ครั้งแรก" ต่อกลุ่ม — ไม่ทับของเดิม)
    sender = message.from_user
    db.execute(
        "INSERT OR IGNORE INTO cross_msgs "
        "(hash, chat_id, chat_title, sender_id, sender_name, msg_id, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            h, chat.id, chat.title or "",
            sender.id if sender else None,
            (sender.full_name if sender else "") or "",
            message.message_id, now,
        ),
    )
    db.commit()

    # เตือนเฉพาะตอน "โผล่ครั้งแรกในกลุ่มนี้" ทั้งที่เคยมีในกลุ่มอื่น -> ไม่สแปมซ้ำ
    if row and not seen_here:
        log.info(
            "cross-group dup: hash=%s โผล่ที่ %s เคยอยู่ที่ %s",
            h[:8], chat.id, row[0],
        )
        await _emit_warning(context, message, row)


# ---------- คำสั่งดูสถานะ ----------
async def cmd_crossgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        n = get_cross_db().execute("SELECT COUNT(*) FROM cross_msgs").fetchone()[0]
    except Exception:  # noqa: BLE001
        n = "?"
    mode = (
        "สังเกตการณ์ (ส่งเข้าห้อง Log อย่างเดียว เงียบในกลุ่ม)"
        if (CROSS_LOG_ONLY and LOG_CHAT_ID)
        else "เตือนในกลุ่มจริง"
    )
    await update.effective_message.reply_text(
        "🔀 <b>สถานะตรวจซ้ำข้ามกลุ่ม</b>\n"
        f"สถานะ: {'เปิด' if CROSS_ENABLED else 'ปิด'}\n"
        f"โหมด: {mode}\n"
        f"กรอบเวลา: {CROSS_WINDOW_MINUTES} นาที\n"
        f"ความยาวขั้นต่ำ: {CROSS_MIN_LENGTH} ตัวอักษร\n"
        f"ต้องมีตัวเลข: {'ใช่' if CROSS_REQUIRE_DIGITS else 'ไม่'} (≥ {CROSS_MIN_DIGITS} หลัก)\n"
        f"คำที่ข้าม (ignore): {len(CROSS_IGNORE)} คำ\n"
        f"ข้อความที่จำข้ามกลุ่ม: {n} รายการ\n"
        f"LOG_CHAT_ID: {LOG_CHAT_ID or 'ยังไม่ตั้ง'}",
        parse_mode="HTML",
    )


# ---------- register ----------
def register(app) -> None:
    """เพิ่ม handler ของ cross-group (group 5 — ไม่ชนกับ bot.py เดิมและ guard.py)"""
    if not CROSS_ENABLED:
        log.info("CROSS_GROUP_ENABLED=off — ไม่โหลดตรวจซ้ำข้ามกลุ่ม")
        return
    get_cross_db()
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & ~filters.VIA_BOT,
            on_message_cross,
        ),
        group=5,
    )
    app.add_handler(CommandHandler("crossgroup", cmd_crossgroup))
    log.info(
        "cross-group พร้อม: window=%smin min_len=%s require_digits=%s(>=%s) log_only=%s",
        CROSS_WINDOW_MINUTES, CROSS_MIN_LENGTH, CROSS_REQUIRE_DIGITS,
        CROSS_MIN_DIGITS, CROSS_LOG_ONLY and bool(LOG_CHAT_ID),
    )
