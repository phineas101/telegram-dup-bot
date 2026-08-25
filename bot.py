"""
บอทตรวจข้อความซ้ำใน Telegram
- ตรวจข้อความที่ "เหมือนกันเป๊ะ" ในกลุ่มเดียวกัน ภายในช่วงเวลาที่กำหนด
- ถ้าเจอซ้ำ จะตอบเตือนในกลุ่ม (reply ไปที่ข้อความซ้ำ)

ออกแบบไว้สำหรับงานส่งถอนเงินให้ลูกค้า ป้องกันการส่งซ้ำที่ทำให้เสียเงิน
"""

import hashlib
import logging
import os
import time
from collections import defaultdict

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

# ข้อความเตือนเมื่อเจอซ้ำ ({minutes_ago} จะถูกแทนด้วยเวลาที่ผ่านมา)
WARNING_TEXT = os.environ.get(
    "WARNING_TEXT",
    "⚠️ <b>ข้อความนี้ซ้ำ!</b>\nเคยส่งข้อความเดียวกันนี้มาแล้วเมื่อ {ago} ที่แล้ว\nโปรดตรวจสอบก่อนทำรายการซ้ำ 🔁",
)

# ---------- ระบบ log ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- ที่เก็บประวัติข้อความ (ในหน่วยความจำ) ----------
# โครงสร้าง: seen[chat_id][hash] = {"ts": เวลาที่ส่งล่าสุด, "msg_id": id ข้อความแรก}
seen: dict[int, dict[str, dict]] = defaultdict(dict)


def normalize(text: str) -> str:
    """ทำให้ข้อความเป็นมาตรฐานก่อนเทียบ: ตัดช่องว่างหัวท้าย + ยุบช่องว่างซ้อน"""
    return " ".join(text.split())


def make_hash(text: str) -> str:
    """สร้าง hash ของข้อความเพื่อเทียบแบบเร็วและประหยัดหน่วยความจำ"""
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


def cleanup(chat_id: int, now: float) -> None:
    """ลบข้อความเก่าที่เกินช่วงเวลาออก เพื่อไม่ให้หน่วยความจำโต"""
    window = DUP_WINDOW_MINUTES * 60
    store = seen[chat_id]
    expired = [h for h, rec in store.items() if now - rec["ts"] > window]
    for h in expired:
        del store[h]


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

    chat_id = message.chat_id
    now = time.time()
    cleanup(chat_id, now)

    h = make_hash(text)
    store = seen[chat_id]
    window = DUP_WINDOW_MINUTES * 60

    prev = store.get(h)
    if prev and (now - prev["ts"]) <= window:
        # เจอข้อความซ้ำภายในช่วงเวลา -> เตือน
        ago = human_ago(now - prev["ts"])
        logger.info("พบข้อความซ้ำในกลุ่ม %s: %r", chat_id, text[:80])
        try:
            await message.reply_text(
                WARNING_TEXT.format(ago=ago),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # กันบอทล่มถ้าตอบไม่ได้
            logger.warning("ตอบข้อความเตือนไม่สำเร็จ: %s", e)

    # อัปเดตเวลาล่าสุดเสมอ (ให้หน้าต่างเวลาเลื่อนตาม เพื่อจับซ้ำครั้งที่ 3, 4 ต่อได้)
    store[h] = {"ts": now, "msg_id": message.message_id}


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
    cleanup(chat_id, now)
    count = len(seen.get(chat_id, {}))
    await update.effective_message.reply_text(
        f"✅ ทำงานปกติ\n"
        f"ช่วงเวลาตรวจซ้ำ: {DUP_WINDOW_MINUTES} นาที\n"
        f"ข้อความที่จำอยู่ในกลุ่มนี้: {count} รายการ"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ ยังไม่ได้ตั้งค่า BOT_TOKEN — ตั้งค่าใน Environment Variables ก่อนรัน"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    # ตรวจทุกข้อความที่ไม่ใช่คำสั่ง (รวมแคปชั่น) แต่ไม่ตรวจข้อความจากบอทด้วยกัน
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
