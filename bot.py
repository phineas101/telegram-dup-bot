"""
บอทตรวจข้อความซ้ำใน Telegram
- ตรวจข้อความที่ "เหมือนกันเป๊ะ" ในกลุ่มเดียวกัน
- เทียบข้อความใหม่กับ "ทุกข้อความในย้อนหลัง N นาที" (ไม่สนลำดับ มีอะไรมาคั่นกลางก็จับได้)
- เก็บประวัติลงไฟล์ฐานข้อมูล SQLite -> restart/deploy กี่ครั้งก็ไม่ลืม
- ถ้าเจอซ้ำ จะตอบเตือนในกลุ่ม (reply ไปที่ข้อความซ้ำ)

ออกแบบไว้สำหรับงานส่งถอนเงินให้ลูกค้า ป้องกันการส่งซ้ำที่ทำให้เสียเงิน
"""

import datetime
import hashlib
import logging
import os
import sqlite3
import time
from zoneinfo import ZoneInfo

try:
    # โหลดค่าจากไฟล์ .env เมื่อรันบนเครื่องตัวเอง (ไม่มีก็ข้ามไป)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------- การตั้งค่า (อ่านจาก Environment Variables) ----------

# โทเคนของบอท (ได้จาก @BotFather) — ต้องตั้งค่าเสมอ
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ช่วงเวลาที่ถือว่าซ้ำ (นาที) ถ้าส่งข้อความเดิมภายในช่วงนี้ = ซ้ำ
# ค่าเริ่มต้น 1440 นาที = 24 ชั่วโมง (ปลอดภัยสำหรับงานเงิน)
DUP_WINDOW_MINUTES = int(os.environ.get("DUP_WINDOW_MINUTES", "1440"))

# ข้อความที่สั้นกว่านี้จะไม่ตรวจ (กันคำทั่วไป เช่น "โอเค", "ครับ")
MIN_LENGTH = int(os.environ.get("MIN_LENGTH", "3"))

# ข้อความเตือนเมื่อเจอซ้ำ
#   {ago}  = เวลาที่ผ่านมาตั้งแต่ส่งครั้งแรก (เช่น "7 นาที")
#   {time} = เวลานาฬิกาที่ชัดเจนของข้อความแรก (เช่น "15:51 น.")
WARNING_TEXT = os.environ.get(
    "WARNING_TEXT",
    "⚠️ <b>ข้อความนี้ซ้ำ!</b>\n"
    "เคยส่งข้อความเดียวกันนี้ครั้งแรกไปแล้วเมื่อ {ago} ที่แล้ว (เวลา {time})\n"
    "โปรดตรวจสอบก่อนทำรายการซ้ำ 🔁",
)

# ตำแหน่งไฟล์ฐานข้อมูล — ถ้าต่อ Volume ของ Railway ไว้ที่ /data จะเก็บถาวร
DB_PATH = os.environ.get("DB_PATH", "/data/dup_bot.db")

# โซนเวลาสำหรับแสดงผล (เวลาไทย) — เซิร์ฟเวอร์รันเป็น UTC จึงต้องแปลงก่อนแสดง
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Bangkok"))

