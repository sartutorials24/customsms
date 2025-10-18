#!/usr/bin/env python3
"""
Telegram bot wrapper for the SMS sender CLI.

Features:
- /start - info
- /help  - usage
- /send <number> | <message>  (single-line quick send using '|' to separate)
- Interactive mode: /send (then bot asks for number and message)
- Logs to sent.log (UTC ISO timestamps)
- Basic per-user rate-limiting (DEFAULT_DELAY between sends)
"""

import os
import sys
import asyncio
from datetime import datetime
from urllib.parse import quote_plus

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ----------------- USER CONFIGURATION -----------------
# You asked to include this token — it's placed here:
BOT_TOKEN = "8476752065:AAFNTWXC4Lbi9VSuUEj1RCyXV1zMQVWJyRw"

# SMS API template from your script:
BASE_URL_TEMPLATE = "https://sms-spoofer.itxkaal.workers.dev/?mo={number}&text={message}"

# Settings
TIMEOUT = 30                 # requests timeout
DEFAULT_DELAY = 1.0          # minimum seconds between sends from same user
LOG_FILE = "sent.log"
# -----------------------------------------------------

# Conversation states
STATE_NUMBER, STATE_MESSAGE = range(2)

# In-memory rate-limiter: maps user_id -> last_send_time (timestamp)
_last_send_at = {}

# ---------------- Banner (optional) ----------------
BANNER = r"""
____  ___       _________   _____    _________
\   \/  /      /   _____/  /     \  /   _____/
 \     /       \_____  \  /  \ /  \ \_____  \ 
 /     \       /        \/    Y    \/        \
/___/\  \_____/_______  /\____|__  /_______  /
      \_/_____/       \/         \/        \/

POWERD BY Hackerhacked
"""
# --------------------------------------------------


def build_url(number: str, message: str) -> str:
    enc_num = quote_plus(number.strip(), safe="")
    enc_msg = quote_plus(message, safe="")
    return BASE_URL_TEMPLATE.replace("{number}", enc_num).replace("{message}", enc_msg)


def log_entry(number: str, message: str, status_code: int, resp_text: str):
    now = datetime.utcnow().isoformat() + "Z"
    short_resp = (resp_text.replace("\n", " "))[:500]
    line = f"{now}\t{number}\t{status_code}\t{short_resp}\t{message}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def send_once_sync(number: str, message: str):
    """
    Blocking function that performs the HTTP request and returns (success:bool, status_code:int, resp_text:str)
    Intended to be run in a thread executor from async code.
    """
    url = build_url(number, message)
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        log_entry(number, message, 0, f"RequestException: {e}")
        return False, 0, f"RequestException: {e}"
    text = (resp.text or "").strip()
    log_entry(number, message, resp.status_code, text)
    success = 200 <= resp.status_code < 300
    return success, resp.status_code, text


def banner_text() -> str:
    return BANNER


# ----------------- Telegram handlers -----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "SMS Sender Bot (wrapped from your CLI)\n\n"
        "Usage:\n"
        "• /send number | message  — quick single-line send (use '|' to separate number and message)\n"
        "• /send                 — interactive: bot will ask for number then message\n"
        "• /help                 — show help\n\n"
        "Example: /send +88017XXXXXXXX | Hello there!\n\n"
        "Note: This bot will make HTTP GET requests to the configured API.\n"
        "Sends are logged to sent.log (UTC timestamps)."
    )
    # send banner (plain)
    await update.message.reply_text(banner_text())
    await update.message.reply_text(msg)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_handler(update, context)


