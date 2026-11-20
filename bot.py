"""Boltix API: the menu bot that links users' own bots.

Flow: /start -> Boltix ff -> send bot token -> optional force-join channel -> Purchase or Connect.
Once a bot is connected, /start shows only the manage panel until the bot is disconnected or removed.
Linked bots are saved in the shared database. The Boltix ff bot runs them as sub bots.
"""
import asyncio
import hashlib
import html
import logging
import os
import re
from urllib.parse import quote

import asyncpg
import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("boltix-api")

BASE = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL_MAIN") or os.environ.get("DATABASE_URL", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "dev_light01")
PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "10000"))
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "8161796121")
ACCOUNT_BANK = os.environ.get("ACCOUNT_BANK", "MONIEPOINT")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "FORTUNE")

FREE_REQUESTS = 20
PLANS = [(20, 1000), (40, 2000), (60, 4000), (100, 8000)]  # (requests, naira)
PRICE = dict(PLANS)
MENU_IMAGE = os.path.join(BASE, "menu.jpg")
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
CHANNEL_RE = re.compile(r"^(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{4,31})/?$")
LINE = "━━━━━━━━━━━━━━━━"
OWNER_LINK = f"https://t.me/{OWNER_USERNAME}"

router = Router()
SESS = {}  # user_id -> session dict, kept in memory
_DB = None


def new_session():
    return {"await": None, "bot": None, "channels": [], "msg": None, "chat": None}


def sess(uid):
    return SESS.setdefault(uid, new_session())


def esc(value):
    return html.escape(str(value or ""))


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text, link):
    return InlineKeyboardButton(text=text, url=link)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


# ===== SCREENS =====
def home_caption(name):
    return (
        "🔥 <b>BOLTIX API</b>\n" + LINE + "\n"
        f"Welcome, <b>{esc(name)}</b>.\n"
        "Link your own bot and run Boltix tools from it.\n\n"
        "✅ <b>Boltix ff</b>: open now\n"
        "🔒 <b>BX ban check</b>: Pending launch\n"
        "🔒 <b>Spin the Bottle</b>: Pending launch\n"
        "🔒 <b>Hoshigami anime</b>: Pending launch\n\n"
        "Tap <b>Boltix ff</b> to start."
    )


def home_kb():
    return kb(
        [btn("🎮 Boltix ff", "ff"), btn("🔒 BX ban check", "locked")],
        [btn("🔒 Spin the Bottle", "locked"), btn("🔒 Hoshigami anime", "locked")],
    )


def ff_caption(error=None):
    body = (
        "🎮 <b>Boltix ff</b>\n" + LINE + "\n"
        "You have access to all Boltix ff features.\n"
        f"Your sub bot can only make <b>{FREE_REQUESTS} requests</b>.\n\n"
        "Send your bot API token from @BotFather to link your bot.\n"
        "▸ Example: <code>123456789:AA...</code>"
    )
    if error:
        body = f"⚠️ {esc(error)}\n\n" + body
    return body


def channel_caption(username, error=None, adding=False):
    title = "Add a force-join channel" if adding else "Force-join channel"
    body = (
        f"📢 <b>{title}</b> for @{esc(username)}\n" + LINE + "\n"
        "Send your channel link, like <code>@mychannel</code> or <code>https://t.me/mychannel</code>.\n"
        "Add your bot as an <b>admin</b> in that channel first.\n\n"
        "Users of your bot will have to join this channel."
    )
    if not adding:
        body += "\n\nNo channel? Tap Skip."
    if error:
        body = f"⚠️ {esc(error)}\n\n" + body
    return body


def channel_kb(adding=False):
    if adding:
        return kb([btn("⬅️ Back", "manage")])
    return kb([btn("⏭ Skip", "skip_channel")], [btn("⬅️ Back", "home")])


def linked_caption(username, channels, note=None):
    text = (
        f"🤖 <b>@{esc(username)}</b> is ready.\n" + LINE + "\n"
        "You have access to all Boltix ff features.\n"
        f"Your sub bot can only make <b>{FREE_REQUESTS} requests</b>.\n"
        f"Force-join channels: <b>{channels}</b>\n\n"
        "Choose an option below."
    )
    return f"{note}\n\n{text}" if note else text


def linked_kb():
    return kb([btn("💳 Purchase", "buy"), btn("🔗 Connect", "connect")], [btn("⬅️ Back", "home")])


