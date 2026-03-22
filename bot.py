"""
Room Expense Tracker Bot
Telegram bot that reads receipts via Google Gemini Vision API,
logs to Google Sheets, and generates monthly PDF reports.
"""

import os
import json
import base64
import logging
from datetime import datetime

import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import cm
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
SHEETS_CRED_FILE = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")
SPREADSHEET_ID   = os.environ["SPREADSHEET_ID"]
ADMIN_CHAT_ID    = int(os.environ["ADMIN_CHAT_ID"])

genai.configure(api_key=GEMINI_API_KEY)

AWAITING_PHOTO    = 1
AWAITING_PERSONAL = 2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_sheets_client():
    creds = Credentials.from_service_account_file(SHEETS_CRED_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def get_or_create_sheet(name: str):
    gc = get_sheets_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=1000, cols=20)

def ensure_headers():
    r = get_or_create_sheet("Receipts")
    if r.row_values(1) == []:
        r.append_row(["Receipt ID","Date","Time","Store","Submitted By","Total AED","VAT AED","Month"])
    i = get_or_create_sheet("Items")
    if i.row_values(1) == []:
        i.append_row(["Receipt ID","Item Name","Category","Price AED","Personal","Submitted By","Month"])
    m = get_or_create_sheet("Roommates")
    if m.row_values(1) == []:
        m.append_row(["Name","Added By","Added On"])

def load_roommates():
    return [r["Name"] for r in get_or_create_sheet("Roommates").get_all_records()]

def save_roommate(name, added_by):
    get_or_create_sheet("Roommates").append_row([name, added_by, datetime.now().strftime("%Y-%m-%d")])

def next_receipt_id():
    rows = get_or_create_sheet("Receipts").get_all_records()
    return f"R{str(len(rows)+1).zfill(4)}"

def log_receipt(rid, date, time, store, by, total, vat, month):
    get_or_create_sheet("Receipts").append_row([rid,date,time,store,by,total,vat,month])

def log_items(rid, items, by, month):
    ws = get_or_create_sheet("Items")
    for item in items:
        ws.append_row([rid, item["name"], item["category"], item["price"],
                       "Yes" if item.get("personal") else "No", by, month])

def get_month_items(month):
    return [r for r in get_or_create_sheet("Items").get_all_records() if r["Month"]==month]

def get_month_receipts(month):
    return [r for r in get_or_create_sheet("Receipts").get_all_records() if r["Month"]==month]

# ─── GEMINI VISION ─────────────────────────────────────────────────────────────
def parse_receipt_with_gemini(image_bytes: bytes) -> dict:
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = """Analyze this receipt image and return ONLY a valid JSON object. No markdown, no backticks, no extra text.

Exact structure required:
{
  "store": "store name",
  "date": "DD-MM-YYYY",
  "time": "HH:MM",
  "total": 0.00,
  "vat": 0.00,
  "items": [
    {"name": "item name", "price": 0.00, "category": "Food & Groceries"}
  ]
}

Categories (pick one per item):
- Food & Groceries: food, eggs, milk, bread, vegetables, meat, rice, spices
- Cleaning & Hygiene: soap, shampoo, detergent, tissue, cleaning products
- Household Items: bags, containers, batteries, light bulbs
- Other: anything else

Rules:
- SKIP voided items (lines with ****Line Void****)
- Return raw JSON only, nothing else"""

    response = model.generate_content([
        prompt,
        {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode("utf-8")}
    ])
    raw = response.text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ─── PDF GENERATION ────────────────────────────────────────────────────────────
