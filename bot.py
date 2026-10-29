#!/usr/bin/env python3
"""DARK_CYBER Number Info Bot — menu UI, credits, referral, history."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import phonenumbers
from phonenumbers import Leniency, PhoneNumberMatcher, carrier, geocoder, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

BASE = Path(__file__).resolve().parent
CONF = BASE / "config.json"
DB = Path(os.getenv("DB_PATH", str(BASE / "numinfo.db")))

DEFAULTS = {
    "brand": "DARK_CYBER",
    "owner": "@Darkxshadow13",
    "admin_contact": "@Darkxshadow13",
    "youtube_url": "https://youtube.com/@xdark_cyber?si=GCip_sQPOeUxpZmJ",
    "channel": "OFFICIAL CHANNEL",
    "force_chat": "",
    "force_chat_url": "",
    "bot_username": "",
    "send_mode": "pre",
    "free_credits": 3,
    "ref_per_credit": 3,
    "cooldown": 3,
    "cache_ttl": 21600,
    "max_results": 3,
    "region": "IN",
    "tz_name": "Asia/Kolkata",
    "tz_label": "IST",
    "mode": "webhook",
    "secret": "numinfo",
    "admins": [],
    "providers": [
        {"name": "phantom", "url": "https://phantom-osint.unknowns.app/api/v1/lookup",
         "method": "POST", "num_param": "query", "format": "results",
         "key": "", "key_param": "X-API-Key", "key_in": "header",
         "extra": {"search_mode": "auto"}},
    ],
}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("numinfo")


def save_cfg(cfg: dict) -> None:
    try:
        CONF.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception:
        pass


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    if CONF.exists():
        try:
            cfg.update(json.loads(CONF.read_text()))
        except Exception as e:
            log.warning("config.json: %s", e)

    for key in list(cfg.keys()):
        if key in ("providers", "admins"):
            continue
        v = os.getenv(key.upper())
        if not v:
            continue
        if isinstance(cfg[key], int):
            try:
                v = int(v)
            except ValueError:
                continue
        cfg[key] = v

    raw = os.getenv("ADMINS", "").strip()
    if raw:
        cfg["admins"] = [int(x) for x in re.split(r"[,\s]+", raw) if x.isdigit()]

    raw = os.getenv("PROVIDERS", "").strip()
    if raw:
        try:
            cfg["providers"] = json.loads(raw)
        except Exception:
            log.warning("PROVIDERS JSON galat hai")

    if not cfg.get("token"):
        tok = os.getenv("BOT_TOKEN", "").strip()
        if not tok:
            try:
                tok = input("BotFather token: ").strip()
            except EOFError:
                tok = ""
        if not tok:
            raise SystemExit("BOT_TOKEN nahi mila.")
        cfg["token"] = tok
        save_cfg(cfg)
    return cfg


CFG = load_cfg()
TOKEN = CFG["token"]


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS records(
            number TEXT PRIMARY KEY, payload TEXT, source TEXT, updated REAL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT 0, ref_count INTEGER DEFAULT 0,
            searches INTEGER DEFAULT 0, created REAL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            number TEXT, ts REAL)""")
        for col, ddl in (("credits", "INTEGER DEFAULT 0"),
                         ("referred_by", "INTEGER DEFAULT 0"),
                         ("ref_count", "INTEGER DEFAULT 0"),
                         ("searches", "INTEGER DEFAULT 0"),
                         ("created", "REAL DEFAULT 0")):
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass


def db_put(number: str, recs: list, source: str) -> None:
    with db() as con:
        con.execute("INSERT INTO records(number,payload,source,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(number) DO UPDATE SET payload=excluded.payload,"
                    "source=excluded.source,updated=excluded.updated",
                    (number, json.dumps(recs, ensure_ascii=False), source, time.time()))


def db_get(number: str, max_age=None):
    with db() as con:
        row = con.execute("SELECT payload, source, updated FROM records WHERE number=?",
                          (number,)).fetchone()
    if not row:
        return None, "", 0.0
    if max_age is not None and time.time() - row[2] > max_age:
        return None, row[1], row[2]
    return json.loads(row[0]), row[1], row[2]