# ---------- ระบบ log ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# ปิด log ของ httpx/telegram ที่ระดับ INFO เพราะมันจะพิมพ์ URL ที่มีโทเคนบอทออกมา
# (กันโทเคนรั่วใน log) — เหลือไว้เฉพาะ WARNING ขึ้นไป
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------- ฐานข้อมูล (SQLite) ----------
_db: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """เปิดฐานข้อมูล (สร้างไฟล์+ตารางถ้ายังไม่มี) พร้อม fallback ถ้าเขียน /data ไม่ได้"""
    global _db, DB_PATH
    if _db is not None:
        return _db

    path = DB_PATH
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    except (OSError, sqlite3.OperationalError) as e:
        # เขียนที่ path เดิมไม่ได้ (เช่น ยังไม่ได้ต่อ Volume) -> ใช้ไฟล์ในโฟลเดอร์ปัจจุบันแทน
        logger.warning(
            "เปิดฐานข้อมูลที่ %s ไม่ได้ (%s) — ใช้ไฟล์ชั่วคราว dup_bot.db แทน "
            "(ข้อมูลจะหายเมื่อ restart ถ้ายังไม่ต่อ Volume)",
            path,
            e,
        )
        path = "dup_bot.db"
        DB_PATH = path
        conn = sqlite3.connect(path, check_same_thread=False)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            chat_id  INTEGER NOT NULL,
            hash     TEXT    NOT NULL,
            ts       REAL    NOT NULL,   -- เวลาที่ส่งครั้งล่าสุด (ใช้กับกรอบเวลา 24 ชม.)
            first_ts REAL,               -- เวลาที่ส่งครั้งแรก (ใช้แสดงในข้อความเตือน)
            msg_id   INTEGER,
            PRIMARY KEY (chat_id, hash)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts)")

    # migrate ฐานข้อมูลเดิมที่ยังไม่มีคอลัมน์ first_ts
    columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "first_ts" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN first_ts REAL")
        conn.execute("UPDATE messages SET first_ts = ts WHERE first_ts IS NULL")
    conn.commit()
    _db = conn
    logger.info("ใช้ฐานข้อมูลที่: %s", path)
    return _db


def normalize(text: str) -> str:
    """ทำให้ข้อความเป็นมาตรฐานก่อนเทียบ: ตัดช่องว่างหัวท้าย + ยุบช่องว่างซ้อน"""
    return " ".join(text.split())


# เครื่องหมายคั่นที่พบในเลขบัญชี/เลขอ้างอิง จะถูกตัดออกก่อนเช็คว่าเป็นตัวเลขล้วนไหม
_NUMBER_SEPARATORS = str.maketrans("", "", " -/.,()+฿")


def is_number_only(text: str) -> bool:
    """
    True ถ้าข้อความเป็น 'ตัวเลขล้วน' (เช่น เลขบัญชี 110492152551 หรือ 110-492-152551)
    ข้อความแบบนี้จะไม่ตรวจซ้ำ เพราะลูกค้าคนเดียวถอนหลายครั้งต่อวันได้
    ส่วนข้อความที่มีตัวหนังสือปน (ข้อความถอนเงินเต็ม ๆ) จะไม่เข้าเงื่อนไขนี้ -> ยังตรวจซ้ำปกติ
    """
    stripped = text.translate(_NUMBER_SEPARATORS)
    return stripped.isdigit()


