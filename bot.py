"""
Telegram Course Bot + FamGateway auto-verification + Admin panel
Env vars: BOT_TOKEN, FAM_API_KEY, ADMIN_ID, (optional) DATA_DIR
"""
import asyncio
import hashlib
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaVideo, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

# ================== CONFIG ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
FAM_API_KEY = os.environ["FAM_API_KEY"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
FAM_BASE = "https://famgateway.in"
DATA_DIR = os.environ.get("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
POLL_EVERY = 4      # seconds
ORDER_TTL = 300     # gateway order expiry (5 min)
HOME_TEXT = "📈 𝗦𝗘𝗟𝗘𝗖𝗧 𝗬𝗢𝗨𝗥 𝗧𝗥𝗔𝗗𝗜𝗡𝗚 𝗚𝗥𝗢𝗨𝗣 👇"
# ============================================

logging.basicConfig(level=logging.INFO)
HEADERS = {"X-Api-Key": FAM_API_KEY, "Content-Type": "application/json"}


# ---------- storage (JSON file) ----------
def normalize(d):
    d.setdefault("demos", [])
    if d.get("demo"):  # migrate old single-video format
        d["demos"].append(d["demo"])
    d.pop("demo", None)
    for x in d["demos"]:
        x.setdefault("uid", hashlib.md5(x["file_id"].encode()).hexdigest()[:12])
    d.setdefault("plans", {})
    d.setdefault("next_id", 1)
    return d


def load_db():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return normalize(d)


def save_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)


DB = load_db()
active = {}        # user_id -> {"order_id", "plan", "task"}
delivered = set()  # order_ids already fulfilled


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ADMIN_ID


# ---------- FamGateway ----------
async def fam_create_order(amount, name):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{FAM_BASE}/api/create-order", headers=HEADERS,
                         json={"amount": float(amount), "customer_name": name})
        return r.json()


async def fam_verify(order_id):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FAM_BASE}/api/verify-order.php",
                        headers=HEADERS, params={"order_id": order_id})
        return r.json()


async def fetch_bytes(url):
    async with httpx.AsyncClient(timeout=20) as c:
        return (await c.get(url)).content


# ---------- user side ----------
def plans_keyboard():
    rows = [[InlineKeyboardButton(f"{p['name']} - ₹{p['price']}",
                                  callback_data=f"plan:{pid}")]
            for pid, p in DB["plans"].items()]
    return InlineKeyboardMarkup(rows) if rows else None


async def send_home(bot, chat_id):
    """Demo video(s) first, with the plan buttons underneath."""
    kb = plans_keyboard()
    demos = DB["demos"][:10]
    body = HOME_TEXT if kb else "No plans are available right now. Please check back soon."
    if len(demos) == 1:
        d = demos[0]
        cap = (d["caption"][:800] + "\n\n") if d.get("caption") else ""
        await bot.send_video(chat_id, d["file_id"], caption=cap + body,
                             reply_markup=kb, supports_streaming=True)
    elif len(demos) > 1:
        media = [InputMediaVideo(d["file_id"],
                                 caption=(d["caption"][:900] if i == 0 and d.get("caption") else None),
                                 supports_streaming=True)
                 for i, d in enumerate(demos)]
        await bot.send_media_group(chat_id, media)
        await bot.send_message(chat_id, body, reply_markup=kb)
    elif kb:
        await bot.send_message(chat_id, f"👋 Welcome!\n\n{body}", reply_markup=kb)
    else:
        await bot.send_message(chat_id, body)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_home(ctx.bot, update.effective_chat.id)


async def get_link(ctx, plan):
    link = plan["link"]
    if link.lstrip("-").isdigit():  # chat id -> single-use invite
        try:
            inv = await ctx.bot.create_chat_invite_link(int(link), member_limit=1)
            return inv.invite_link
        except Exception as e:
            logging.warning("invite link failed: %s", e)
            return None
    return link


async def del_qr(bot, uid, a):
    mid = a.get("qr_msg")
    if mid:
        try:
            await bot.delete_message(uid, mid)
        except Exception:
            pass


async def deliver(ctx, user_id, order_id, plan, utr=""):
    if order_id in delivered:
        return
    delivered.add(order_id)
    a = active.get(user_id)
    link = await get_link(ctx, plan)
    if not link:
        delivered.discard(order_id)
        await ctx.bot.send_message(
            user_id, "✅ Payment received, but we could not generate your link right now. "
                     "The admin has been notified and will send it to you shortly.")
        await ctx.bot.send_message(
            ADMIN_ID, f"⚠️ Link generation failed.\nUser ID: {user_id}\n"
                      f"Order: {order_id}\nUTR: {utr}\nPlan: {plan['name']}\n"
                      "Make sure the bot is an admin in the group with 'Invite users' permission.")
        return
    if a:
        await del_qr(ctx.bot, user_id, a)  # remove the QR/payment message first
    await ctx.bot.send_message(
        user_id, f"✅ Payment confirmed!\n\n🎓 Your access link:\n{link}\n\n"
                 "This link works only once, so please join right away.")
    active.pop(user_id, None)