def plans_caption(username):
    lines = "\n".join(f"▸ <b>{n} requests</b> = ₦{p:,}" for n, p in PLANS)
    return f"💳 <b>Purchase requests</b> for @{esc(username)}\n{LINE}\n{lines}\n\nPick a plan."


def plans_kb():
    rows = [[btn(f"{n} requests · ₦{p:,}", f"plan:{n}")] for n, p in PLANS]
    rows.append([btn("⬅️ Back", "linked")])
    return kb(*rows)


def pay_caption(n):
    return (
        f"💳 <b>{n} requests</b> · ₦{PRICE[n]:,}\n{LINE}\n"
        "Make your payment to:\n\n"
        f"Account number: <code>{ACCOUNT_NUMBER}</code>\n"
        f"Bank: <b>{esc(ACCOUNT_BANK)}</b>\n"
        f"Name: <b>{esc(ACCOUNT_NAME)}</b>\n\n"
        "After you pay, tap <b>I've paid</b>."
    )


def pay_kb(n):
    return kb([btn("✅ I've paid", f"paid:{n}")], [btn("Cancel ❌", "buy")])


def paid_caption(n, username):
    message = f"Hi, I paid ₦{PRICE[n]:,} for {n} requests for my bot @{username}. Please confirm."
    return (
        "✅ <b>Payment noted</b>\n" + LINE + "\n"
        f"Plan: <b>{n} requests</b> for @{esc(username)}\n\n"
        "Tap <b>Chat owner</b> and send this message:\n"
        f"<code>{esc(message)}</code>\n\n"
        "Once the owner confirms, your requests are added."
    )


def paid_kb():
    return kb([url_btn("💬 Chat owner", OWNER_LINK)], [btn("🔗 Connect", "connect")], [btn("⬅️ Home", "home")])


def manage_caption(row, note=None):
    text = (
        f"🛠 <b>Manage @{esc(row['username'])}</b>\n" + LINE + "\n"
        "Status: <b>Connected ✅</b>\n"
        f"Requests left: <b>{row['credits']}</b>\n"
        f"Force-join channels: <b>{row['channels']}</b>\n\n"
        "Choose an option below."
    )
    return f"{note}\n\n{text}" if note else text


def manage_kb(row):
    return kb(
        [btn("💳 Purchase requests", "buy")],
        [btn("➕ Add channel", "addch")],
        [url_btn("🤖 Open my bot", f"https://t.me/{row['username']}")],
        [btn("🔌 Disconnect", "disc")],
    )


def disconnect_caption(username):
    return (
        f"🔌 <b>Disconnect @{esc(username)}?</b>\n" + LINE + "\n"
        "The bot will stop working. You can connect it again later with Connect."
    )


def disconnect_kb():
    return kb([btn("✅ Yes, disconnect", "disc_yes")], [btn("❌ No", "manage")])


# ===== DISPLAY =====
async def show(bot, chat_id, uid, caption, markup):
    """Edit the one menu message in place (same image, new caption and buttons)."""
    s = sess(uid)
    if s["msg"] and s["chat"] == chat_id:
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=s["msg"], caption=caption, reply_markup=markup)
            return
        except TelegramAPIError as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("Could not edit the menu, sending a new one: %s", exc)
    sent = await bot.send_photo(chat_id, FSInputFile(MENU_IMAGE), caption=caption, reply_markup=markup)
    s["msg"], s["chat"] = sent.message_id, chat_id


async def show_manage(bot, chat_id, uid, row, note=None):
    await show(bot, chat_id, uid, manage_caption(row, note), manage_kb(row))


# ===== TELEGRAM HELPERS =====
async def telegram_get_me(token):
    if not TOKEN_RE.match(token or ""):
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data.get("result") if data.get("ok") else None


async def check_channel(sub_token, sub_id, raw):
    """Returns (channel_dict, None) on success or (None, error_text)."""
    m = CHANNEL_RE.match((raw or "").strip())
    if not m:
        return None, "That doesn't look like a channel link. Send something like @mychannel."
    handle = "@" + m.group(1)
    try:
        async with Bot(sub_token) as sub:
            chat = await sub.get_chat(handle)
            member = await sub.get_chat_member(chat_id=handle, user_id=sub_id)
    except Exception as exc:  # any Telegram or network error means the channel can't be checked
        log.info("Channel check failed for %s: %s", handle, exc)
        return None, "I couldn't find that channel. Make sure it's public and your bot is an admin there."
    status = str(getattr(member.status, "value", member.status)).lower()
    if status not in ("administrator", "creator"):
        return None, "Add your bot as an admin in that channel first, then send the link again."
    return {"chat": handle, "label": chat.title or handle, "url": f"https://t.me/{m.group(1)}"}, None


