import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from html import escape as esc
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from logic import EXPENSE_CATS, bar, fmt, parse_entry

logging.basicConfig(level=logging.INFO)
DB_PATH = os.getenv("DB_PATH", "moneta.db")
DEFAULT_TZ = "Europe/Kyiv"

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, currency TEXT DEFAULT '₴', limit_amt REAL DEFAULT 0,
  remind INTEGER DEFAULT 1, tz TEXT DEFAULT 'Europe/Kyiv', last_remind TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, amount REAL,
  category TEXT, note TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS ix_entries ON entries(user_id, ts);
""")

for _col in ("joined", "last_seen"):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col} INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass

router = Router()

MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🕘 История")],
    [KeyboardButton(text="🎯 Лимит"), KeyboardButton(text="⚙️ Настройки")],
])

WELCOME = (
    "👋 Привет! Я <b>Монетка</b> — помогу вести траты без таблиц и лишних кнопок.\n\n"
    "Просто пишите мне как другу:\n"
    "• <code>кофе 150</code> — запишу трату и сам определю категорию\n"
    "• <code>такси 320</code>, <code>аренда 12к</code>\n"
    "• <code>+30000 зарплата</code> — это доход\n\n"
    "Дальше я покажу статистику, предупрежу о лимите и вечером напомню записать траты.\n"
    "Команды: /stats /history /limit /export /currency /remind /tz"
)


# ---------- helpers ----------
def user(uid):
    now = int(datetime.now().timestamp())
    db.execute("INSERT OR IGNORE INTO users(id, joined) VALUES(?, ?)", (uid, now))
    db.execute("UPDATE users SET last_seen=? WHERE id=?", (now, uid))
    db.commit()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def zone(u):
    try:
        return ZoneInfo(u["tz"])
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def since(u, period):
    now = datetime.now(zone(u))
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return 0
    return int(start.timestamp())


def month_spent(u):
    r = db.execute("SELECT COALESCE(SUM(amount),0) FROM entries WHERE user_id=? AND kind='expense' AND ts>=?",
                   (u["id"], since(u, "month"))).fetchone()
    return r[0]


def get_entry(eid, uid):
    return db.execute("SELECT * FROM entries WHERE id=? AND user_id=?", (eid, uid)).fetchone()


def entry_text(r, cur):
    sign = "+" if r["kind"] == "income" else "−"
    return f"✅ <b>{sign}{fmt(r['amount'])} {esc(cur)}</b> · {esc(r['note'])}\n{r['category']}"


def entry_kb(r):
    row = []
    if r["kind"] == "expense":
        row.append(InlineKeyboardButton(text="🗂 Категория", callback_data=f"chg:{r['id']}"))
    row.append(InlineKeyboardButton(text="❌ Удалить", callback_data=f"del:{r['id']}:a"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def cat_kb(eid):
    btns = [InlineKeyboardButton(text=c, callback_data=f"cat:{eid}:{i}") for i, c in enumerate(EXPENSE_CATS)]
    return InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)])


def stats_text(u, period):
    names = {"week": "за неделю", "month": "за месяц", "all": "за всё время"}
    rows = db.execute("SELECT kind, category, SUM(amount) s FROM entries WHERE user_id=? AND ts>=? "
                      "GROUP BY kind, category ORDER BY s DESC", (u["id"], since(u, period))).fetchall()
    cur = esc(u["currency"])
    inc = sum(r["s"] for r in rows if r["kind"] == "income")
    exp = [r for r in rows if r["kind"] == "expense"]
    spent = sum(r["s"] for r in exp)
    if not rows:
        return f"📊 Пока пусто {names[period]}. Напишите, например: <code>кофе 150</code>"
    out = [f"📊 <b>Статистика {names[period]}</b>",
           f"💰 Доходы: {fmt(inc)} {cur}", f"💸 Расходы: {fmt(spent)} {cur}",
           f"⚖️ Баланс: {fmt(inc - spent)} {cur}"]
    if exp:
        out.append("\n<b>Куда уходят деньги:</b>")
        for r in exp:
            p = r["s"] / spent * 100
            out.append(f"{r['category']} — {fmt(r['s'])} {cur}\n<code>{bar(p)}</code> {p:.0f}%")
    if period == "month" and u["limit_amt"] > 0:
        out.append(f"\n🎯 Лимит: {fmt(spent)} / {fmt(u['limit_amt'])} {cur} ({spent / u['limit_amt'] * 100:.0f}%)")
    return "\n".join(out)


def period_kb(cur):
    items = (("week", "Неделя"), ("month", "Месяц"), ("all", "Всё время"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=("• " if p == cur else "") + n, callback_data=f"st:{p}") for p, n in items]])


def history_view(u):
    rows = db.execute("SELECT * FROM entries WHERE user_id=? ORDER BY id DESC LIMIT 10", (u["id"],)).fetchall()
    if not rows:
        return "🕘 Записей пока нет.", None
    cur = esc(u["currency"])
    lines = ["🕘 <b>Последние записи</b>"]
    for r in rows:
        sign = "+" if r["kind"] == "income" else "−"
        d = datetime.fromtimestamp(r["ts"], zone(u)).strftime("%d.%m")
        lines.append(f"#{r['id']} {d} {sign}{fmt(r['amount'])} {cur} · {esc(r['note'])} · {r['category']}")
    btns = [InlineKeyboardButton(text=f"❌ {r['id']}", callback_data=f"del:{r['id']}:h") for r in rows]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 5] for i in range(0, len(btns), 5)])


# ---------- commands ----------
@router.message(CommandStart())
@router.message(Command("help"))
async def start(m: Message):
    user(m.from_user.id)
    await m.answer(WELCOME, reply_markup=MENU)


@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def stats(m: Message):
    u = user(m.from_user.id)
    await m.answer(stats_text(u, "month"), reply_markup=period_kb("month"))


@router.callback_query(F.data.startswith("st:"))
async def stats_cb(c: CallbackQuery):
    u = user(c.from_user.id)
    p = c.data.split(":")[1]
    try:
        await c.message.edit_text(stats_text(u, p), reply_markup=period_kb(p))
    except Exception:
        pass
    await c.answer()


@router.message(Command("history"))
@router.message(F.text == "🕘 История")
async def history(m: Message):
    text, kb = history_view(user(m.from_user.id))
    await m.answer(text, reply_markup=kb)


@router.message(Command("limit"))
@router.message(F.text == "🎯 Лимит")
async def limit(m: Message, command: CommandObject = None):
    u = user(m.from_user.id)
    arg = command.args.strip().lower() if command and command.args else ""
    if arg:
        if arg in ("0", "off", "выкл"):
            val = 0.0
        else:
            parsed = parse_entry(arg)
            val = parsed[1] if parsed else None
        if val is None:
            await m.answer("Напишите число, например: <code>/limit 30000</code> (или <code>/limit off</code>).")
            return
        db.execute("UPDATE users SET limit_amt=? WHERE id=?", (val, u["id"]))
        db.commit()
        await m.answer("🎯 Лимит выключен." if val == 0 else
                       f"🎯 Лимит на месяц: <b>{fmt(val)} {esc(u['currency'])}</b>. Предупрежу на 80% и при превышении.")
        return
    cur = esc(u["currency"])
    now = f"Сейчас: {fmt(u['limit_amt'])} {cur}" if u["limit_amt"] > 0 else "Сейчас лимит не задан"
    await m.answer(f"🎯 Месячный лимит расходов. {now}.\nЗадать: <code>/limit 30000</code>")


@router.message(Command("currency"))
async def currency(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg or len(arg) > 4:
        await m.answer("Укажите символ валюты, например: <code>/currency $</code> или <code>/currency грн</code>")
        return
    user(m.from_user.id)
    db.execute("UPDATE users SET currency=? WHERE id=?", (arg, m.from_user.id))
    db.commit()
    await m.answer(f"Готово, валюта: {esc(arg)}")


@router.message(Command("remind"))
async def remind(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await m.answer("Вечернее напоминание в 21:00: <code>/remind on</code> или <code>/remind off</code>")
        return
    user(m.from_user.id)
    db.execute("UPDATE users SET remind=? WHERE id=?", (1 if arg == "on" else 0, m.from_user.id))
    db.commit()
    await m.answer("⏰ Напоминание включено." if arg == "on" else "🔕 Напоминание выключено.")


@router.message(Command("tz"))
async def tz(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    try:
        ZoneInfo(arg)
    except Exception:
        await m.answer("Укажите часовой пояс, например: <code>/tz Europe/Kyiv</code> или <code>/tz Europe/Warsaw</code>")
        return
    user(m.from_user.id)
    db.execute("UPDATE users SET tz=? WHERE id=?", (arg, m.from_user.id))
    db.commit()
    await m.answer(f"🕘 Часовой пояс: {esc(arg)}")


@router.message(F.text == "⚙️ Настройки")
async def settings(m: Message):
    u = user(m.from_user.id)
    await m.answer(
        "⚙️ <b>Настройки</b>\n"
        f"Валюта: {esc(u['currency'])} — <code>/currency $</code>\n"
        f"Лимит: {fmt(u['limit_amt']) if u['limit_amt'] > 0 else 'не задан'} — <code>/limit 30000</code>\n"
        f"Напоминание в 21:00: {'вкл' if u['remind'] else 'выкл'} — <code>/remind on|off</code>\n"
        f"Часовой пояс: {esc(u['tz'])} — <code>/tz Europe/Kyiv</code>\n"
        "Выгрузка в таблицу: /export")


@router.message(Command("export"))
async def export(m: Message):
    u = user(m.from_user.id)
    rows = db.execute("SELECT * FROM entries WHERE user_id=? ORDER BY ts", (u["id"],)).fetchall()
    if not rows:
        await m.answer("Пока нечего выгружать.")
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "type", "amount", "category", "note"])
    for r in rows:
        w.writerow([datetime.fromtimestamp(r["ts"], zone(u)).strftime("%Y-%m-%d %H:%M"),
                    r["kind"], r["amount"], r["category"], r["note"]])
    await m.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="moneta.csv"))


@router.message(Command("admin"))
async def admin(m: Message):
    # Статистика только для владельца: задайте ADMIN_ID (ваш Telegram ID) в переменных окружения.
    if str(m.from_user.id) != os.getenv("ADMIN_ID", ""):
        return
    now = int(datetime.now().timestamp())

    def count(sql, *args):
        return db.execute(sql, args).fetchone()[0]

    total = count("SELECT COUNT(*) FROM users")
    await m.answer(
        "📈 <b>Статистика бота</b>\n"
        f"👥 Всего пользователей: {total}\n"
        f"🆕 Новых за 24 часа: {count('SELECT COUNT(*) FROM users WHERE joined>=?', now - 86400)}\n"
        f"🆕 Новых за 7 дней: {count('SELECT COUNT(*) FROM users WHERE joined>=?', now - 7 * 86400)}\n"
        f"🆕 Новых за 30 дней: {count('SELECT COUNT(*) FROM users WHERE joined>=?', now - 30 * 86400)}\n"
        f"🔥 Активных за 24 часа: {count('SELECT COUNT(*) FROM users WHERE last_seen>=?', now - 86400)}\n"
        f"🔥 Активных за 7 дней: {count('SELECT COUNT(*) FROM users WHERE last_seen>=?', now - 7 * 86400)}\n"
        f"🧾 Всего записей о тратах: {count('SELECT COUNT(*) FROM entries')}")


# ---------- entry callbacks ----------
@router.callback_query(F.data.startswith("chg:"))
async def chg(c: CallbackQuery):
    eid = int(c.data.split(":")[1])
    if get_entry(eid, c.from_user.id):
        await c.message.edit_reply_markup(reply_markup=cat_kb(eid))
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def setcat(c: CallbackQuery):
    _, eid, idx = c.data.split(":")
    r = get_entry(int(eid), c.from_user.id)
    if r and 0 <= int(idx) < len(EXPENSE_CATS):
        db.execute("UPDATE entries SET category=? WHERE id=?", (EXPENSE_CATS[int(idx)], r["id"]))
        db.commit()
        r = get_entry(r["id"], c.from_user.id)
        await c.message.edit_text(entry_text(r, user(c.from_user.id)["currency"]), reply_markup=entry_kb(r))
    await c.answer("Категория изменена")


@router.callback_query(F.data.startswith("del:"))
async def delete(c: CallbackQuery):
    _, eid, where = c.data.split(":")
    u = user(c.from_user.id)
    if get_entry(int(eid), u["id"]):
        db.execute("DELETE FROM entries WHERE id=?", (int(eid),))
        db.commit()
    if where == "h":
        text, kb = history_view(u)
        try:
            await c.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    else:
        await c.message.edit_text("🗑 Запись удалена.")
    await c.answer("Удалено")


# ---------- add entry (plain text) ----------
@router.message(F.text & ~F.text.startswith("/"))
async def add_entry(m: Message):
    u = user(m.from_user.id)
    if len(m.text) > 200:
        await m.answer("Слишком длинно. Напишите коротко: <code>кофе 150</code>")
        return
    p = parse_entry(m.text)
    if not p:
        await m.answer("Не нашёл сумму 🤔 Напишите, например: <code>кофе 150</code> или <code>+30000 зарплата</code>")
        return
    kind, amount, note, cat = p
    cur = db.execute("INSERT INTO entries(user_id, kind, amount, category, note, ts) VALUES(?,?,?,?,?,?)",
                     (u["id"], kind, amount, cat, note, int(datetime.now().timestamp())))
    db.commit()
    r = get_entry(cur.lastrowid, u["id"])
    text = entry_text(r, u["currency"])
    if kind == "expense" and u["limit_amt"] > 0:
        pct = month_spent(u) / u["limit_amt"] * 100
        if pct >= 100:
            text += f"\n\n🚨 Лимит на месяц превышен ({pct:.0f}%)."
        elif pct >= 80:
            text += f"\n\n⚠️ Использовано {pct:.0f}% месячного лимита."
    await m.answer(text, reply_markup=entry_kb(r))


# ---------- evening reminder ----------
async def reminder_loop(bot: Bot):
    while True:
        await asyncio.sleep(60)
        try:
            for u in db.execute("SELECT * FROM users WHERE remind=1").fetchall():
                now = datetime.now(zone(u))
                today = now.strftime("%Y-%m-%d")
                if now.hour != 21 or u["last_remind"] == today:
                    continue
                db.execute("UPDATE users SET last_remind=? WHERE id=?", (today, u["id"]))
                db.commit()
                try:
                    await bot.send_message(u["id"], "🌙 Как прошёл день? Запишите траты, пока не забыли — "
                                                    "например: <code>ужин 280</code>")
                except TelegramForbiddenError:
                    db.execute("UPDATE users SET remind=0 WHERE id=?", (u["id"],))
                    db.commit()
                except Exception:
                    logging.exception("reminder failed")
        except Exception:
            logging.exception("reminder loop error")


async def start_web():
    # Мини-сервер для хостингов типа Render Web Service: им нужен открытый порт.
    port = int(os.getenv("PORT", "0"))
    if not port:
        return None

    async def ok(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main():
    await start_web()
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="history", description="Последние записи"),
        BotCommand(command="limit", description="Месячный лимит"),
        BotCommand(command="export", description="Выгрузка в CSV"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    task = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