async def poll_order(ctx, user_id, order_id, plan):
    waited = 0
    try:
        while waited <= ORDER_TTL + 20:
            await asyncio.sleep(POLL_EVERY)
            waited += POLL_EVERY
            res = await fam_verify(order_id)
            status = res.get("status")
            if status == "success":
                data = res.get("data", {})
                if float(data.get("amount", 0)) >= float(plan["price"]):
                    await deliver(ctx, user_id, order_id, plan, data.get("utr", ""))
                return
            if status in ("expired", "not_found"):
                break
        a = active.pop(user_id, None)
        if a:
            await del_qr(ctx.bot, user_id, a)
        await ctx.bot.send_message(
            user_id, "⌛ Your order has expired. Send /start to choose a plan again.")
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("poll error")


async def on_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data.split(":")[1]
    plan = DB["plans"].get(pid)
    if not plan:
        await q.answer("This plan is no longer available.", show_alert=True)
        return
    await q.answer()

    old = active.pop(uid, None)
    if old:
        old["task"].cancel()
        await del_qr(ctx.bot, uid, old)

    res = await fam_create_order(plan["price"], q.from_user.full_name)
    if res.get("status") != "success":
        logging.error("create-order failed: %s", res)
        await ctx.bot.send_message(uid, "❌ We could not create your order. Please try again in a moment.")
        return
    d = res["data"]

    caption = (
        f"📌 {plan['name']}\n\n"
        f"💰 Pay exactly: ₹{d['payable_amount']}\n"
        f"💳 UPI ID: {d['upi_id']}\n"
        f"📲 Scan the QR code with any UPI app and pay.\n"
        f"⏱ This order expires in 5 minutes.\n"
        f"⚡ Your access link is sent automatically after payment.\n\n"
        f"⚠️ Please send the exact amount shown above."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Pay with UPI app", url=d["checkout_url"])],
        [InlineKeyboardButton("I have paid ✅", callback_data="paid")],
        [InlineKeyboardButton("⬅ Back", callback_data="back")],
    ])
    msg = await ctx.bot.send_photo(uid, await fetch_bytes(d["qr_url"]),
                                   caption=caption, reply_markup=kb)
    task = asyncio.create_task(poll_order(ctx, uid, d["order_id"], plan))
    active[uid] = {"order_id": d["order_id"], "plan": plan, "task": task,
                   "qr_msg": msg.message_id}


async def on_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    a = active.get(uid)
    if not a:
        await q.answer("No active order. Send /start to begin.", show_alert=True)
        return
    res = await fam_verify(a["order_id"])
    data = res.get("data", {})
    if res.get("status") == "success" and float(data.get("amount", 0)) >= float(a["plan"]["price"]):
        await q.answer()
        a["task"].cancel()
        await deliver(ctx, uid, a["order_id"], a["plan"], data.get("utr", ""))
    else:
        await q.answer("Payment not received yet. Please wait a few seconds and try again.",
                       show_alert=True)


async def on_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    old = active.pop(q.from_user.id, None)
    if old:
        old["task"].cancel()
    try:
        await q.message.delete()
    except Exception:
        pass
    await send_home(ctx.bot, q.message.chat_id)


# ---------- admin side ----------
def admin_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add plan", callback_data="adm:add")],
        [InlineKeyboardButton("📋 View / delete plans", callback_data="adm:list")],
        [InlineKeyboardButton("🎬 Add demo video(s)", callback_data="adm:demo")],
        [InlineKeyboardButton("📋 View / delete demo videos", callback_data="adm:demos")],
        [InlineKeyboardButton("🗑 Remove all demo videos", callback_data="adm:rmdemo")],
        [InlineKeyboardButton("💾 Backup data", callback_data="adm:backup")],
    ])


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("🛠 Admin panel", reply_markup=admin_panel_kb())


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=admin_panel_kb())