def generate_pdf_report(month, roommates, summary, items):
    filename = f"/tmp/expense_report_{month.replace(' ','_')}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Room Expense Report", ParagraphStyle(
        "T", parent=styles["Title"], fontSize=18,
        textColor=colors.HexColor("#1a1a2e"))))
    story.append(Paragraph(month, styles["Heading2"]))
    story.append(Spacer(1, 0.5*cm))

    total_shared = summary["total_shared"]
    per_person = total_shared / len(roommates) if roommates else 0

    def make_table(data, col_widths, header_color):
        t = Table(data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor(header_color)),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("GRID",(0,0),(-1,-1),0.5,colors.grey),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f5f5f5")]),
            ("FONTSIZE",(0,0),(-1,-1),10),
            ("PADDING",(0,0),(-1,-1),8),
        ]))
        return t

    story.append(Paragraph("Summary", styles["Heading3"]))
    story.append(make_table([
        ["Metric","Amount (AED)"],
        ["Total Shared Spend", f"{total_shared:.2f}"],
        ["People Splitting", str(len(roommates))],
        ["Each Person's Share", f"{per_person:.2f}"],
    ], [10*cm, 6*cm], "#1a1a2e"))
    story.append(Spacer(1,0.5*cm))

    story.append(Paragraph("By Category", styles["Heading3"]))
    cat_data = [["Category","Total (AED)"]] + [[k, f"{v:.2f}"] for k,v in summary["by_category"].items()]
    story.append(make_table(cat_data, [10*cm, 6*cm], "#2d6a4f"))
    story.append(Spacer(1,0.5*cm))

    story.append(Paragraph("Settlement", styles["Heading3"]))
    settle_data = [["Person","Paid (AED)","Share (AED)","Owes Zack (AED)"]]
    for mate in roommates:
        paid = summary["paid_by"].get(mate, 0)
        settle_data.append([mate, f"{paid:.2f}", f"{per_person:.2f}", f"{max(per_person-paid,0):.2f}"])
    story.append(make_table(settle_data, [5*cm,4*cm,4*cm,4*cm], "#c9184a"))
    story.append(Spacer(1,0.5*cm))

    story.append(Paragraph("Full Item List (Shared Only)", styles["Heading3"]))
    item_data = [["Receipt","Item","Category","Price","By"]]
    for item in items:
        if item["Personal"] == "No":
            item_data.append([
                str(item.get("Receipt ID",""))[:6],
                str(item["Item Name"])[:35],
                str(item["Category"]),
                f"{float(item['Price AED']):.2f}",
                str(item["Submitted By"])
            ])
    t = Table(item_data, colWidths=[2.5*cm,6.5*cm,3.5*cm,2.5*cm,2.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#4a4e69")),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("GRID",(0,0),(-1,-1),0.3,colors.grey),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f5f5f5")]),
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("PADDING",(0,0),(-1,-1),5),
    ]))
    story.append(t)
    doc.build(story)
    return filename

# ─── SUMMARY CALCULATOR ────────────────────────────────────────────────────────
def calculate_monthly_summary(month):
    items = get_month_items(month)
    shared = [i for i in items if i["Personal"]=="No"]
    total_shared = sum(float(i["Price AED"]) for i in shared)
    by_category = {}
    paid_by = {}
    for i in shared:
        by_category[i["Category"]] = by_category.get(i["Category"],0) + float(i["Price AED"])
        paid_by[i["Submitted By"]] = paid_by.get(i["Submitted By"],0) + float(i["Price AED"])
    return {"total_shared": total_shared, "by_category": by_category, "paid_by": paid_by}

# ─── BOT HANDLERS ──────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 *Room Expense Tracker*\n\n"
        "📷 `/add` — Scan a receipt\n"
        "👥 `/addmate [name]` — Add roommate\n"
        "👥 `/mates` — List roommates\n"
        "📊 `/summary` — This month's spending\n"
        "💸 `/owe` — Who owes what\n"
        "📋 `/history` — Your receipts\n"
        "📄 `/report` — Generate PDF now\n\n"
        "Start with `/add` and send a photo! 📸",
        parse_mode="Markdown")