def get_user(uid: int) -> dict:
    with db() as con:
        row = con.execute("SELECT credits, referred_by, ref_count FROM users WHERE user_id=?",
                          (uid,)).fetchone()
        if row is None:
            con.execute("INSERT INTO users(user_id,credits,created) VALUES(?,?,?)",
                        (uid, CFG["free_credits"], time.time()))
            return {"credits": CFG["free_credits"], "referred_by": 0, "ref_count": 0}
        return {"credits": row[0], "referred_by": row[1], "ref_count": row[2]}


def add_credits(uid: int, n: int) -> int:
    get_user(uid)
    with db() as con:
        con.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (n, uid))
        return con.execute("SELECT credits FROM users WHERE user_id=?", (uid,)).fetchone()[0]


def spend(uid: int) -> bool:
    if uid in CFG["admins"]:
        return True
    with db() as con:
        cur = con.execute("SELECT credits FROM users WHERE user_id=?", (uid,)).fetchone()
        if not cur or cur[0] <= 0:
            return False
        con.execute("UPDATE users SET credits = credits - 1, searches = searches + 1 "
                    "WHERE user_id=?", (uid,))
        return True


def refund(uid: int) -> None:
    if uid in CFG["admins"]:
        return
    with db() as con:
        con.execute("UPDATE users SET credits = credits + 1 WHERE user_id=?", (uid,))


def credits_of(uid: int) -> int:
    return -1 if uid in CFG["admins"] else get_user(uid)["credits"]


def add_history(uid: int, number: str) -> None:
    with db() as con:
        con.execute("INSERT INTO history(user_id,number,ts) VALUES(?,?,?)",
                    (uid, number, time.time()))


def get_history(uid: int, limit: int = 10) -> list:
    with db() as con:
        return con.execute("SELECT number, ts FROM history WHERE user_id=? "
                           "ORDER BY ts DESC LIMIT ?", (uid, limit)).fetchall()


# ───────────────────────────── force join
async def is_joined(bot, uid: int) -> bool:
    if not CFG["force_chat"]:
        return True
    try:
        m = await bot.get_chat_member(CFG["force_chat"], uid)
    except Exception as e:
        log.warning("get_chat_member fail: %s", e)
        return True
    if m.status in ("creator", "administrator", "member"):
        return True
    return bool(getattr(m, "is_member", False))


def join_kb() -> InlineKeyboardMarkup:
    rows = []
    if CFG["force_chat_url"]:
        rows.append([InlineKeyboardButton("📢 Join Channel", url=CFG["force_chat_url"])])
    rows.append([InlineKeyboardButton("✅ Joined — Check", callback_data="checkjoin")])
    return InlineKeyboardMarkup(rows)


JOIN_TEXT = ("👋 <b>Welcome {brand}</b>\n\n"
             "Bot use karne ke liye pehle channel join karo:\n"
             "1️⃣ Neeche 📢 Join Channel dabao\n"
             "2️⃣ Join karke wapas aao\n"
             "3️⃣ ✅ Joined — Check dabao\n\n"
             "Join ke baad <b>{free}</b> free credits milenge 🎁")


# ───────────────────────────── menu
def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Number to Info", callback_data="m:num")],
        [InlineKeyboardButton("💎 My Credits", callback_data="m:cred")],
        [InlineKeyboardButton("💳 Buy Credits", callback_data="m:buy")],
        [InlineKeyboardButton("🤝 Refer & Earn", callback_data="m:ref")],
        [InlineKeyboardButton("📜 My History", callback_data="m:hist")],
        [InlineKeyboardButton("🆔 My ID", callback_data="m:id")],
        [InlineKeyboardButton("📞 Contact Admin", callback_data="m:adm")],
        [InlineKeyboardButton("📺 YouTube", url=CFG["youtube_url"])],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="m:menu")]])