async def send_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick /send <number> | <message> style calls"""
    if update.message is None:
        return

    args = update.message.text.partition(" ")[2].strip()
    if not args:
        # start interactive flow
        await update.message.reply_text("Enter the phone number to send to (e.g. 8436764239 or +88017...):")
        return await ConversationHandler.END if False else await start_number_prompt(update, context)
    # attempt to split by '|' first, otherwise by newline
    if '|' in args:
        number, _, message = args.partition('|')
    elif '\n' in args:
        parts = args.split('\n', 1)
        number = parts[0]
        message = parts[1] if len(parts) > 1 else ""
    else:
        # no separator, treat first token as number, rest as message
        tokens = args.split(" ", 1)
        number = tokens[0]
        message = tokens[1] if len(tokens) > 1 else ""

    number = number.strip()
    message = message.strip()
    if number == "" or message == "":
        await update.message.reply_text("Invalid quick command format. Example:\n/send +88017XXXXXXX | Hello there!")
        return

    # rate-limit check
    user_id = update.effective_user.id
    last = _last_send_at.get(user_id, 0)
    now_ts = asyncio.get_event_loop().time()
    elapsed = now_ts - last
    if elapsed < DEFAULT_DELAY:
        wait = DEFAULT_DELAY - elapsed
        await update.message.reply_text(f"You're sending too fast. Please wait {wait:.1f}s.")
        return

    await update.message.reply_text(f"Sending to {number}...")
    # run send in thread
    success, status_code, resp_text = await asyncio.get_event_loop().run_in_executor(None, send_once_sync, number, message)
    _last_send_at[user_id] = asyncio.get_event_loop().time()
    if success:
        await update.message.reply_text(f"Likely successful (HTTP {status_code}).")
        if resp_text:
            snippet = resp_text if len(resp_text) < 1000 else resp_text[:1000]
            await update.message.reply_text(f"Response:\n{snippet}")
    else:
        await update.message.reply_text(f"Send failed (HTTP {status_code}). Response/error:\n{resp_text}")


# ---- Conversation-based send (ask number then message) ----
async def start_number_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please enter the phone number (or send empty to cancel):")
    return STATE_NUMBER


async def number_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    number = update.message.text.strip()
    if number == "":
        await update.message.reply_text("Empty number — cancelled.")
        return ConversationHandler.END
    context.user_data["pending_number"] = number
    await update.message.reply_text("Now send the message text you want to send:")
    return STATE_MESSAGE


async def message_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()
    if message == "":
        await update.message.reply_text("Empty message — cancelled.")
        return ConversationHandler.END
    number = context.user_data.get("pending_number")
    if not number:
        await update.message.reply_text("Number missing — cancelled.")
        return ConversationHandler.END

    # rate-limit check
    user_id = update.effective_user.id
    last = _last_send_at.get(user_id, 0)
    now_ts = asyncio.get_event_loop().time()
    elapsed = now_ts - last
    if elapsed < DEFAULT_DELAY:
        wait = DEFAULT_DELAY - elapsed
        await update.message.reply_text(f"You're sending too fast. Please wait {wait:.1f}s.")
        return ConversationHandler.END

    await update.message.reply_text(f"Sending to {number}...")
    success, status_code, resp_text = await asyncio.get_event_loop().run_in_executor(None, send_once_sync, number, message)
    _last_send_at[user_id] = asyncio.get_event_loop().time()
    if success:
        await update.message.reply_text(f"Likely successful (HTTP {status_code}).")
        if resp_text:
            snippet = resp_text if len(resp_text) < 1000 else resp_text[:1000]
            await update.message.reply_text(f"Response:\n{snippet}")
    else:
        await update.message.reply_text(f"Send failed (HTTP {status_code}). Response/error:\n{resp_text}")

    # cleanup
    context.user_data.pop("pending_number", None)
    return ConversationHandler.END


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# --------------- Main -------------------
def main():
    # check token set
    token = BOT_TOKEN.strip()
    if not token or token == "<YOUR_TOKEN_HERE>":
        print("Bot token not configured. Edit the script to add BOT_TOKEN.")
        sys.exit(1)

    app = ApplicationBuilder().token(token).build()

    # Conversation handler for interactive flow
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("send", start_number_prompt, filters=None)],
        states={
            STATE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, number_received)],
            STATE_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, message_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        allow_reentry=True,
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    # Add a special /send handler that tries quick parsing first:
    app.add_handler(CommandHandler("send", send_quick))
    # Add the conversation handler (this will also match /send with no args)
    app.add_handler(conv_handler)

    # Fallback text handler to let users know about /send
    async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Unknown input. Use /send to send an SMS or /help for usage.")

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    print("Bot starting polling... (Ctrl+C to stop)")
    app.run_polling()


if __name__ == "__main__":
    main()
