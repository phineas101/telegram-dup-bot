"""
guard.py — ฟีเจอร์ป้องกันกลุ่ม (เสริมให้ bot.py เดิม โดยไม่กระทบการตรวจข้อความซ้ำ)

3 ฟีเจอร์:
  1) กันเตะ (antikick)  — คนไม่ไว้ใจเตะสมาชิก -> ถอดแอดมิน + ปลดแบน + เชิญกลับ + แจ้งเจ้าของ
  2) แคปช่า (captcha)   — คนใหม่ถูกมิวต์ ต้องกดปุ่มยืนยันใน N วิ ไม่งั้นถูกเตะ
  3) กันสแปม (antispam) — ฟลัด/ลิงก์/คำต้องห้าม -> ลบ + เตือน -> ครบโควตาก็มิวต์/เตะ

ออกแบบให้ปลอดภัยกับกลุ่มทำงานเงิน:
  - ทุกฟีเจอร์ "ปิด/แจ้งเตือนอย่างเดียว" เป็นค่าเริ่มต้น เปิดใช้ทีละอย่างผ่าน env
  - เก็บข้อมูลถาวรที่ /data (Volume เดียวกับบอทเดิม)
  - handler อยู่คนละ group กับของเดิม + antispam หยุด propagate เฉพาะตอนเจอสแปมจริง
"""

