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
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

try:
    # โหลดค่าจากไฟล์ .env เมื่อรันบนเครื่องตัวเอง (ไม่มีก็ข้ามไป)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from telegram import ReplyParameters, Update
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
    "เคยส่งข้อความเดียวกันนี้ไปแล้วเมื่อ {ago} ที่แล้ว (เวลา {time})\n"
    "โปรดตรวจสอบก่อนทำรายการซ้ำ 🔁",
)

# สเต็ป 2: ข้อความที่บอทจะ reply ไปที่ "ข้อความก่อนหน้า" (ครั้งล่าสุดที่ส่งก่อนอันนี้)
# ตั้งเป็นค่าว่างเพื่อปิดสเต็ป 2 (ให้เหลือแค่แจ้งเตือน)
ORIGINAL_QUOTE_TEXT = os.environ.get(
    "ORIGINAL_QUOTE_TEXT",
    "☝️ <b>นี่คือข้อความก่อนหน้าที่เหมือนกัน</b> (ส่งเมื่อ {time})",
)

# ตำแหน่งไฟล์ฐานข้อมูล — ถ้าต่อ Volume ของ Railway ไว้ที่ /data จะเก็บถาวร
DB_PATH = os.environ.get("DB_PATH", "/data/dup_bot.db")

# โซนเวลาสำหรับแสดงผล (เวลาไทย) — เซิร์ฟเวอร์รันเป็น UTC จึงต้องแปลงก่อนแสดง
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Bangkok"))

# ตรวจ "ถอนต่างสกุลเงิน": ถ้าข้อความมีชื่อธนาคารเกาหลี (ในลิสต์) และมีคำว่า "บาท" พร้อมกัน
# = สกุลเงินผิด (ธนาคารเกาหลีต้องเป็นวอน) -> เตือน
# รายชื่อธนาคารเกาหลี (คั่นด้วยจุลภาค) — ใส่คำเด่น ๆ ของชื่อธนาคารก็พอ ไม่ต้องมีคำว่า Bank
KOREAN_BANKS = [
    b.strip()
    for b in os.environ.get(
        "KOREAN_BANKS",
        "Hana,Woori,Kookmin,Shinhan,IBK,Jeonbuk,NH,Kyongnam,Gyongnam,"
        "Kwangju,KFCC,KB,BNK,Jeju",
    ).split(",")
    if b.strip()
]

# คำบอกสกุลเงินที่ "ผิด" สำหรับธนาคารเกาหลี (คั่นด้วยจุลภาค)
CURRENCY_WORDS = [
    w.strip()
    for w in os.environ.get("CURRENCY_WORDS", "บาท,THB").split(",")
    if w.strip()
]

# ข้อความเตือนเมื่อเจอถอนต่างสกุลเงิน ({bank} = ชื่อธนาคารที่เจอ)
CURRENCY_ALERT_TEXT = os.environ.get(
    "CURRENCY_ALERT_TEXT",
    "🚨 <b>เตือน: ถอนต่างสกุลเงิน!</b>\n"
    "สลิปนี้เป็นธนาคารเกาหลี ({bank}) แต่ระบุเป็น «บาท» — ปกติต้องเป็นวอน (KRW)\n"
    "โปรดตรวจสอบก่อนถอน 💱",
)

# regex จับชื่อธนาคารเกาหลีแบบคำเต็ม (กันไปตรงกับตัวอักษรในโค้ดสุ่ม)
_KOREAN_BANK_RE = re.compile(
    r"\b(" + "|".join(re.escape(b) for b in KOREAN_BANKS) + r")\b",
    re.IGNORECASE,
) if KOREAN_BANKS else None

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


def currency_mismatch_bank(text: str) -> str | None:
    """
    ถ้าข้อความมีทั้ง 'ธนาคารเกาหลี' และ 'คำว่าบาท' พร้อมกัน = ถอนต่างสกุลเงิน
    คืนชื่อธนาคารที่เจอ / None ถ้าไม่เข้าเงื่อนไข
    """
    if _KOREAN_BANK_RE is None:
        return None
    low = text.lower()
    if not any(w.lower() in low for w in CURRENCY_WORDS):
        return None
    m = _KOREAN_BANK_RE.search(text)
    return m.group(1) if m else None


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