# ===== DATABASE =====
async def db_init():
    global _DB
    if not DATABASE_URL:
        log.error("DATABASE_URL_MAIN is not set. Linking and purchases are unavailable.")
        return
    try:
        _DB = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_bots (
                bot_id BIGINT PRIMARY KEY,
                token TEXT NOT NULL,
                username TEXT,
                owner_id BIGINT,
                credits INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                free_granted BOOLEAN NOT NULL DEFAULT false,
                active BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_channels (
                id SERIAL PRIMARY KEY,
                bot_id BIGINT NOT NULL,
                chat TEXT NOT NULL,
                label TEXT,
                url TEXT,
                UNIQUE (bot_id, chat))""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_payments (
                id SERIAL PRIMARY KEY,
                buyer_id BIGINT NOT NULL,
                bot_id BIGINT NOT NULL,
                bot_username TEXT,
                plan INTEGER NOT NULL,
                amount_naira INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        log.info("Database connected and tables are ready.")
    except Exception as exc:
        log.error("Database connection failed: %s", exc)
        _DB = None


async def active_bot(uid):
    """The user's connected bot (active and owned by them), or None."""
    if _DB is None:
        return None
    return await _DB.fetchrow(
        """SELECT b.bot_id, b.username, b.token, b.credits,
                  (SELECT count(*) FROM sub_channels c WHERE c.bot_id = b.bot_id) AS channels
           FROM sub_bots b WHERE b.owner_id = $1 AND b.active = true
           ORDER BY b.created_at DESC LIMIT 1""",
        uid,
    )


async def link_bot(uid, bot_info, token, channels):
    """Save and activate the bot for this buyer. Returns the row, or None if another account owns it."""
    row = await _DB.fetchrow("SELECT owner_id FROM sub_bots WHERE bot_id = $1", bot_info["id"])
    if row and row["owner_id"] and row["owner_id"] != uid:
        return None
    await _DB.execute(
        """INSERT INTO sub_bots (bot_id, token, username, owner_id, credits, free_granted, active)
           VALUES ($1, $2, $3, $4, $5, true, true)
           ON CONFLICT (bot_id) DO UPDATE SET
               token = EXCLUDED.token,
               username = EXCLUDED.username,
               owner_id = EXCLUDED.owner_id,
               active = true,
               credits = sub_bots.credits + CASE WHEN sub_bots.free_granted THEN 0 ELSE EXCLUDED.credits END,
               free_granted = true""",
        bot_info["id"], token, bot_info.get("username") or str(bot_info["id"]), uid, FREE_REQUESTS,
    )
    for ch in channels:
        await _DB.execute(
            "INSERT INTO sub_channels (bot_id, chat, label, url) VALUES ($1, $2, $3, $4) ON CONFLICT (bot_id, chat) DO NOTHING",
            bot_info["id"], ch["chat"], ch["label"], ch["url"],
        )
    return await active_bot(uid)


async def notify_owner(bot, buyer, n, s_bot):
    if not OWNER_ID:
        return
    text = (
        "💰 <b>Payment claim</b>\n"
        f"Plan: <b>{n} requests</b> (₦{PRICE[n]:,})\n"
        f"Buyer: <a href=\"tg://user?id={buyer['id']}\">{esc(buyer['name'])}</a>\n"
        f"Sub bot: @{esc(s_bot['username'])}\n\n"
        f"After you confirm the money, send in the Boltix ff bot:\n<code>/{n} {s_bot['token']}</code>"
    )
    try:
        await bot.send_message(int(OWNER_ID), text)
    except (TelegramAPIError, ValueError) as exc:
        log.error("Could not message the owner (they must start this bot once): %s", exc)


async def ensure_bot(uid):
    """Session bot if present, otherwise the connected bot from the database."""
    s = sess(uid)
    if s["bot"]:
        return s["bot"]
    row = await active_bot(uid)
    if row:
        s["bot"] = {"id": row["bot_id"], "username": row["username"], "token": row["token"]}
    return s["bot"]


# ===== HANDLERS =====
@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    SESS[uid] = new_session()
    row = await active_bot(uid)
    if row:
        sess(uid)["bot"] = {"id": row["bot_id"], "username": row["username"], "token": row["token"]}
        await show_manage(message.bot, message.chat.id, uid, row)
        return
    await show(message.bot, message.chat.id, uid, home_caption(message.from_user.first_name or "friend"), home_kb())


@router.callback_query(F.data == "locked")
async def on_locked(cb: CallbackQuery):
    await cb.answer("Pending launch", show_alert=True)


@router.callback_query()
async def on_button(cb: CallbackQuery):
    uid = cb.from_user.id
    chat = cb.message.chat.id
    data = cb.data or ""
    s = sess(uid)
    bot = cb.bot

    if s["msg"] and cb.message.message_id != s["msg"]:
        await cb.answer("This menu is old. Send /start to open a new one.", show_alert=True)
        return
    try:
        await cb.answer()
    except TelegramAPIError:
        pass

    if data == "home":
        row = await active_bot(uid)
        if row:
            await show_manage(bot, chat, uid, row)
            return
        s["await"] = None
        await show(bot, chat, uid, home_caption(cb.from_user.first_name or "friend"), home_kb())

    elif data == "ff":
        row = await active_bot(uid)
        if row:
            await show_manage(bot, chat, uid, row)
            return
        s["await"] = "token"
        await show(bot, chat, uid, ff_caption(), kb([btn("⬅️ Back", "home")]))

    elif data == "skip_channel":
        s["channels"] = []
        s["await"] = None
        await show(bot, chat, uid, linked_caption(s["bot"]["username"], 0), linked_kb())

    elif data == "linked":
        if not await ensure_bot(uid):
            await show(bot, chat, uid, ff_caption("Send your bot token first."), kb([btn("⬅️ Back", "home")]))
            return
        await show(bot, chat, uid, linked_caption(s["bot"]["username"], len(s["channels"])), linked_kb())

    elif data == "manage":
        row = await active_bot(uid)
        if row:
            await show_manage(bot, chat, uid, row)

    elif data == "buy":
        if not await ensure_bot(uid):
            await show(bot, chat, uid, ff_caption("Send your bot token first."), kb([btn("⬅️ Back", "home")]))
            return
        await show(bot, chat, uid, plans_caption(s["bot"]["username"]), plans_kb())

    elif data == "connect":
        if not await ensure_bot(uid) and not s["bot"]:
            await show(bot, chat, uid, ff_caption("Send your bot token first."), kb([btn("⬅️ Back", "home")]))
            return
        if _DB is None:
            await show(bot, chat, uid, linked_caption(s["bot"]["username"], len(s["channels"]),
                                                     "⚠️ The service is starting. Try Connect again in a minute."), linked_kb())
            return
        row = await link_bot(uid, {"id": s["bot"]["id"], "username": s["bot"]["username"]},
                             s["bot"]["token"], s["channels"])
        if row is None:
            await show(bot, chat, uid, linked_caption(s["bot"]["username"], len(s["channels"]),
                                                     "⚠️ This bot is already linked to another account."), linked_kb())
            return
        s["channels"] = []
        await show_manage(bot, chat, uid, row, note="✅ Connected. Your bot starts within about 30 seconds.")

    elif data == "addch":
        row = await active_bot(uid)
        if not row:
            return
        s["await"] = "channel_add"
        await show(bot, chat, uid, channel_caption(row["username"], adding=True), channel_kb(adding=True))

    elif data == "disc":
        row = await active_bot(uid)
        if row:
            await show(bot, chat, uid, disconnect_caption(row["username"]), disconnect_kb())

    elif data == "disc_yes":
        row = await active_bot(uid)
        if row:
            await _DB.execute("UPDATE sub_bots SET active = false WHERE bot_id = $1 AND owner_id = $2", row["bot_id"], uid)
        s["bot"] = None
        s["await"] = None
        await show(bot, chat, uid, home_caption(cb.from_user.first_name or "friend"), home_kb())

    elif data.startswith("plan:"):
        try:
            n = int(data.split(":", 1)[1])
        except ValueError:
            return
        if n not in PRICE or not await ensure_bot(uid):
            return
        await show(bot, chat, uid, pay_caption(n), pay_kb(n))

    elif data.startswith("paid:"):
        try:
            n = int(data.split(":", 1)[1])
        except ValueError:
            return
        s_bot = await ensure_bot(uid)
        if n not in PRICE or not s_bot:
            return
        if _DB is not None:
            await _DB.execute(
                "INSERT INTO sub_payments (buyer_id, bot_id, bot_username, plan, amount_naira) VALUES ($1, $2, $3, $4, $5)",
                uid, s_bot["id"], s_bot["username"], n, PRICE[n],
            )
        await notify_owner(bot, {"id": uid, "name": cb.from_user.first_name or str(uid)}, n, s_bot)
        await show(bot, chat, uid, paid_caption(n, s_bot["username"]), paid_kb())


@router.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    s = sess(uid)
    if s["await"] not in ("token", "channel_new", "channel_add"):
        return
    text = (message.text or "").strip()
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    bot, chat = message.bot, message.chat.id

    if s["await"] == "token":
        if not TOKEN_RE.match(text):
            await show(bot, chat, uid, ff_caption("That doesn't look like a bot token."), kb([btn("⬅️ Back", "home")]))
            return
        info = await telegram_get_me(text)
        if not info:
            await show(bot, chat, uid, ff_caption("Telegram didn't accept that token. Check it and send it again."),
                       kb([btn("⬅️ Back", "home")]))
            return
        if BOT_TOKEN and str(info["id"]) == BOT_TOKEN.split(":")[0]:
            await show(bot, chat, uid, ff_caption("That is the Boltix main bot. Send your own bot's token."),
                       kb([btn("⬅️ Back", "home")]))
            return
        s["bot"] = {"id": info["id"], "username": info.get("username") or str(info["id"]), "token": text}
        s["channels"] = []
        s["await"] = "channel_new"
        await show(bot, chat, uid, channel_caption(s["bot"]["username"]), channel_kb())
        return

    if s["await"] == "channel_new":
        ch, error = await check_channel(s["bot"]["token"], s["bot"]["id"], text)
        if error:
            await show(bot, chat, uid, channel_caption(s["bot"]["username"], error), channel_kb())
            return
        s["channels"] = [ch]
        s["await"] = None
        await show(bot, chat, uid, linked_caption(s["bot"]["username"], 1,
                                                 f"📢 Channel added: <b>{esc(ch['label'])}</b>"), linked_kb())
        return

    # channel_add: a connected bot gets another force-join channel
    row = await active_bot(uid)
    s["await"] = None
    if not row:
        await show(bot, chat, uid, home_caption(message.from_user.first_name or "friend"), home_kb())
        return
    ch, error = await check_channel(row["token"], row["bot_id"], text)
    if error:
        s["await"] = "channel_add"
        await show(bot, chat, uid, channel_caption(row["username"], error, adding=True), channel_kb(adding=True))
        return
    await _DB.execute(
        "INSERT INTO sub_channels (bot_id, chat, label, url) VALUES ($1, $2, $3, $4) ON CONFLICT (bot_id, chat) DO NOTHING",
        row["bot_id"], ch["chat"], ch["label"], ch["url"],
    )
    row = await active_bot(uid)
    await show_manage(bot, chat, uid, row, note=f"📢 Channel added: <b>{esc(ch['label'])}</b>")


# ===== KEEP-ALIVE AND RUN =====
async def keepalive_loop(base_url):
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            await asyncio.sleep(300)
            try:
                await client.get(base_url.rstrip("/") + "/health")
            except httpx.HTTPError as exc:
                log.warning("Keep-alive failed: %s", exc)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it in Render environment variables.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    updates = dp.resolve_used_update_types()

    if not PUBLIC_URL:
        async def _poll():
            await bot.delete_webhook(drop_pending_updates=True)
            await db_init()
            await dp.start_polling(bot, allowed_updates=updates)
        asyncio.run(_poll())
        return

    async def run_webhook():
        secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
        path = f"/tg/{secret}"
        await bot.set_webhook(url=PUBLIC_URL.rstrip("/") + path, secret_token=secret,
                              allowed_updates=updates, drop_pending_updates=True)
        await db_init()
        app = web.Application()
        app.router.add_get("/health", lambda request: web.Response(text="ok"))
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Boltix API webhook running on port %s", PORT)
        asyncio.create_task(keepalive_loop(PUBLIC_URL))
        await asyncio.Event().wait()

    asyncio.run(run_webhook())


if __name__ == "__main__":
    main()