import html
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("guard")


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "on" if default else "off").strip().lower() in {
        "1", "on", "true", "yes", "y",
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# ---------- การตั้งค่า (env) — ค่าเริ่มต้นปลอดภัย ----------
GUARD_ENABLED = _env_bool("GUARD_ENABLED", True)          # สวิตช์ใหญ่
OWNER_ID = _env_int("OWNER_ID", 0)                        # เจ้าของบอท (0 = ยังไม่ตั้ง)
LOG_CHAT_ID = _env_int("LOG_CHAT_ID", 0)                  # ห้องรับแจ้งเตือน (0 = ไม่มี)

# กันเตะ: off | alert (แจ้งอย่างเดียว) | enforce (ตอบโต้จริง) — เริ่มที่ alert เพื่อความปลอดภัย
ANTIKICK_MODE = os.environ.get("ANTIKICK_MODE", "alert").strip().lower()
WATCH_NEW_ADMINS = _env_bool("WATCH_NEW_ADMINS", True)

# แคปช่า: ปิดเป็นค่าเริ่มต้น (ต้องเป็น supergroup + บอทมีสิทธิ์ก่อน)
CAPTCHA_ENABLED = _env_bool("CAPTCHA_ENABLED", False)
CAPTCHA_TIMEOUT_SEC = _env_int("CAPTCHA_TIMEOUT_SEC", 60)

# กันสแปม: ปิดเป็นค่าเริ่มต้น (กัน false positive กับสลิป)
ANTISPAM_ENABLED = _env_bool("ANTISPAM_ENABLED", False)
FLOOD_ENABLED = _env_bool("FLOOD_ENABLED", True)
FLOOD_MAX_MSGS = _env_int("FLOOD_MAX_MSGS", 6)
FLOOD_WINDOW_SEC = _env_int("FLOOD_WINDOW_SEC", 8)
FLOOD_MUTE_MINUTES = _env_int("FLOOD_MUTE_MINUTES", 60)
LINKS_ENABLED = _env_bool("ANTISPAM_LINKS", True)
BADWORDS = [w.strip().lower() for w in os.environ.get("BADWORDS", "").split(",") if w.strip()]
MAX_WARNS = _env_int("MAX_WARNS", 3)
WARN_ACTION = os.environ.get("WARN_ACTION", "mute").strip().lower()  # mute | kick
WARN_MUTE_MINUTES = _env_int("WARN_MUTE_MINUTES", 60)

# ตำแหน่งไฟล์เก็บข้อมูลถาวร (Volume เดียวกับบอทเดิม)
GUARD_STORE_PATH = os.environ.get("GUARD_STORE_PATH", "/data/guard_storage.json")

# ---------- สิทธิ์ ----------
MUTED = ChatPermissions(can_send_messages=False)
OPEN = ChatPermissions(
    can_send_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


# ---------- storage (JSON ถาวร) ----------
_store = {"trusted_admins": [], "warns": {}}
_store_path = GUARD_STORE_PATH


def _store_load() -> None:
    global _store, _store_path
    for path in (GUARD_STORE_PATH, "guard_storage.json"):
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            data = {"trusted_admins": [], "warns": {}}
            if os.path.exists(path):
                try:
                    data = json.loads(open(path, encoding="utf-8").read())
                except Exception as e:  # ไฟล์เสีย (JSONDecodeError ฯลฯ) — เริ่มใหม่ ไม่ crash
                    log.warning("guard storage ที่ %s อ่านไม่ได้ (%s) — เริ่มใหม่", path, e)
                    data = {"trusted_admins": [], "warns": {}}
            data.setdefault("trusted_admins", [])
            data.setdefault("warns", {})
            _store = data
            _store_path = path
            # พิสูจน์ว่าเขียนได้จริง (ถ้า /data ไม่ได้ mount จะ throw → ตกไป path ถัดไป)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_store, f, ensure_ascii=False, indent=2)
            log.info("guard storage: %s", path)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("ใช้ guard storage ที่ %s ไม่ได้ (%s) — ลองที่อื่น", path, e)
    _store_path = "guard_storage.json"


def _store_save() -> None:
    try:
        with open(_store_path, "w", encoding="utf-8") as f:
            json.dump(_store, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("บันทึก guard storage ไม่สำเร็จ: %s", e)


def get_trusted() -> set:
    return set(_store["trusted_admins"])


def add_trusted(uid: int) -> None:
    if uid not in _store["trusted_admins"]:
        _store["trusted_admins"].append(uid)
        _store_save()


def remove_trusted(uid: int) -> None:
    if uid in _store["trusted_admins"]:
        _store["trusted_admins"].remove(uid)
        _store_save()


def _wkey(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def add_warn(chat_id: int, user_id: int) -> int:
    k = _wkey(chat_id, user_id)
    _store["warns"][k] = _store["warns"].get(k, 0) + 1
    _store_save()
    return _store["warns"][k]


def reset_warns(chat_id: int, user_id: int) -> None:
    _store["warns"].pop(_wkey(chat_id, user_id), None)
    _store_save()


# ---------- utils ----------
def is_owner(uid: int) -> bool:
    return OWNER_ID != 0 and uid == OWNER_ID


def mention(user) -> str:
    if user is None:
        return "ใครบางคน"
    name = getattr(user, "full_name", None) or str(user.id)
    return f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'


# แคชรายชื่อแอดมินต่อกลุ่ม (ลดการเรียก API ตอนกันสแปม)
_admin_cache: dict[int, tuple[set, float]] = {}
_ADMIN_TTL = 300


async def _admin_ids(chat_id: int, bot) -> set:
    now = time.time()
    hit = _admin_cache.get(chat_id)
    if hit and hit[1] > now:
        return hit[0]
    try:
        admins = await bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins}
    except TelegramError:
        ids = _admin_cache.get(chat_id, (set(), 0))[0]
    _admin_cache[chat_id] = (ids, now + _ADMIN_TTL)
    return ids


async def is_exempt(chat_id: int, user, bot) -> bool:
    """ยกเว้นไม่ต้องตรวจ: บอท, เจ้าของ, แอดมินที่ไว้ใจ, และแอดมินกลุ่ม"""
    if user is None or user.is_bot:
        return True
    if is_owner(user.id) or user.id in get_trusted():
        return True
    return user.id in await _admin_ids(chat_id, bot)


async def _alert(context, text: str) -> None:
    target = LOG_CHAT_ID or OWNER_ID
    if not target:
        return
    try:
        await context.bot.send_message(
            target, text, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ส่ง alert ไม่สำเร็จ: %s", e)


# ================= 1) กันเตะ =================
_PRESENT = {
    ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.RESTRICTED, ChatMemberStatus.OWNER,
}
_GONE = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}


def _is_anonymous(actor, chat) -> bool:
    if actor is None or actor.id == chat.id:
        return True
    return getattr(actor, "username", None) == "GroupAnonymousBot"


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GUARD_ENABLED or ANTIKICK_MODE == "off":
        return
    ev = update.chat_member
    if ev is None:
        return
    chat = ev.chat
    old = ev.old_chat_member.status
    new = ev.new_chat_member.status
    victim = ev.new_chat_member.user
    actor = ev.from_user

    # กันลูป: บอทเป็นคนทำเอง (เช่น แคปช่าเตะ) — ไม่ต้องตอบโต้
    if actor and actor.id == context.bot.id:
        return

    if old in _PRESENT and new in _GONE:
        # ออกเอง (ลาออก) — ไม่ทำอะไร
        if new == ChatMemberStatus.LEFT and actor and actor.id == victim.id:
            return
        # แอดมินไม่ระบุตัว — ระบุคนเตะไม่ได้ → แจ้งอย่างเดียว
        if _is_anonymous(actor, chat):
            await _alert(
                context,
                f"⚠️ มีการเตะ {mention(victim)} โดย <b>แอดมินไม่ระบุตัว</b> "
                f"ในกลุ่ม {html.escape(chat.title or '')} — ระบุคนทำไม่ได้",
            )
            return
        # เจ้าของบอท/แอดมินที่ไว้ใจ → ปกติ
        if is_owner(actor.id) or actor.id in get_trusted():
            return
        # ผู้สร้างกลุ่มจริง (creator) → ไม่ยุ่ง (กันไปสู้กับเจ้าของกลุ่ม)
        try:
            am = await context.bot.get_chat_member(chat.id, actor.id)
            if am.status == ChatMemberStatus.OWNER:
                return
        except TelegramError:
            pass

        if ANTIKICK_MODE == "enforce":
            await _rescue(context, chat, actor, victim)
        else:  # alert
            await _alert(
                context,
                f"🛡️ ตรวจพบการเตะ: {mention(victim)} ถูกเตะโดย {mention(actor)} "
                f"ในกลุ่ม {html.escape(chat.title or '')} (โหมดแจ้งเตือน — ยังไม่ตอบโต้)",
            )
        return

    # เฝ้าระวังการตั้งแอดมินใหม่ (แจ้งอย่างเดียวเสมอ)
    if (
        WATCH_NEW_ADMINS
        and new == ChatMemberStatus.ADMINISTRATOR
        and old != ChatMemberStatus.ADMINISTRATOR
        and actor
        and not is_owner(actor.id)
        and actor.id not in get_trusted()
        and not _is_anonymous(actor, chat)
    ):
        await _alert(
            context,
            f"⚠️ {mention(victim)} ถูกตั้งเป็นแอดมินโดย {mention(actor)} "
            f"ในกลุ่ม {html.escape(chat.title or '')} — ตรวจสอบด้วย",
        )


async def _rescue(context, chat, actor, victim) -> None:
    demoted = False
    try:
        await context.bot.promote_chat_member(
            chat.id, actor.id,
            can_manage_chat=False, can_change_info=False, can_delete_messages=False,
            can_invite_users=False, can_restrict_members=False, can_pin_messages=False,
            can_promote_members=False, can_manage_video_chats=False, is_anonymous=False,
            can_manage_topics=False,
        )
        demoted = True
    except TelegramError as e:
        log.info("ถอดแอดมินไม่สำเร็จ (ปกติถ้าบอทไม่ได้ตั้งเขา): %s", e)

    try:
        await context.bot.unban_chat_member(chat.id, victim.id, only_if_banned=True)
    except Exception as e:  # noqa: BLE001
        log.info("ปลดแบนไม่สำเร็จ: %s", e)

    invite = None
    try:
        link = await context.bot.create_chat_invite_link(
            chat.id, member_limit=1, name=f"restore-{victim.id}"
        )
        invite = link.invite_link
    except Exception as e:  # noqa: BLE001
        log.info("สร้างลิงก์เชิญไม่สำเร็จ: %s", e)

    if invite:
        try:
            await context.bot.send_message(
                victim.id,
                f"คุณถูกเตะจากกลุ่ม <b>{html.escape(chat.title or '')}</b>\n"
                f"กดลิงก์นี้เพื่อกลับเข้ากลุ่ม:\n{invite}",
                parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001
            pass

    status_txt = (
        "ถอดสิทธิ์แอดมินคนเตะแล้ว ✅" if demoted
        else "⚠️ ถอดแอดมินไม่ได้ (บอทไม่ได้ตั้งเขา) — เจ้าของต้องจัดการเอง"
    )
    msg = (
        f"🛡️ <b>ตรวจพบการเตะสมาชิก</b>\n"
        f"คนเตะ: {mention(actor)}\nเหยื่อ: {mention(victim)}\n{status_txt}"
    )
    if invite:
        msg += "\nส่งลิงก์เชิญกลับให้เหยื่อทาง DM แล้ว"
    try:
        await context.bot.send_message(
            chat.id, msg, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception:  # noqa: BLE001
        pass
    await _alert(context, msg + f"\nกลุ่ม: {html.escape(chat.title or '')}")


# ================= 2) แคปช่า =================
def _cap_job(chat_id: int, user_id: int) -> str:
    return f"captcha:{chat_id}:{user_id}"


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GUARD_ENABLED or not CAPTCHA_ENABLED:
        return
    ev = update.chat_member
    if ev is None:
        return
    old = ev.old_chat_member.status
    new = ev.new_chat_member.status
    if not (old in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
            and new == ChatMemberStatus.MEMBER):
        return

    chat = ev.chat
    user = ev.new_chat_member.user
    if user.is_bot or await is_exempt(chat.id, user, context.bot):
        return

    # ไม่มี job_queue = จับเวลาไม่ได้ -> ห้ามมิวต์ (ไม่งั้นคนใหม่ติดมิวต์ถาวร)
    if context.job_queue is None:
        log.error("แคปช่าข้าม: ไม่มี job_queue (ต้องลง python-telegram-bot[job-queue])")
        return

    try:
        await context.bot.restrict_chat_member(chat.id, user.id, permissions=MUTED)
    except TelegramError as e:
        log.warning("แคปช่า restrict ไม่สำเร็จ (เป็น supergroup + บอทมีสิทธิ์ Ban ไหม?): %s", e)
        return

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ ยืนยันว่าเป็นคน", callback_data=f"cap:{user.id}")]]
    )
    try:
        m = await context.bot.send_message(
            chat.id,
            f"👋 ยินดีต้อนรับ {mention(user)}\n"
            f"กดปุ่มด้านล่างภายใน {CAPTCHA_TIMEOUT_SEC} วินาที เพื่อยืนยันว่าเป็นคน "
            f"ไม่งั้นจะถูกเตะออกอัตโนมัติ",
            parse_mode="HTML", reply_markup=kb,
        )
    except TelegramError as e:
        log.warning("แคปช่าส่งข้อความไม่สำเร็จ: %s", e)
        return

    for j in context.job_queue.get_jobs_by_name(_cap_job(chat.id, user.id)):
        j.schedule_removal()
    context.job_queue.run_once(
        _cap_timeout, when=CAPTCHA_TIMEOUT_SEC, name=_cap_job(chat.id, user.id),
        data={"chat_id": chat.id, "user_id": user.id, "msg_id": m.message_id},
    )


async def on_captcha_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    if not data.startswith("cap:"):
        return
    try:
        target_id = int(data.split(":", 1)[1])
    except ValueError:
        return
    if q.from_user.id != target_id:
        await q.answer("ปุ่มนี้ไม่ใช่ของคุณ 🚫", show_alert=True)
        return

    chat_id = q.message.chat.id
    try:
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=OPEN)
    except TelegramError as e:
        log.warning("ปลดล็อกสิทธิ์ไม่สำเร็จ: %s", e)
    if context.job_queue is not None:
        for j in context.job_queue.get_jobs_by_name(_cap_job(chat_id, target_id)):
            j.schedule_removal()
    await q.answer("ยืนยันแล้ว ✅ ยินดีต้อนรับ")
    try:
        await q.message.delete()
    except Exception:  # noqa: BLE001
        pass


async def _cap_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    try:
        await context.bot.ban_chat_member(d["chat_id"], d["user_id"])
        await context.bot.unban_chat_member(d["chat_id"], d["user_id"], only_if_banned=True)
    except Exception as e:  # noqa: BLE001
        log.info("แคปช่าเตะไม่สำเร็จ: %s", e)
    try:
        await context.bot.delete_message(d["chat_id"], d["msg_id"])
    except Exception:  # noqa: BLE001
        pass


# ================= 3) กันสแปม =================
_flood: dict[tuple[int, int], deque] = defaultdict(deque)
_flood_last_sweep = [0.0]


def _flood_sweep(now: float) -> None:
    """ลบ key ที่ไม่มี timestamp ในหน้าต่างเวลาแล้ว — กัน _flood โตไม่หยุด"""
    for k in list(_flood.keys()):
        dq = _flood[k]
        while dq and now - dq[0] > FLOOD_WINDOW_SEC:
            dq.popleft()
        if not dq:
            del _flood[k]


def _has_link(message) -> bool:
    ents = list(message.entities or []) + list(message.caption_entities or [])
    for e in ents:
        if e.type in ("url", "text_link"):
            return True
    return False


def _has_badword(text: str) -> bool:
    if not BADWORDS:
        return False
    low = text.lower()
    return any(w in low for w in BADWORDS)


async def _punish(context, chat_id: int, user, reason: str) -> None:
    """เตือน + ครบโควตาก็มิวต์/เตะ"""
    n = add_warn(chat_id, user.id)
    if n < MAX_WARNS:
        try:
            await context.bot.send_message(
                chat_id,
                f"⚠️ {mention(user)} {reason} (เตือน {n}/{MAX_WARNS})",
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    reset_warns(chat_id, user.id)
    if WARN_ACTION == "kick":
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            await context.bot.unban_chat_member(chat_id, user.id, only_if_banned=True)
            note = "ถูกเตะออก"
        except Exception:  # noqa: BLE001
            note = "พยายามเตะแต่ไม่สำเร็จ"
    else:
        until = datetime.now(timezone.utc) + timedelta(minutes=WARN_MUTE_MINUTES)
        try:
            await context.bot.restrict_chat_member(chat_id, user.id, permissions=MUTED, until_date=until)
            note = f"ถูกมิวต์ {WARN_MUTE_MINUTES} นาที"
        except Exception:  # noqa: BLE001
            note = "พยายามมิวต์แต่ไม่สำเร็จ"
    try:
        await context.bot.send_message(
            chat_id, f"🚫 {mention(user)} ครบโควตาเตือน — {note}", parse_mode="HTML"
        )
    except Exception:  # noqa: BLE001
        pass


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GUARD_ENABLED or not ANTISPAM_ENABLED:
        return
    message = update.effective_message
    if message is None or message.chat.type == "private":
        return
    user = message.from_user
    if user is None:
        return
    if await is_exempt(message.chat_id, user, context.bot):
        return

    chat_id = message.chat_id
    text = message.text or message.caption or ""

    # 1) ฟลัด — ส่งถี่เกินในหน้าต่างเวลา -> มิวต์
    if FLOOD_ENABLED:
        now = time.time()
        if now - _flood_last_sweep[0] > 300:  # กวาด key เก่าทุก 5 นาที
            _flood_last_sweep[0] = now
            _flood_sweep(now)
        dq = _flood[(chat_id, user.id)]
        dq.append(now)
        while dq and now - dq[0] > FLOOD_WINDOW_SEC:
            dq.popleft()
        if len(dq) > FLOOD_MAX_MSGS:
            del _flood[(chat_id, user.id)]
            until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MINUTES)
            try:
                await context.bot.restrict_chat_member(chat_id, user.id, permissions=MUTED, until_date=until)
            except Exception:  # noqa: BLE001
                pass
            try:
                await context.bot.send_message(
                    chat_id,
                    f"🚫 {mention(user)} ส่งข้อความถี่เกินไป — มิวต์ {FLOOD_MUTE_MINUTES} นาที",
                    parse_mode="HTML",
                )
            except Exception:  # noqa: BLE001
                pass
            raise ApplicationHandlerStop

    # 2) ลิงก์
    if LINKS_ENABLED and _has_link(message):
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
        await _punish(context, chat_id, user, "ส่งลิงก์ (ไม่อนุญาต)")
        raise ApplicationHandlerStop

    # 3) คำต้องห้าม
    if _has_badword(text):
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
        await _punish(context, chat_id, user, "ใช้คำต้องห้าม")
        raise ApplicationHandlerStop
    # ไม่เข้าเงื่อนไขสแปม -> ปล่อยผ่านไปให้ handler เดิม (ตรวจซ้ำ) ทำงานตามปกติ


# ================= my_chat_member (บอทถูกตั้ง/ถอดแอดมิน) =================
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ev = update.my_chat_member
    if ev is None:
        return
    new = ev.new_chat_member.status
    chat = ev.chat
    log.info("สถานะบอทในกลุ่ม %s (%s): %s -> %s",
             chat.title, chat.id, ev.old_chat_member.status, new)
    if new == ChatMemberStatus.ADMINISTRATOR:
        try:
            await context.bot.send_message(
                chat.id,
                "🛡️ ผมเป็นแอดมินแล้ว พร้อมช่วยดูแลกลุ่มนี้\n"
                "ปิด Group Privacy ใน @BotFather ด้วย เพื่อให้ระบบกันสแปมเห็นข้อความครบ",
            )
        except Exception:  # noqa: BLE001
            pass


# ================= คำสั่งจัดการ =================
def _reply_target(update: Update):
    m = update.effective_message
    return m.reply_to_message.from_user if m.reply_to_message else None


async def _owner_only(update: Update) -> bool:
    if OWNER_ID == 0:
        await update.effective_message.reply_text(
            "⚠️ ยังไม่ได้ตั้ง OWNER_ID — พิมพ์ /id ดู id ของคุณ แล้วตั้งใน Railway Variables ก่อน"
        )
        return False
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("🚫 เฉพาะเจ้าของบอทเท่านั้น")
        return False
    return True


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u, c = update.effective_user, update.effective_chat
    await update.effective_message.reply_text(
        f"👤 user id: <code>{u.id}</code>\n💬 chat id: <code>{c.id}</code>\n"
        f"ชนิดแชท: <code>{c.type}</code>",
        parse_mode="HTML",
    )


async def cmd_trust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update):
        return
    t = _reply_target(update)
    if not t:
        await update.effective_message.reply_text("ตอบกลับข้อความของคนนั้น แล้วพิมพ์ /trust")
        return
    add_trusted(t.id)
    await update.effective_message.reply_text(f"✅ เพิ่ม {mention(t)} เป็นแอดมินที่ไว้ใจแล้ว", parse_mode="HTML")


async def cmd_untrust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update):
        return
    t = _reply_target(update)
    if not t:
        await update.effective_message.reply_text("ตอบกลับข้อความของคนนั้น แล้วพิมพ์ /untrust")
        return
    remove_trusted(t.id)
    await update.effective_message.reply_text(f"เอา {mention(t)} ออกจากรายชื่อไว้ใจแล้ว", parse_mode="HTML")


async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update):
        return
    t = _reply_target(update)
    if not t:
        await update.effective_message.reply_text("ตอบกลับข้อความของคนที่จะตั้งแอดมิน แล้วพิมพ์ /promote")
        return
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, t.id,
            can_manage_chat=True, can_delete_messages=True, can_restrict_members=True,
            can_invite_users=True, can_pin_messages=True, can_manage_video_chats=True,
        )
        add_trusted(t.id)
        await update.effective_message.reply_text(
            f"✅ ตั้ง {mention(t)} เป็นแอดมินผ่านบอทแล้ว (บอทถอดได้ถ้าจำเป็น)", parse_mode="HTML"
        )
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ ไม่สำเร็จ: {e}\n(บอทต้องมีสิทธิ์ Add new admins)")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update):
        return
    t = _reply_target(update)
    if not t:
        await update.effective_message.reply_text("ตอบกลับข้อความของคนนั้น แล้วพิมพ์ /unmute")
        return
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, t.id, permissions=OPEN)
        reset_warns(update.effective_chat.id, t.id)
        await update.effective_message.reply_text(f"🔊 ปลดมิวต์ {mention(t)} แล้ว", parse_mode="HTML")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ ไม่สำเร็จ: {e}")


