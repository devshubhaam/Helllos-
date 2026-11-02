"""
Telegram Course Bot + FamGateway (auto payment verification)
Install:  pip install "python-telegram-bot==21.6" httpx
Run:      python bot_famgateway.py
"""
import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

# ================== CONFIG ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
FAM_API_KEY = os.environ["FAM_API_KEY"]
FAM_BASE = "https://famgateway.in"

# key: (button name, price INR, group_chat_id or None, fallback invite link)
# group_chat_id (e.g. -1001234567890) dene ke liye bot ko us group me admin
# banao (Invite Users permission). Tab har buyer ko single-use link milega.
PLANS = {
    "g1": ("Group 1 - Candlestick Basics", 49, None, "https://t.me/+XXXXXXXX1"),
    "g2": ("Group 2 - Price Action", 99, None, "https://t.me/+XXXXXXXX2"),
    "g3": ("Group 3 - Option Trading", 149, None, "https://t.me/+XXXXXXXX3"),
    "g4": ("All Groups - Lifetime", 249, None, "https://t.me/+XXXXXXXX4"),
}
POLL_EVERY = 4        # seconds (docs: 3-5s)
ORDER_TTL = 300       # gateway order expiry = 5 min
# ============================================

logging.basicConfig(level=logging.INFO)
HEADERS = {"X-Api-Key": FAM_API_KEY, "Content-Type": "application/json"}

active = {}       # user_id -> {"order_id", "plan", "task"}
delivered = set() # order_ids already fulfilled (double-delivery guard)


async def fam_create_order(amount: int, name: str):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{FAM_BASE}/api/create-order", headers=HEADERS,
                         json={"amount": float(amount), "customer_name": name})
        return r.json()


async def fam_verify(order_id: str):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FAM_BASE}/api/verify-order.php",
                        headers=HEADERS, params={"order_id": order_id})
        return r.json()


async def fetch_qr(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=20) as c:
        return (await c.get(url)).content


def plans_keyboard():
    rows = [[InlineKeyboardButton(f"{n} - ₹{p}", callback_data=f"plan:{k}")]
            for k, (n, p, _, _) in PLANS.items()]
    return InlineKeyboardMarkup(rows)


async def get_link(ctx, plan_key: str) -> str:
    _, _, chat_id, fallback = PLANS[plan_key]
    if chat_id:
        try:
            inv = await ctx.bot.create_chat_invite_link(chat_id, member_limit=1)
            return inv.invite_link
        except Exception as e:
            logging.warning("invite link failed: %s", e)
    return fallback


async def deliver(ctx, user_id: int, order_id: str, plan_key: str, utr: str = ""):
    if order_id in delivered:
        return
    delivered.add(order_id)
    link = await get_link(ctx, plan_key)
    await ctx.bot.send_message(
        user_id,
        f"✅ Payment confirmed!\nUTR: {utr}\n\n🎓 Your access link:\n{link}\n\n"
        "Link sirf ek baar kaam karega, jaldi join karo.")
    active.pop(user_id, None)


async def poll_order(ctx, user_id: int, order_id: str, plan_key: str, price: int):
    """Background loop: har 4s verify-order check karta hai."""
    waited = 0
    try:
        while waited <= ORDER_TTL + 20:
            await asyncio.sleep(POLL_EVERY)
            waited += POLL_EVERY
            res = await fam_verify(order_id)
            status = res.get("status")
            if status == "success":
                data = res.get("data", {})
                if float(data.get("amount", 0)) >= price:
                    await deliver(ctx, user_id, order_id, plan_key, data.get("utr", ""))
                return
            if status in ("expired", "not_found"):
                break
        await ctx.bot.send_message(
            user_id, "⌛ Order expire ho gaya. Dobara /start karke plan chuno.")
        active.pop(user_id, None)
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("poll error")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n\n📈 𝗦𝗘𝗟𝗘𝗖𝗧 𝗬𝗢𝗨𝗥 𝗧𝗥𝗔𝗗𝗜𝗡𝗚 𝗚𝗥𝗢𝗨𝗣 👇",
        reply_markup=plans_keyboard())


async def on_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    key = q.data.split(":")[1]
    name, price, _, _ = PLANS[key]

    # purana pending order ho to uska polling band karo
    old = active.pop(uid, None)
    if old:
        old["task"].cancel()

    res = await fam_create_order(price, q.from_user.full_name)
    if res.get("status") != "success":
        await ctx.bot.send_message(uid, "❌ Order nahi ban paya, thodi der baad try karo.")
        logging.error("create-order failed: %s", res)
        return
    d = res["data"]
    pay_amt = d["payable_amount"]  # kabhi 49.01 bhi ho sakta hai, yahi dikhana hai

    caption = (
        f"📌 {name}\n\n"
        f"💰 Exact amount pay karo: ₹{pay_amt}\n"
        f"💳 UPI ID: {d['upi_id']}\n"
        f"📲 QR scan karke pay karo\n"
        f"⏱ 5 minute me expire hoga\n"
        f"⚡ Payment ke baad access apne aap mil jayega\n\n"
        f"⚠️ Amount bilkul wahi bhejna jo upar likha hai."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Open UPI App", url=d["checkout_url"])],
        [InlineKeyboardButton("I have paid ✅", callback_data="paid")],
        [InlineKeyboardButton("⬅ Back", callback_data="back")],
    ])
    qr_bytes = await fetch_qr(d["qr_url"])
    await ctx.bot.send_photo(uid, qr_bytes, caption=caption, reply_markup=kb)

    task = asyncio.create_task(poll_order(ctx, uid, d["order_id"], key, price))
    active[uid] = {"order_id": d["order_id"], "plan": key, "price": price, "task": task}


async def on_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    a = active.get(uid)
    if not a:
        await q.answer("Koi active order nahi. /start karo.", show_alert=True)
        return
    res = await fam_verify(a["order_id"])
    if res.get("status") == "success":
        await q.answer()
        data = res.get("data", {})
        if float(data.get("amount", 0)) >= a["price"]:
            a["task"].cancel()
            await deliver(ctx, uid, a["order_id"], a["plan"], data.get("utr", ""))
    else:
        await q.answer("Payment abhi nahi mila. Pay karke 10 sec baad dobara dabao.",
                       show_alert=True)


async def on_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    old = active.pop(q.from_user.id, None)
    if old:
        old["task"].cancel()
    await ctx.bot.send_message(q.message.chat_id,
                               "📈 𝗦𝗘𝗟𝗘𝗖𝗧 𝗬𝗢𝗨𝗥 𝗧𝗥𝗔𝗗𝗜𝗡𝗚 𝗚𝗥𝗢𝗨𝗣 👇",
                               reply_markup=plans_keyboard())


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def main():
    # Render Web Service ko open port chahiye; ye health endpoint deta hai
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_paid, pattern=r"^paid$"))
    app.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))
    app.run_polling()


if __name__ == "__main__":
    main()