def welcome(user) -> str:
    name = html.escape(user.first_name or "User")
    return (f"👋 <b>Hello {name}!</b>\n\n"
            f"📞 <b>WELCOME TO {html.escape(CFG['brand'])} NUMBER INFORMATION BOT</b>\n\n"
            "• 🔎 Number to Info lookup (Indian mobile)\n"
            "• 💎 Check your credits balance\n"
            "• 💳 Buy Credits via UPI\n"
            f"• 🤝 Refer & Earn – get 1 credit per {CFG['ref_per_credit']} referrals\n"
            "• 📜 View your history\n"
            "• 🆔 Get your Telegram ID\n"
            "• 📞 Contact admin\n\n"
            f"<b>THANK YOU TEAM {html.escape(CFG['brand'])}</b> 😊\n\n"
            "⚠️ Use only for legal &amp; ethical purposes.\n\n"
            "Choose an option below 👇👇👇")


def ref_link(bot, uid: int) -> str:
    uname = CFG.get("bot_username") or getattr(bot, "username", "") or "your_bot"
    return f"https://t.me/{uname}?start=ref_{uid}"


# ───────────────────────────── providers
ALIASES = {
    "name": ("name", "full_name", "subscriber_name", "truecaller_name"),
    "fname": ("fname", "father_name", "fathersname", "fathername"),
    "aadhaar": ("aadhaar", "aadhar", "aadhaarnumber", "aadharnumber", "uid"),
    "mobile": ("mobile", "phonenumber", "number", "registered_mobile"),
    "alt": ("alt", "alternate", "othernumber", "alternate_number", "alt_number"),
    "circle": ("circle", "operator", "region", "regions", "state", "district",
               "town", "telecom_circle"),
    "address": ("address", "addresses", "full_address"),
    "email": ("email", "emails"),
    "type": ("line_type", "type"),
    "tz": ("timezone", "time_zone"),
}


def extract_records(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("results", "data", "result", "records"):
            v = data.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                return [v]
        if {str(x).lower() for x in data} & {"name", "fname", "aadhaar", "address"}:
            return [data]
    return []


def normalize(rec: dict) -> dict:
    low = {str(k).lower(): v for k, v in rec.items()}
    out = {}
    for canon, keys in ALIASES.items():
        for k in keys:
            v = low.get(k)
            if v not in (None, "", [], {}, "-"):
                out[canon] = ", ".join(map(str, v)) if isinstance(v, list) else str(v).strip()
                break
    return out


async def call_provider(client, p, number):
    params, headers = {}, {}
    if p.get("key"):
        if p.get("key_in") == "header":
            headers[p.get("key_param", "X-API-Key")] = p["key"]
        else:
            params[p.get("key_param", "key")] = p["key"]
    if p.get("method", "GET") == "POST":
        r = await client.post(p["url"], json={p["num_param"]: number, **(p.get("extra") or {})},
                              headers=headers)
    else:
        params[p["num_param"]] = number
        r = await client.get(p["url"], params=params, headers=headers)
    r.raise_for_status()
    return extract_records(r.json())


TYPES = {0: "Fixed line", 1: "Mobile", 2: "Fixed/Mobile", 3: "Toll free", 4: "Premium",
         5: "Shared cost", 6: "VoIP", 7: "Personal", 8: "Pager", 9: "UAN", 27: "Unknown"}


def offline(number: str, region: str) -> list:
    try:
        num = phonenumbers.parse(number, region)
    except phonenumbers.NumberParseException:
        return []
    if not phonenumbers.is_valid_number(num):
        return []
    return [{
        "mobile": phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164),
        "circle": f"{carrier.name_for_number(num, 'en') or 'Unknown'} "
                  f"{phonenumbers.region_code_for_number(num) or ''}".strip(),
        "type": TYPES.get(phonenumbers.number_type(num), "Unknown"),
        "tz": ", ".join(timezone.time_zones_for_number(num)) or "-",
    }]