def make_hash(text: str) -> str:
    """สร้าง hash ของข้อความเพื่อเทียบแบบเร็วและประหยัดพื้นที่"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def human_ago(seconds: float) -> str:
    """แปลงวินาทีเป็นข้อความอ่านง่ายภาษาไทย"""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} วินาที"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} นาที"
    hours = minutes // 60
    remain_min = minutes % 60
    if remain_min:
        return f"{hours} ชั่วโมง {remain_min} นาที"
    return f"{hours} ชั่วโมง"


def format_clock(ts: float) -> str:
    """แปลง timestamp เป็นเวลานาฬิกาไทยที่อ่านง่าย เช่น '15:51 น.' หรือ '24/08 15:51 น.' ถ้าคนละวัน"""
    dt = datetime.datetime.fromtimestamp(ts, TZ)
    today = datetime.datetime.now(TZ).date()
    if dt.date() == today:
        return dt.strftime("%H:%M น.")
    return dt.strftime("%d/%m %H:%M น.")


def check_and_record(chat_id: int, text: str, msg_id: int, now: float) -> float | None:
    """
    เทียบข้อความกับทุกข้อความในย้อนหลัง (ในช่วงเวลา) แล้วบันทึกข้อความนี้ลงฐานข้อมูล
    คืนค่า: เวลา (timestamp) ของข้อความ 'ครั้งแรก' ถ้าซ้ำ / None ถ้าไม่ซ้ำ
    """
    window = DUP_WINDOW_MINUTES * 60
    cutoff = now - window
    h = make_hash(text)
    db = get_db()

    # 1) ลบข้อความที่เก่ากว่าช่วงเวลาออก (นับย้อนหลังจาก "ตอนนี้")
    db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))

    # 2) หาว่าเคยส่งข้อความเดียวกันในช่วงเวลานี้ไหม (ที่เหลืออยู่ = อยู่ในช่วงเวลาแน่นอน)
    row = db.execute(
        "SELECT first_ts FROM messages WHERE chat_id = ? AND hash = ?",
        (chat_id, h),
    ).fetchone()

    first_ts = row[0] if row is not None else None

    # 3) บันทึกข้อความนี้: ถ้าซ้ำให้อัปเดตเวลาล่าสุด (คงเวลาข้อความแรกไว้)
    #    ถ้าเป็นข้อความใหม่ ให้ตั้งทั้ง first_ts และ ts เป็นตอนนี้
    db.execute(
        "INSERT INTO messages (chat_id, hash, ts, first_ts, msg_id) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, hash) DO UPDATE SET ts = excluded.ts, msg_id = excluded.msg_id",
        (chat_id, h, now, now, msg_id),
    )
    db.commit()
    return first_ts


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ตรวจทุกข้อความ (text/caption) ที่เข้ามาในกลุ่ม"""
    message = update.effective_message
    if message is None:
        return

    # ดึงเนื้อหา: รองรับทั้งข้อความปกติและแคปชั่นของรูป/ไฟล์
    raw = message.text or message.caption
    if not raw:
        return

    text = normalize(raw)
    if len(text) < MIN_LENGTH:
        return

    # ข้ามข้อความที่เป็นตัวเลขล้วน (เช่น เลขบัญชี) — ไม่ตรวจซ้ำ
    # เพราะลูกค้าคนเดียวถอนหลายครั้งต่อวันได้ ต้องส่งเลขบัญชีซ้ำเป็นเรื่องปกติ
    if is_number_only(text):
        logger.info("ข้ามข้อความตัวเลขล้วน (ไม่ตรวจซ้ำ): %r", text[:80])
        return

    chat_id = message.chat_id
    now = time.time()

    first_ts = check_and_record(chat_id, text, message.message_id, now)

    if first_ts is not None:
        ago = now - first_ts
        logger.info("พบข้อความซ้ำในกลุ่ม %s (ห่างครั้งแรก %.0f วิ): %r", chat_id, ago, text[:80])
        try:
            await message.reply_text(
                WARNING_TEXT.format(ago=human_ago(ago), time=format_clock(first_ts)),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # กันบอทล่มถ้าตอบไม่ได้
            logger.warning("ตอบข้อความเตือนไม่สำเร็จ: %s", e)
    else:
        logger.info("ข้อความใหม่ในกลุ่ม %s: %r", chat_id, text[:80])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🤖 บอทตรวจข้อความซ้ำพร้อมทำงานแล้ว!\n\n"
        "เพิ่มบอทเข้ากลุ่ม แล้วบอทจะเตือนอัตโนมัติเมื่อมีการส่งข้อความเดิมซ้ำ\n"
        f"ช่วงเวลาที่ถือว่าซ้ำ: {DUP_WINDOW_MINUTES} นาที\n\n"
        "คำสั่ง: /status ดูสถานะ"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id
    now = time.time()
    cutoff = now - DUP_WINDOW_MINUTES * 60
    db = get_db()
    db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0]
    await update.effective_message.reply_text(
        f"✅ ทำงานปกติ\n"
        f"ช่วงเวลาตรวจซ้ำ: {DUP_WINDOW_MINUTES} นาที\n"
        f"ข้อความที่จำอยู่ในกลุ่มนี้: {count} รายการ\n"
        f"ฐานข้อมูล: {DB_PATH}"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ ยังไม่ได้ตั้งค่า BOT_TOKEN — ตั้งค่าใน Environment Variables ก่อนรัน"
        )

    get_db()  # เปิด/สร้างฐานข้อมูลตั้งแต่ตอนเริ่ม เพื่อให้เห็น log ว่าเก็บที่ไหน

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    # ตรวจทุกข้อความที่ไม่ใช่คำสั่ง (รวมแคปชั่น) แต่ไม่ตรวจข้อความที่ส่งผ่านบอทอื่น
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & ~filters.VIA_BOT,
            handle_message,
        )
    )

    logger.info("บอทเริ่มทำงาน (ช่วงเวลาตรวจซ้ำ = %s นาที)", DUP_WINDOW_MINUTES)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