async def on_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not allowed.", show_alert=True)
        return
    await q.answer()
    kind, _, val = q.data.partition(":")
    chat = q.message.chat_id

    if kind == "dv":
        DB["demos"] = [x for x in DB["demos"] if x["uid"] != val]
        save_db()
        await q.edit_message_text("🗑 Demo video deleted. Open /admin to continue.")
    elif kind == "del":
        DB["plans"].pop(val, None)
        save_db()
        await q.edit_message_text("🗑 Plan deleted. Open /admin to continue.")
    elif val == "add":
        ctx.user_data.clear()
        ctx.user_data["step"] = "name"
        await ctx.bot.send_message(
            chat, "Step 1/3: Send the plan name.\nExample: Group 1 - Candlestick Basics\n\n/cancel to stop.")
    elif val == "list":
        if not DB["plans"]:
            await ctx.bot.send_message(chat, "No plans yet. Use ➕ Add plan.")
            return
        rows = [[InlineKeyboardButton(f"🗑 {p['name']} - ₹{p['price']}", callback_data=f"del:{pid}")]
                for pid, p in DB["plans"].items()]
        await ctx.bot.send_message(chat, "Tap a plan to delete it:",
                                   reply_markup=InlineKeyboardMarkup(rows))
    elif val == "demo":
        await ctx.bot.send_message(
            chat, "🎬 Send me one or more videos now (max 10). Each video is added to the demo list "
                  "and shown to every user at the top when they press /start.\n\n"
                  "Tip: the caption of the FIRST video is shown with the demo.")
    elif val == "demos":
        if not DB["demos"]:
            await ctx.bot.send_message(chat, "No demo videos yet. Use 🎬 Add demo video(s).")
            return
        rows = [[InlineKeyboardButton(f"🗑 Delete video {i + 1}", callback_data=f"dv:{x['uid']}")]
                for i, x in enumerate(DB["demos"])]
        await ctx.bot.send_message(
            chat, f"You have {len(DB['demos'])} demo video(s), shown in this order. Tap to delete:",
            reply_markup=InlineKeyboardMarkup(rows))
    elif val == "rmdemo":
        DB["demos"] = []
        save_db()
        await ctx.bot.send_message(chat, "🗑 All demo videos removed.")
    elif val == "backup":
        save_db()
        with open(DATA_FILE, "rb") as f:
            await ctx.bot.send_document(
                chat, f, filename="data.json",
                caption="Backup. To restore after a redeploy, just send this file back to the bot.")


async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get("step")
    if not step:
        return
    t = update.message.text.strip()

    if step == "name":
        ctx.user_data["new"] = {"name": t[:60]}
        ctx.user_data["step"] = "price"
        await update.message.reply_text("Step 2/3: Send the price in INR (numbers only). Example: 49")
    elif step == "price":
        try:
            price = float(t)
            if price <= 0:
                raise ValueError
            price = int(price) if price == int(price) else round(price, 2)
        except ValueError:
            await update.message.reply_text("Please send a valid number, for example 49")
            return
        ctx.user_data["new"]["price"] = price
        ctx.user_data["step"] = "link"
        await update.message.reply_text(
            "Step 3/3: Send the group invite link (https://t.me/+...) OR the group chat ID (-100...).\n\n"
            "If you send a chat ID, add this bot as an admin of that group with the 'Invite users' "
            "permission. The bot will then create a separate single-use link for each buyer.")
    elif step == "link":
        if not (t.startswith("http") or t.lstrip("-").isdigit()):
            await update.message.reply_text("Please send a valid invite link or a numeric chat ID.")
            return
        new = ctx.user_data["new"]
        new["link"] = t
        pid = str(DB["next_id"])
        DB["next_id"] += 1
        DB["plans"][pid] = new
        save_db()
        ctx.user_data.clear()
        await update.message.reply_text(
            f"✅ Plan added: {new['name']} - ₹{new['price']}\nSend /start to preview.",
            reply_markup=admin_panel_kb())


async def admin_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    v = update.message.video
    if any(x["uid"] == v.file_unique_id for x in DB["demos"]):
        await update.message.reply_text("This video is already in the demo list.")
        return
    if len(DB["demos"]) >= 10:
        await update.message.reply_text("Maximum 10 demo videos reached. Delete one from /admin first.")
        return
    DB["demos"].append({"uid": v.file_unique_id, "file_id": v.file_id,
                        "caption": update.message.caption or ""})
    save_db()
    await update.message.reply_text(
        f"✅ Demo video added ({len(DB['demos'])}/10). Send more videos to add them, "
        "or /start to preview.")


async def admin_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not (doc.file_name or "").endswith(".json"):
        return
    try:
        raw = await (await doc.get_file()).download_as_bytearray()
        d = json.loads(bytes(raw))
        assert isinstance(d.get("plans", {}), dict)
    except Exception:
        await update.message.reply_text("❌ That file is not a valid backup.")
        return
    DB.clear()
    DB.update(normalize(d))
    save_db()
    await update.message.reply_text(
        f"✅ Backup restored: {len(DB['plans'])} plan(s), {len(DB['demos'])} demo video(s).")


# ---------- health server for Render ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def run_health_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Health).serve_forever()


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    adm = filters.User(user_id=ADMIN_ID)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_paid, pattern=r"^paid$"))
    app.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^(adm|del|dv):"))
    app.add_handler(MessageHandler(filters.VIDEO & adm, admin_video))
    app.add_handler(MessageHandler(filters.Document.ALL & adm, admin_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & adm, admin_text))
    app.run_polling()


if __name__ == "__main__":
    main()