async def lookup(number: str):
    c, s, _ = db_get(number, CFG["cache_ttl"])
    if c:
        return c, "cache"
    async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
        for p in CFG["providers"]:
            if not p.get("url"):
                continue
            try:
                recs = [r for r in (normalize(x) for x in await call_provider(client, p, number)) if r]
            except Exception as e:
                log.warning("provider %s fail: %s", p.get("name"), e)
                continue
            if recs:
                db_put(number, recs, p.get("name", "api"))
                return recs, p.get("name", "api")
    st, ss, _ = db_get(number)
    if st:
        return st, f"{ss} (stale)"
    off = offline(number, CFG["region"])
    return (off, "offline") if off else ([], "none")


# ───────────────────────────── render
TAG = re.compile(r"</?[bi]>")


def esc(v) -> str:
    return html.escape(str(v))


def finish(text: str) -> str:
    mode = CFG["send_mode"]
    if mode == "html":
        return text
    plain = html.unescape(TAG.sub("", text))
    return f"<pre>{html.escape(plain)}</pre>" if mode == "pre" else plain


def bars(text: str, w: int) -> tuple:
    head = f"─ {text} "
    return ("┌" + head + "─" * max(0, w - len(head)) + "┐", "└" + "─" * w + "┘")


def render(number: str, recs: list, source: str = "") -> str:
    fields = [("NAME", "name"), ("FATHER", "fname"), ("AADHAAR", "aadhaar"),
              ("MOBILE", "mobile"), ("ALT", "alt"), ("CIRCLE", "circle"),
              ("ADDRESS", "address"), ("EMAIL", "email")]
    w = max(22, len(CFG["brand"]) + 8)
    top, bot = bars(CFG["brand"].upper(), w)
    t = datetime.now(ZoneInfo(CFG["tz_name"])).strftime("%H:%M")

    o = [top, f"│ $ lookup {esc(number)}", f"│ ✓ {len(recs)} match  ·  {t}", bot]
    for i, r in enumerate(recs, 1):
        o += ["", f"┃ {i:02d}"]
        for label, key in fields:
            if r.get(key):
                o.append(f"┃ {label:<9} {esc(r[key])}")
    o += ["", "┈" * w]
    return finish("\n".join(o))


def chunks(text: str, size: int = 3800) -> list:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    return out + ([cur] if cur.strip() else [])


def pmode():
    return ParseMode.HTML if CFG["send_mode"] in ("html", "pre") else None