async def addmate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Only Zack can add roommates.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/addmate Ahmed`", parse_mode="Markdown")
        return
    name = " ".join(context.args).strip()
    save_roommate(name, update.effective_user.first_name)
    await update.message.reply_text(f"✅ *{name}* added!", parse_mode="Markdown")

async def mates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    roommates = load_roommates()
    if not roommates:
        await update.message.reply_text("No roommates yet. Use `/addmate [name]`", parse_mode="Markdown")
        return
    await update.message.reply_text(
        "👥 *Roommates:*\n" + "\n".join([f"• {m}" for m in roommates]),
        parse_mode="Markdown")

async def add_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Send me the receipt photo now.")
    return AWAITING_PHOTO

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Reading receipt with Gemini AI...")
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    try:
        data = parse_receipt_with_gemini(image_bytes)
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        await update.message.reply_text(f"❌ Could not read receipt. Try a clearer photo.")
        return ConversationHandler.END

    context.user_data["receipt"] = data
    context.user_data["submitter"] = update.effective_user.first_name

    lines = [f"{i}. {item['name']} ......... *AED {item['price']:.2f}*"
             for i, item in enumerate(data["items"], 1)]

    await update.message.reply_text(
        f"✅ *Receipt scanned!*\n"
        f"📍 {data['store']} | {data['date']} | {data['time']}\n\n"
        + "\n".join(lines) +
        f"\n\n💰 *Total: AED {data['total']:.2f}* (VAT: {data['vat']:.2f})\n\n"
        f"Reply with item numbers to mark as *PERSONAL* (e.g. `1,3`)\n"
        f"or type `none` if all shared.",
        parse_mode="Markdown")
    return AWAITING_PERSONAL

async def handle_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    data = context.user_data.get("receipt", {})
    submitter = context.user_data.get("submitter", "Unknown")
    items = data.get("items", [])

    personal_indices = set()
    if text != "none":
        try:
            personal_indices = {int(x.strip())-1 for x in text.split(",")}
        except ValueError:
            await update.message.reply_text("❌ Use numbers like `1,3` or type `none`.", parse_mode="Markdown")
            return AWAITING_PERSONAL

    for i, item in enumerate(items):
        item["personal"] = (i in personal_indices)

    month = datetime.now().strftime("%B %Y")
    rid = next_receipt_id()
    log_receipt(rid, data["date"], data["time"], data["store"],
                submitter, data["total"], data["vat"], month)
    log_items(rid, items, submitter, month)

    shared = sum(i["price"] for i in items if not i.get("personal"))
    personal = sum(i["price"] for i in items if i.get("personal"))

    await update.message.reply_text(
        f"✅ *Logged! Receipt {rid}*\n\n"
        f"🤝 Shared: *AED {shared:.2f}*\n"
        f"👤 Personal: *AED {personal:.2f}*\n\n"
        f"Use /summary to see totals.",
        parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = datetime.now().strftime("%B %Y")
    s = calculate_monthly_summary(month)
    roommates = load_roommates()
    n = len(roommates) if roommates else 1
    cats = "\n".join([f"  • {k}: AED {v:.2f}" for k,v in s["by_category"].items()])
    await update.message.reply_text(
        f"📊 *{month} Summary*\n\n"
        f"💰 Total Shared: *AED {s['total_shared']:.2f}*\n"
        f"👥 Split among {n} people\n"
        f"🔢 Each share: *AED {s['total_shared']/n:.2f}*\n\n"
        f"📦 By Category:\n{cats}",
        parse_mode="Markdown")

async def owe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = datetime.now().strftime("%B %Y")
    s = calculate_monthly_summary(month)
    roommates = load_roommates()
    n = len(roommates) if roommates else 1
    per_person = s["total_shared"] / n
    lines = []
    for mate in roommates:
        owes = per_person - s["paid_by"].get(mate, 0)
        lines.append(f"• {mate} owes you: *AED {owes:.2f}*" if owes > 0 else f"• {mate}: ✅ settled")
    await update.message.reply_text(
        f"💸 *Who Owes What — {month}*\n\n" + ("\n".join(lines) if lines else "No data yet."),
        parse_mode="Markdown")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = datetime.now().strftime("%B %Y")
    submitter = update.effective_user.first_name
    receipts = [r for r in get_month_receipts(month) if r["Submitted By"]==submitter]
    if not receipts:
        await update.message.reply_text("No receipts from you this month yet.")
        return
    lines = [f"• {r['Receipt ID']} | {r['Store']} | AED {r['Total AED']} | {r['Date']}" for r in receipts]
    await update.message.reply_text(
        f"📋 *Your receipts — {month}*\n\n" + "\n".join(lines),
        parse_mode="Markdown")

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_monthly_report(context.application)
    await update.message.reply_text("📄 Report generated and sent!")

async def send_monthly_report(app):
    month = datetime.now().strftime("%B %Y")
    roommates = load_roommates()
    s = calculate_monthly_summary(month)
    items = get_month_items(month)
    pdf_path = generate_pdf_report(month, roommates, s, items)
    n = len(roommates) if roommates else 1
    per_person = s["total_shared"] / n
    cats = "\n".join([f"  {k}: AED {v:.2f}" for k,v in s["by_category"].items()])
    settle = "\n".join([f"• {m}: owes *AED {max(per_person-s['paid_by'].get(m,0),0):.2f}*" for m in roommates])
    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏠 *ROOM EXPENSES — {month.upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 *Total Shared:* AED {s['total_shared']:.2f}\n"
        f"👥 Split among {n} people\n"
        f"🔢 Each share: AED {per_person:.2f}\n\n"
        f"📊 *By Category:*\n{cats}\n\n"
        f"💸 *Settlement:*\n{settle}\n\n"
        f"📎 Full PDF attached."
    )
    await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode="Markdown")
    with open(pdf_path, "rb") as f:
        await app.bot.send_document(chat_id=ADMIN_CHAT_ID, document=f,
                                     filename=f"Expenses_{month}.pdf")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    ensure_headers()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_receipt)],
        states={
            AWAITING_PHOTO:    [MessageHandler(filters.PHOTO, handle_photo)],
            AWAITING_PERSONAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_personal)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addmate", addmate))
    app.add_handler(CommandHandler("mates", mates))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("owe", owe))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(conv)

    scheduler = AsyncIOScheduler(timezone="Asia/Dubai")
    scheduler.add_job(send_monthly_report, "cron", day="last", hour=20, minute=0, args=[app])
    scheduler.start()

    logger.info("✅ Room Expense Bot is running!")
    app.run_polling()

if __name__ == "__main__":
    main()