def check_and_record(
    chat_id: int, text: str, msg_id: int, now: float
) -> tuple[float, int | None] | None:
    """
    เทียบข้อความกับทุกข้อความในย้อนหลัง (ในช่วงเวลา) แล้วบันทึกข้อความนี้ลงฐานข้อมูล
    คืนค่า: (เวลาครั้งก่อนหน้า, id ข้อความครั้งก่อนหน้า) ถ้าซ้ำ / None ถ้าไม่ซ้ำ
    ครั้งก่อนหน้าอยู่ในกรอบเวลา (24 ชม.) เสมอ
    """
    window = DUP_WINDOW_MINUTES * 60
    cutoff = now - window
    h = make_hash(text)
    db = get_db()

    # 1) ลบข้อความที่เก่ากว่าช่วงเวลาออก (นับย้อนหลังจาก "ตอนนี้")
    db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))

    # 2) ดึง "ครั้งก่อนหน้า" (ts + msg_id ล่าสุดที่เก็บไว้) — ที่เหลืออยู่ = อยู่ในกรอบ 24 ชม. เสมอ
    #    (เพราะข้อ 1 ลบตัวที่เกินกรอบทิ้งไปแล้ว) จึงการันตีว่าเวลาในตัวเตือนไม่เกิน 24 ชม.
    row = db.execute(
        "SELECT ts, msg_id FROM messages WHERE chat_id = ? AND hash = ?",
        (chat_id, h),
    ).fetchone()

    result = (row[0], row[1]) if row is not None else None

    # 3) บันทึกข้อความนี้เป็น "ครั้งล่าสุด" (อัปเดตทั้งเวลาและ msg_id เพื่อให้ครั้งหน้าอ้างอิงอันนี้)
    db.execute(
        "INSERT INTO messages (chat_id, hash, ts, first_ts, msg_id) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, hash) DO UPDATE SET ts = excluded.ts, msg_id = excluded.msg_id",
        (chat_id, h, now, now, msg_id),
    )
    db.commit()
    return result


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

    # เตือน "ถอนต่างสกุลเงิน" — ธนาคารเกาหลี + คำว่าบาท พร้อมกัน (ตรวจแยกจากการตรวจซ้ำ)
    bank = currency_mismatch_bank(text)
    if bank is not None:
        logger.info("พบถอนต่างสกุลเงิน (ธนาคาร %r + บาท): %r", bank, text[:80])
        try:
            await message.reply_text(
                CURRENCY_ALERT_TEXT.format(bank=bank),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("ส่งเตือนสกุลเงินไม่สำเร็จ: %s", e)

    if len(text) < MIN_LENGTH:
        return

    # ข้ามข้อความที่เป็นตัวเลขล้วน (เช่น เลขบัญชี) — ไม่ตรวจซ้ำ
    # เพราะลูกค้าคนเดียวถอนหลายครั้งต่อวันได้ ต้องส่งเลขบัญชีซ้ำเป็นเรื่องปกติ
    if is_number_only(text):
        logger.info("ข้ามข้อความตัวเลขล้วน (ไม่ตรวจซ้ำ): %r", text[:80])
        return

    chat_id = message.chat_id
    now = time.time()

    result = check_and_record(chat_id, text, message.message_id, now)

    if result is not None:
        prev_ts, prev_msg_id = result
        ago = now - prev_ts
        logger.info("พบข้อความซ้ำในกลุ่ม %s (ห่างครั้งก่อน %.0f วิ): %r", chat_id, ago, text[:80])

        # สเต็ป 1: แจ้งเตือน (reply ไปที่ข้อความซ้ำอันใหม่)
        try:
            await message.reply_text(
                WARNING_TEXT.format(ago=human_ago(ago), time=format_clock(prev_ts)),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # กันบอทล่มถ้าตอบไม่ได้
            logger.warning("ตอบข้อความเตือนไม่สำเร็จ: %s", e)

        # สเต็ป 2: reply ไปที่ "ข้อความก่อนหน้า" (ครั้งล่าสุด) เพื่อให้กดแล้วเด้งไปดูได้
        if ORIGINAL_QUOTE_TEXT and prev_msg_id:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=ORIGINAL_QUOTE_TEXT.format(time=format_clock(prev_ts)),
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=prev_msg_id,
                        allow_sending_without_reply=True,
                    ),
                )
            except Exception as e:
                logger.warning("reply ข้อความก่อนหน้าไม่สำเร็จ: %s", e)
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