# ───────────────────────────── commands
_last: dict = {}


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.effective_message
    get_user(uid)

    if not CFG["admins"]:
        CFG["admins"] = [uid]
        save_cfg(CFG)
        await msg.reply_text(f"👑 DARK_CYBER (ID {uid}).")

    args = ctx.args or []
    if args and args[0].startswith("ref_"):
        try:
            rid = int(args[0][4:])
        except ValueError:
            rid = 0
        u = get_user(uid)
        if rid and rid != uid and not u["referred_by"]:
            with db() as con:
                con.execute("UPDATE users SET referred_by=? WHERE user_id=?", (rid, uid))
                con.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (rid,))
                total = con.execute("SELECT ref_count FROM users WHERE user_id=?",
                                    (rid,)).fetchone()[0]
            if total % CFG["ref_per_credit"] == 0:
                add_credits(rid, 1)
                try:
                    await ctx.bot.send_message(rid, "🎉 Referral complete! +1 credit mila.")
                except Exception:
                    pass

    if not await is_joined(ctx.bot, uid):
        await msg.reply_text(JOIN_TEXT.format(brand=CFG["brand"], free=CFG["free_credits"]),
                             parse_mode=ParseMode.HTML, reply_markup=join_kb())
        return

    await msg.reply_text(welcome(update.effective_user), parse_mode=ParseMode.HTML,
                         reply_markup=menu_kb())


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"🆔 Your Telegram ID:\n<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def cmd_addcredits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CFG["admins"]:
        return
    a = ctx.args or []
    if len(a) < 2 or not a[0].isdigit() or not a[1].lstrip("-").isdigit():
        await update.effective_message.reply_text("Format: <code>/addcredits 123456789 10</code>",
                                                 parse_mode=ParseMode.HTML)
        return
    total = add_credits(int(a[0]), int(a[1]))
    await update.effective_message.reply_text(
        f"✅ <code>{a[0]}</code> ko {a[1]} credits. Ab total: <b>{total}</b>",
        parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        u = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        r = con.execute("SELECT COALESCE(SUM(ref_count),0) FROM users").fetchone()[0]
    await update.effective_message.reply_text(
        f"👥 Users: <b>{u}</b>\n🔍 Records: <b>{n}</b>\n🤝 Referrals: <b>{r}</b>",
        parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CFG["admins"]:
        return
    parts = update.effective_message.text.split(" ", 1)
    if len(parts) < 2:
        await update.effective_message.reply_text(
            "Format:\n<code>/add 8840224499 | name=X | fname=Y | address=...</code>",
            parse_mode=ParseMode.HTML)
        return
    bits = [b.strip() for b in parts[1].split("|") if b.strip()]
    num = re.sub(r"\D", "", bits[0])[-10:]
    rec = {}
    for b in bits[1:]:
        if "=" in b:
            k, v = b.split("=", 1)
            rec[k.strip().lower()] = v.strip()
    rec.setdefault("mobile", num)
    old, _, _ = db_get(num)
    db_put(num, (old or []) + [rec], "manual")
    await update.effective_message.reply_text(f"✅ Saved <code>{num}</code>",
                                             parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CFG["admins"]:
        return
    p = update.effective_message.text.split(" ", 1)
    if len(p) < 2:
        return
    with db() as con:
        ids = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    ok = 0
    for uid in ids:
        try:
            await ctx.bot.send_message(uid, p[1], parse_mode=ParseMode.HTML)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.effective_message.reply_text(f"✅ {ok}/{len(ids)}")


# ───────────────────────────── callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = q.data or ""

    if d == "checkjoin":
        if await is_joined(ctx.bot, uid):
            await q.answer("✅ Verified!")
            await q.message.edit_text(welcome(q.from_user), parse_mode=ParseMode.HTML,
                                      reply_markup=menu_kb())
        else:
            await q.answer("Pehle channel join karo 🙂", show_alert=True)
        return

    if not await is_joined(ctx.bot, uid):
        await q.answer("Pehle channel join karo 🙂", show_alert=True)
        return

    await q.answer()

    if d == "m:menu":
        await q.message.edit_text(welcome(q.from_user), parse_mode=ParseMode.HTML,
                                  reply_markup=menu_kb())
        return

    if d == "m:num":
        ctx.user_data["await"] = True
        await q.message.reply_text(
            "📞 Send the Indian mobile number (10 digits, e.g. 9876543210).\n"
            "Each lookup costs 1 credit.", reply_markup=back_kb())
        return

    if d == "m:cred":
        c = credits_of(uid)
        u = get_user(uid)
        await q.message.reply_text(
            f"💎 <b>Credits:</b> {'∞' if c < 0 else c}\n🤝 Referrals: <b>{u['ref_count']}</b>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if d == "m:buy":
        await q.message.reply_text(
            f"💳 <b>Buy Credits via UPI</b>\n\nContact admin: {html.escape(CFG['admin_contact'])}\n\n"
            f"Kitne credits chahiye batao, admin add kar dega.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if d == "m:ref":
        u = get_user(uid)
        need = CFG["ref_per_credit"] - (u["ref_count"] % CFG["ref_per_credit"])
        await q.message.reply_text(
            f"🤝 <b>Refer & Earn</b>\n\n"
            f"Har <b>{CFG['ref_per_credit']}</b> referral pe <b>1 credit</b> milega.\n\n"
            f"Tumhara link:\n<code>{ref_link(ctx.bot, uid)}</code>\n\n"
            f"Referrals: <b>{u['ref_count']}</b>\n"
            f"Agle credit ke liye <b>{need}</b> aur chahiye.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if d == "m:hist":
        rows = get_history(uid)
        if not rows:
            await q.message.reply_text("📜 Abhi koi history nahi.", reply_markup=back_kb())
            return
        lines = [f"<code>{n}</code> — {datetime.fromtimestamp(t, ZoneInfo(CFG['tz_name'])).strftime('%d-%m %H:%M')}"
                 for n, t in rows]
        await q.message.reply_text("📜 <b>Last searches</b>\n\n" + "\n".join(lines),
                                   parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if d == "m:id":
        await q.message.reply_text(f"🆔 Your Telegram ID:\n<code>{uid}</code>",
                                   parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if d == "m:adm":
        await q.message.reply_text(
            f"📞 <b>Contact Admin</b>\n\n{html.escape(CFG['admin_contact'])}",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return


# ───────────────────────────── lookup handler
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    uid = user.id
    now = time.time()
    if now - _last.get(uid, 0) < CFG["cooldown"]:
        return
    _last[uid] = now

    get_user(uid)

    if not await is_joined(ctx.bot, uid):
        await msg.reply_text(JOIN_TEXT.format(brand=CFG["brand"], free=CFG["free_credits"]),
                             parse_mode=ParseMode.HTML, reply_markup=join_kb())
        return

    text = (msg.text or "").strip()
    try:
        found = list(PhoneNumberMatcher(text, CFG["region"], Leniency.POSSIBLE))[:CFG["max_results"]]
    except Exception:
        found = []

    if found:
        nums = [re.sub(r"\D", "", phonenumbers.format_number(
            m.number, phonenumbers.PhoneNumberFormat.NATIONAL)) for m in found]
    else:
        dg = re.sub(r"\D", "", text)
        if len(dg) < 10:
            await msg.reply_text("📞 Send the Indian mobile number (10 digits, e.g. 9876543210).",
                                 reply_markup=back_kb())
            return
        nums = [dg[-10:]]

    spent = 0
    if uid not in CFG["admins"]:
        for _ in nums:
            if spend(uid):
                spent += 1
            else:
                break
        if spent == 0:
            await msg.reply_text(
                f"💎 <b>Credits left:</b> 0\n\n"
                f"💳 Buy Credits ya 🤝 Refer & Earn se credits lo.\n"
                f"Admin: {html.escape(CFG['admin_contact'])}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Buy Credits", callback_data="m:buy")],
                    [InlineKeyboardButton("🤝 Refer & Earn", callback_data="m:ref")],
                    [InlineKeyboardButton("🔙 Menu", callback_data="m:menu")]]))
            return
        nums = nums[:spent]

    st = await msg.reply_text("🔍 <b>Searching...</b>\nPlease wait.", parse_mode=ParseMode.HTML)
    res = await asyncio.gather(*[lookup(n) for n in nums])
    blocks = [render(n, r, s) for n, (r, s) in zip(nums, res) if r]

    if not blocks:
        for _ in range(spent):
            refund(uid)
        await st.edit_text("⚠️ <b>Koi record nahi mila.</b>\nCredit refund kar diya.",
                           parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    for n in nums:
        add_history(uid, n)

    c = credits_of(uid)
    body = "\n\n".join(blocks) + (f"\n\n💎 <b>Credits left:</b> {'∞' if c < 0 else c}")
    parts = chunks(body)
    try:
        await st.edit_text(parts[0], parse_mode=pmode(), reply_markup=back_kb())
    except Exception:
        await msg.reply_text(parts[0], parse_mode=pmode(), reply_markup=back_kb())
    for p in parts[1:]:
        await msg.reply_text(p, parse_mode=pmode())


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Bot live. Mode=%s", CFG["mode"])

    if CFG["mode"] == "webhook":
        url = os.getenv("WEBHOOK_URL", "").rstrip("/")
        if url:
            app.run_webhook(listen="0.0.0.0", port=int(os.getenv("PORT", "8080")),
                            url_path=CFG["secret"], webhook_url=f"{url}/{CFG['secret']}",
                            allowed_updates=Update.ALL_TYPES)
            return
        log.error("WEBHOOK_URL nahi mila — polling fallback")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