async def cmd_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ดูสถานะฟีเจอร์ป้องกันกลุ่ม"""
    lines = [
        "🛡️ <b>สถานะระบบป้องกันกลุ่ม</b>",
        f"สวิตช์ใหญ่: {'เปิด' if GUARD_ENABLED else 'ปิด'}",
        f"กันเตะ: <b>{ANTIKICK_MODE}</b> (off/alert/enforce)",
        f"แคปช่า: {'เปิด' if CAPTCHA_ENABLED else 'ปิด'} ({CAPTCHA_TIMEOUT_SEC}s)",
        f"กันสแปม: {'เปิด' if ANTISPAM_ENABLED else 'ปิด'} "
        f"(ฟลัด {FLOOD_MAX_MSGS}/{FLOOD_WINDOW_SEC}s, ลิงก์ {'เปิด' if LINKS_ENABLED else 'ปิด'}, "
        f"คำต้องห้าม {len(BADWORDS)} คำ)",
        f"OWNER_ID: {OWNER_ID or 'ยังไม่ตั้ง'} | LOG_CHAT_ID: {LOG_CHAT_ID or 'ไม่มี'}",
        f"แอดมินที่ไว้ใจ: {len(get_trusted())} คน",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ================= register =================
def register(app) -> None:
    """เพิ่ม handler ของ guard เข้า Application (คนละ group กับของเดิม)"""
    if not GUARD_ENABLED:
        log.info("GUARD_ENABLED=off — ไม่โหลดฟีเจอร์ป้องกันกลุ่ม")
        return
    _store_load()

    # กันสแปม: group -1 (รันก่อน handler เดิม; หยุด propagate เฉพาะตอนเจอสแปม)
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND & ~filters.VIA_BOT, on_message),
        group=-1,
    )
    # แคปช่า + กันเตะ: chat_member คนละ group กัน (ให้ทำงานทั้งคู่ต่อ 1 อัปเดต)
    app.add_handler(ChatMemberHandler(on_new_member, ChatMemberHandler.CHAT_MEMBER), group=1)
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER), group=2)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=3)
    # ปุ่มแคปช่า
    app.add_handler(CallbackQueryHandler(on_captcha_click, pattern=r"^cap:"))
    # คำสั่งจัดการ (default group; ชื่อไม่ชนของเดิม /start /status)
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("trust", cmd_trust))
    app.add_handler(CommandHandler("untrust", cmd_untrust))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("guard", cmd_guard))

    if CAPTCHA_ENABLED and app.job_queue is None:
        log.error("CAPTCHA_ENABLED แต่ไม่มี job_queue — ต้องลง python-telegram-bot[job-queue]")

    log.info(
        "guard พร้อม: antikick=%s captcha=%s antispam=%s owner=%s",
        ANTIKICK_MODE, CAPTCHA_ENABLED, ANTISPAM_ENABLED, OWNER_ID or "unset",
    )
