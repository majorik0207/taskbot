import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)
from telegram.constants import ParseMode

from database import Database
from scheduler import TaskScheduler

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── States for conversation ───────────────────────────────────────────────────
(TASK_TITLE, TASK_NOTE, TASK_DATE, TASK_TIME, TASK_DEADLINE,
 TASK_PRIORITY, TASK_LINK, TASK_CONFIRM,
 EDIT_CHOICE, EDIT_VALUE) = range(10)

# ─── Priority config ───────────────────────────────────────────────────────────
PRIORITY = {
    "critical": {"label": "🔴 КРИТИЧНО",   "emoji": "🔴", "name": "Критично"},
    "high":     {"label": "🟠 Высокий",     "emoji": "🟠", "name": "Высокий"},
    "medium":   {"label": "🟡 Средний",     "emoji": "🟡", "name": "Средний"},
    "low":      {"label": "🟢 Низкий",      "emoji": "🟢", "name": "Низкий"},
}

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bot.meduzacrystal.by/index.html")

db = Database("tasks.db")
scheduler: TaskScheduler = None


# ─── Helpers ───────────────────────────────────────────────────────────────────

def now() -> datetime:
    return datetime.now(TIMEZONE)


def fmt_dt(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=TIMEZONE)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_str


def task_card(task: dict, short: bool = False) -> str:
    p = PRIORITY.get(task["priority"], PRIORITY["medium"])
    status_map = {
        "pending":    "⏳ Ожидает",
        "done":       "✅ Выполнено",
        "irrelevant": "🚫 Неактуально",
    }
    status = status_map.get(task["status"], task["status"])

    lines = [
        f"{p['emoji']} <b>{task['title']}</b>",
        f"📅 Дата: <code>{fmt_dt(task['scheduled_at'])}</code>",
    ]
    if task.get("deadline"):
        lines.append(f"⏰ Дедлайн: <code>{fmt_dt(task['deadline'])}</code>")
    lines.append(f"🏷 Приоритет: {p['name']}   |   {status}")

    if not short:
        if task.get("note"):
            lines.append(f"\n📝 <i>{task['note']}</i>")
        if task.get("link"):
            lines.append(f"🔗 <a href='{task['link']}'>Ссылка</a>")
        if task.get("photo_id"):
            lines.append("🖼 Есть прикреплённое фото")

    return "\n".join(lines)


def task_keyboard(task: dict) -> InlineKeyboardMarkup:
    tid = task["id"]
    row1 = [
        InlineKeyboardButton("✅ Выполнено",    callback_data=f"done:{tid}"),
        InlineKeyboardButton("🚫 Неактуально", callback_data=f"irrelevant:{tid}"),
    ]
    row2 = [
        InlineKeyboardButton("📅 Перенести",    callback_data=f"reschedule:{tid}"),
        InlineKeyboardButton("🗑 Удалить",      callback_data=f"delete:{tid}"),
    ]
    return InlineKeyboardMarkup([row1, row2])


# ─── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.first_name)

    # Привязываем user_id к записи в team_members по username
    if user.username:
        db.link_member_user_id(user.username, user.id, user.first_name)

    await update.message.reply_text(
        f"👋 Привет, <b>{user.first_name}</b>!\n\n"
        "Здесь будут приходить уведомления о твоих задачах.\n\n"
        "Все задачи создаются и редактируются в приложении — "
        "открой его кнопкой <b>«ЗАДАЧИ»</b> рядом с полем ввода.",
        parse_mode=ParseMode.HTML,
    )


# ─── Add task flow ──────────────────────────────────────────────────────────────

async def new_task_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["task"] = {}
    await update.message.reply_text(
        "📝 <b>Создание задачи</b>\n\nВведи <b>название</b> задачи:",
        parse_mode=ParseMode.HTML,
    )
    return TASK_TITLE


async def got_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["task"]["title"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Пропустить →", callback_data="skip_note")
    ]])
    await update.message.reply_text(
        "📋 Добавь <b>примечание</b> (или пропусти):",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )
    return TASK_NOTE


async def got_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["task"]["note"] = update.message.text.strip()
    return await ask_date(update, ctx)


async def skip_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["task"]["note"] = ""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📋 Примечание пропущено.")
    return await ask_date(update, ctx)


async def ask_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = now()
    quick = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Сегодня",   callback_data=f"qdate:{today.strftime('%d.%m.%Y')}"),
            InlineKeyboardButton("Завтра",    callback_data=f"qdate:{(today + timedelta(days=1)).strftime('%d.%m.%Y')}"),
        ],
        [
            InlineKeyboardButton("+2 дня",   callback_data=f"qdate:{(today + timedelta(days=2)).strftime('%d.%m.%Y')}"),
            InlineKeyboardButton("+7 дней",  callback_data=f"qdate:{(today + timedelta(days=7)).strftime('%d.%m.%Y')}"),
        ],
    ])
    msg = "📅 Укажи <b>дату</b> задачи (ДД.ММ.ГГГГ)\nили выбери быстрый вариант:"
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=quick)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=quick)
    return TASK_DATE


async def got_quick_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data.split(":")[1]
    ctx.user_data["task"]["date_str"] = date_str
    await query.edit_message_text(f"📅 Дата: {date_str}")
    await query.message.reply_text("⏰ Введи <b>время</b> начала (ЧЧ:ММ):", parse_mode=ParseMode.HTML)
    return TASK_TIME


async def got_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        datetime.strptime(text, "%d.%m.%Y")
        ctx.user_data["task"]["date_str"] = text
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи дату как ДД.ММ.ГГГГ:")
        return TASK_DATE
    await update.message.reply_text("⏰ Введи <b>время</b> начала (ЧЧ:ММ):", parse_mode=ParseMode.HTML)
    return TASK_TIME


async def got_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        t = datetime.strptime(text, "%H:%M")
        ds = ctx.user_data["task"]["date_str"]
        dt = datetime.strptime(f"{ds} {text}", "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
        ctx.user_data["task"]["scheduled_at"] = dt.isoformat()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи время как ЧЧ:ММ:")
        return TASK_TIME

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Без дедлайна →", callback_data="skip_deadline")
    ]])
    await update.message.reply_text(
        "⏰ Укажи <b>дедлайн</b> (ДД.ММ.ГГГГ ЧЧ:ММ) или пропусти:",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )
    return TASK_DEADLINE


async def got_deadline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        dt = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
        ctx.user_data["task"]["deadline"] = dt.isoformat()
    except ValueError:
        await update.message.reply_text("❌ Формат: ДД.ММ.ГГГГ ЧЧ:ММ")
        return TASK_DEADLINE
    return await ask_priority(update, ctx)


async def skip_deadline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["task"]["deadline"] = None
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("⏰ Дедлайн не указан.")
    return await ask_priority(update, ctx)


async def ask_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 КРИТИЧНО — срочно и важно", callback_data="pri:critical")],
        [InlineKeyboardButton("🟠 Высокий — важно",           callback_data="pri:high")],
        [InlineKeyboardButton("🟡 Средний — обычная задача",  callback_data="pri:medium")],
        [InlineKeyboardButton("🟢 Низкий — когда будет время", callback_data="pri:low")],
    ])
    msg = "🏷 Выбери <b>приоритет</b>:"
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    return TASK_PRIORITY


async def got_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pri = query.data.split(":")[1]
    ctx.user_data["task"]["priority"] = pri
    await query.edit_message_text(f"🏷 Приоритет: {PRIORITY[pri]['label']}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Без ссылки →", callback_data="skip_link")
    ]])
    await query.message.reply_text(
        "🔗 Прикрепи <b>ссылку</b> (или пропусти):",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )
    return TASK_LINK


async def got_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["task"]["link"] = update.message.text.strip()
    return await show_task_confirm(update, ctx)


async def skip_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["task"]["link"] = None
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🔗 Ссылка не добавлена.")
    return await show_task_confirm(update, ctx)


async def show_task_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    task = ctx.user_data["task"]
    p = PRIORITY.get(task.get("priority", "medium"), PRIORITY["medium"])
    lines = [
        "✅ <b>Проверь задачу перед сохранением:</b>\n",
        f"{p['emoji']} <b>{task['title']}</b>",
        f"📅 {fmt_dt(task['scheduled_at'])}",
    ]
    if task.get("deadline"):
        lines.append(f"⏰ Дедлайн: {fmt_dt(task['deadline'])}")
    lines.append(f"🏷 {p['name']}")
    if task.get("note"):
        lines.append(f"📝 {task['note']}")
    if task.get("link"):
        lines.append(f"🔗 {task['link']}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Сохранить",  callback_data="confirm_save"),
        InlineKeyboardButton("❌ Отменить",   callback_data="confirm_cancel"),
    ]])
    msg = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    return TASK_CONFIRM


async def confirm_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task = ctx.user_data["task"]
    user_id = update.effective_user.id

    task_id = db.add_task(
        user_id=user_id,
        title=task["title"],
        note=task.get("note", ""),
        scheduled_at=task["scheduled_at"],
        deadline=task.get("deadline"),
        priority=task.get("priority", "medium"),
        link=task.get("link"),
    )
    t = db.get_task(task_id)
    if scheduler:
        scheduler.schedule_task(t)

    await query.edit_message_text("💾 Задача сохранена!")
    await query.message.reply_text(
        task_card(t),
        parse_mode=ParseMode.HTML,
        reply_markup=task_keyboard(t),
    )
    return ConversationHandler.END


async def confirm_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("❌ Создание задачи отменено.")
    ctx.user_data.clear()
    return ConversationHandler.END


# ─── Voice task ────────────────────────────────────────────────────────────────

async def voice_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎤 Отправь голосовое сообщение!\n\n"
        "Скажи что-то вроде:\n"
        "<i>«Сделать инфографику для карточек товара, завтра до 16:00, срочно»</i>",
        parse_mode=ParseMode.HTML,
    )


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Распознаю речь…")
    try:
        import openai
        voice = update.message.voice
        file = await ctx.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()

        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # Transcribe
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", bytes(audio_bytes), "audio/ogg"),
        )
        text = transcript.text
        await update.message.reply_text(f"📝 Распознано: <i>{text}</i>", parse_mode=ParseMode.HTML)

        # Parse with GPT
        today_str = now().strftime("%d.%m.%Y")
        prompt = f"""Сегодня {today_str}. Разбери задачу из текста и верни JSON (без markdown):
{{
  "title": "краткое название",
  "note": "детали или ''",
  "date": "ДД.ММ.ГГГГ или '{today_str}' если не указано",
  "time": "ЧЧ:ММ или '09:00' если не указано",
  "deadline_date": "ДД.ММ.ГГГГ или null",
  "deadline_time": "ЧЧ:ММ или null",
  "priority": "critical|high|medium|low"
}}
Текст: {text}"""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        data = json.loads(resp.choices[0].message.content)

        dt = datetime.strptime(f"{data['date']} {data['time']}", "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
        task = {
            "title": data["title"],
            "note": data.get("note", ""),
            "date_str": data["date"],
            "scheduled_at": dt.isoformat(),
            "deadline": None,
            "priority": data.get("priority", "medium"),
            "link": None,
        }
        if data.get("deadline_date") and data.get("deadline_time"):
            ddl = datetime.strptime(
                f"{data['deadline_date']} {data['deadline_time']}", "%d.%m.%Y %H:%M"
            ).replace(tzinfo=TIMEZONE)
            task["deadline"] = ddl.isoformat()

        ctx.user_data["task"] = task
        return await show_task_confirm(update, ctx)

    except ImportError:
        await update.message.reply_text(
            "⚠️ Голосовой ввод требует OpenAI API.\n"
            "Добавь OPENAI_API_KEY в .env файл."
        )
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не удалось распознать. Попробуй ещё раз.")


# ─── Task list & calendar ───────────────────────────────────────────────────────

async def show_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = db.get_user_tasks(user_id, status="pending")
    if not tasks:
        await update.message.reply_text(
            "📭 Нет активных задач.\n\nНажми <b>➕ Новая задача</b>!",
            parse_mode=ParseMode.HTML,
        )
        return

    # Sort by priority then date
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    tasks.sort(key=lambda t: (order.get(t["priority"], 2), t["scheduled_at"]))

    await update.message.reply_text(
        f"📋 <b>Активные задачи ({len(tasks)})</b>",
        parse_mode=ParseMode.HTML,
    )
    for task in tasks[:10]:
        await update.message.reply_text(
            task_card(task),
            parse_mode=ParseMode.HTML,
            reply_markup=task_keyboard(task),
        )


async def show_urgent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = db.get_user_tasks(user_id, priority_filter=["critical", "high"], status="pending")
    if not tasks:
        await update.message.reply_text("✅ Нет срочных задач!")
        return
    await update.message.reply_text(
        f"🚨 <b>Срочные задачи ({len(tasks)})</b>",
        parse_mode=ParseMode.HTML,
    )
    for task in tasks[:10]:
        await update.message.reply_text(
            task_card(task),
            parse_mode=ParseMode.HTML,
            reply_markup=task_keyboard(task),
        )


async def show_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = now().date()
    tasks = db.get_tasks_range(user_id, today, today + timedelta(days=7))

    lines = ["📅 <b>Задачи на ближайшие 7 дней</b>\n"]
    current_date = None
    for task in tasks:
        dt = datetime.fromisoformat(task["scheduled_at"]).replace(tzinfo=TIMEZONE)
        d = dt.date()
        if d != current_date:
            current_date = d
            day_label = "Сегодня" if d == today else ("Завтра" if d == today + timedelta(days=1) else d.strftime("%d.%m (%a)"))
            lines.append(f"\n<b>── {day_label} ──</b>")
        p = PRIORITY.get(task["priority"], PRIORITY["medium"])
        lines.append(f"  {p['emoji']} {dt.strftime('%H:%M')} — {task['title']}")

    if len(lines) == 1:
        lines.append("\nЗадач нет 🎉")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Показать все подробно", callback_data="list_week"),
    ]])
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb,
    )


async def list_week_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    today = now().date()
    tasks = db.get_tasks_range(user_id, today, today + timedelta(days=7))
    if not tasks:
        await query.message.reply_text("Задач нет 🎉")
        return
    for task in tasks:
        await query.message.reply_text(
            task_card(task), parse_mode=ParseMode.HTML, reply_markup=task_keyboard(task),
        )


# ─── Task actions ───────────────────────────────────────────────────────────────

async def handle_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, tid = query.data.split(":", 1)
    tid = int(tid)
    task = db.get_task(tid)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return

    if action == "done":
        db.update_task_status(tid, "done")
        if scheduler:
            scheduler.cancel_task(tid)
        await query.edit_message_text(
            f"✅ <b>Выполнено!</b>\n\n{task['title']}",
            parse_mode=ParseMode.HTML,
        )

    elif action == "irrelevant":
        db.update_task_status(tid, "irrelevant")
        if scheduler:
            scheduler.cancel_task(tid)
        await query.edit_message_text(
            f"🚫 Задача помечена как <b>неактуальная</b>:\n{task['title']}",
            parse_mode=ParseMode.HTML,
        )

    elif action == "delete":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data=f"del_confirm:{tid}"),
            InlineKeyboardButton("Отмена",      callback_data=f"del_cancel:{tid}"),
        ]])
        await query.edit_message_text(
            f"❓ Удалить задачу «{task['title']}»?",
            reply_markup=kb,
        )

    elif action == "del_confirm":
        db.delete_task(tid)
        if scheduler:
            scheduler.cancel_task(tid)
        await query.edit_message_text("🗑 Задача удалена.")

    elif action == "del_cancel":
        t = db.get_task(tid)
        await query.edit_message_text(task_card(t), parse_mode=ParseMode.HTML, reply_markup=task_keyboard(t))

    elif action == "dup":
        new_id = db.duplicate_task(tid)
        new_task = db.get_task(new_id)
        await query.message.reply_text(
            f"📋 Задача продублирована!\n\n{task_card(new_task)}",
            parse_mode=ParseMode.HTML,
            reply_markup=task_keyboard(new_task),
        )

    elif action == "reschedule":
        ctx.user_data["reschedule_id"] = tid
        today = now()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Завтра",   callback_data=f"rq:{tid}:{(today+timedelta(days=1)).strftime('%d.%m.%Y')}"),
                InlineKeyboardButton("+2 дня",   callback_data=f"rq:{tid}:{(today+timedelta(days=2)).strftime('%d.%m.%Y')}"),
                InlineKeyboardButton("+7 дней",  callback_data=f"rq:{tid}:{(today+timedelta(days=7)).strftime('%d.%m.%Y')}"),
            ],
        ])
        await query.message.reply_text(
            f"📅 Перенести «<b>{task['title']}</b>» на:\n(или введи дату ДД.ММ.ГГГГ)",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        ctx.user_data["awaiting_reschedule"] = tid

    elif action == "rq":
        parts = query.data.split(":")
        task_id = int(parts[1])
        date_str = parts[2]
        t = db.get_task(task_id)
        old_dt = datetime.fromisoformat(t["scheduled_at"])
        new_dt = datetime.strptime(
            f"{date_str} {old_dt.strftime('%H:%M')}", "%d.%m.%Y %H:%M"
        ).replace(tzinfo=TIMEZONE)
        db.update_task_field(task_id, "scheduled_at", new_dt.isoformat())
        db.update_task_status(task_id, "pending")
        updated = db.get_task(task_id)
        if scheduler:
            scheduler.cancel_task(task_id)
            scheduler.schedule_task(updated)
        await query.edit_message_text(
            f"📅 Перенесено на {date_str}!\n\n{task_card(updated)}",
            parse_mode=ParseMode.HTML,
            reply_markup=task_keyboard(updated),
        )


async def edit_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, field, tid = query.data.split(":")
    ctx.user_data["edit_id"] = int(tid)
    ctx.user_data["edit_field"] = field

    prompts = {
        "title":    "Введи новое <b>название</b>:",
        "note":     "Введи новое <b>примечание</b>:",
        "datetime": "Введи новые <b>дату и время</b> (ДД.ММ.ГГГГ ЧЧ:ММ):",
        "deadline": "Введи новый <b>дедлайн</b> (ДД.ММ.ГГГГ ЧЧ:ММ):",
        "link":     "Введи новую <b>ссылку</b>:",
    }
    if field == "priority":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 КРИТИЧНО", callback_data=f"ep:critical:{tid}")],
            [InlineKeyboardButton("🟠 Высокий",  callback_data=f"ep:high:{tid}")],
            [InlineKeyboardButton("🟡 Средний",  callback_data=f"ep:medium:{tid}")],
            [InlineKeyboardButton("🟢 Низкий",   callback_data=f"ep:low:{tid}")],
        ])
        await query.edit_message_text("🏷 Выбери новый приоритет:", reply_markup=kb)
        return

    await query.edit_message_text(prompts.get(field, "Введи новое значение:"), parse_mode=ParseMode.HTML)
    ctx.user_data["awaiting_edit"] = True


async def edit_priority_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pri, tid = query.data.split(":")
    db.update_task_field(int(tid), "priority", pri)
    updated = db.get_task(int(tid))
    await query.edit_message_text(task_card(updated), parse_mode=ParseMode.HTML, reply_markup=task_keyboard(updated))


async def receive_edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_edit"):
        return
    tid = ctx.user_data.get("edit_id")
    field = ctx.user_data.get("edit_field")
    text = update.message.text.strip()

    try:
        if field == "datetime":
            dt = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
            db.update_task_field(tid, "scheduled_at", dt.isoformat())
        elif field == "deadline":
            dt = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
            db.update_task_field(tid, "deadline", dt.isoformat())
        else:
            db.update_task_field(tid, field, text)
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Попробуй ещё раз.")
        return

    ctx.user_data.pop("awaiting_edit", None)
    updated = db.get_task(tid)
    if scheduler and field in ("datetime", "deadline"):
        scheduler.cancel_task(tid)
        scheduler.schedule_task(updated)
    await update.message.reply_text(
        f"✅ Обновлено!\n\n{task_card(updated)}",
        parse_mode=ParseMode.HTML,
        reply_markup=task_keyboard(updated),
    )


# ─── Text router ───────────────────────────────────────────────────────────────

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    # Проверяем ожидание причины отказа от задания
    if await receive_decline_reason(update, ctx):
        return
    if ctx.user_data.get("awaiting_edit"):
        return await receive_edit_value(update, ctx)
    if ctx.user_data.get("awaiting_reschedule"):
        try:
            tid = ctx.user_data.pop("awaiting_reschedule")
            t = db.get_task(tid)
            old_dt = datetime.fromisoformat(t["scheduled_at"])
            date_str = text.strip()
            new_dt = datetime.strptime(
                f"{date_str} {old_dt.strftime('%H:%M')}", "%d.%m.%Y %H:%M"
            ).replace(tzinfo=TIMEZONE)
            db.update_task_field(tid, "scheduled_at", new_dt.isoformat())
            db.update_task_status(tid, "pending")
            updated = db.get_task(tid)
            if scheduler:
                scheduler.cancel_task(tid)
                scheduler.schedule_task(updated)
            await update.message.reply_text(
                f"📅 Задача перенесена!\n\n{task_card(updated)}",
                parse_mode=ParseMode.HTML, reply_markup=task_keyboard(updated),
            )
        except ValueError:
            await update.message.reply_text("❌ Неверный формат даты. Введи ДД.ММ.ГГГГ:")
        return

    if text == "📋 Мои задачи":
        return await show_tasks(update, ctx)
    if text == "📅 Календарь":
        return await show_calendar(update, ctx)
    if text == "🔍 Срочные":
        return await show_urgent(update, ctx)
    if text == "➕ Новая задача":
        return await new_task_start(update, ctx)
    if text == "🎤 Голосовая задача":
        return await voice_start(update, ctx)


# ─── Photo handler ─────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("task"):
        await update.message.reply_text("Сначала создай задачу (➕ Новая задача), потом прикрепи фото.")
        return
    photo_id = update.message.photo[-1].file_id
    ctx.user_data["task"]["photo_id"] = photo_id
    await update.message.reply_text("🖼 Фото прикреплено к задаче!")


# ─── Assignment responses (from team members) ──────────────────────────────────

# State для ожидания причины отказа
AWAITING_DECLINE = "awaiting_decline_for"


async def handle_assignment_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ✅ Принял / ❌ Не могу от сотрудников."""
    query = update.callback_query
    await query.answer()
    action, aid = query.data.split(":", 1)
    aid = int(aid)
    assignment = db.get_assignment(aid)
    if not assignment:
        await query.edit_message_text("❌ Задание не найдено.")
        return

    if action == "assign_accept":
        db.update_assignment_status(aid, "accepted")
        await query.edit_message_text(
            f"✅ <b>Принято!</b>\n\n{assignment['title']}\n\n"
            "Ответ зафиксирован.",
            parse_mode=ParseMode.HTML,
        )
        # Уведомляем владельца
        member = db.get_member(assignment["member_id"])
        if member:
            name = member.get("name") or f"@{member['username']}"
            role = f" ({member['role']})" if member.get("role") else ""
            try:
                await ctx.bot.send_message(
                    chat_id=assignment["owner_id"],
                    text=(
                        f"✅ <b>{name}{role}</b> принял задание:\n"
                        f"<i>{assignment['title']}</i>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить владельца: {e}")

    elif action == "assign_decline":
        # Просим написать причину
        ctx.user_data[AWAITING_DECLINE] = aid
        await query.edit_message_text(
            f"❌ Задание: <b>{assignment['title']}</b>\n\n"
            "Напишите причину, почему не можете выполнить задачу:",
            parse_mode=ParseMode.HTML,
        )


async def receive_decline_reason(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перехватывает текст как причину отказа. Возвращает True если обработал."""
    aid = ctx.user_data.get(AWAITING_DECLINE)
    if not aid:
        return False
    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text("Пожалуйста, напишите причину:")
        return True

    ctx.user_data.pop(AWAITING_DECLINE, None)
    db.update_assignment_status(aid, "declined", decline_reason=reason)

    assignment = db.get_assignment(aid)
    await update.message.reply_text(
        "Принято. Причина записана.",
        parse_mode=ParseMode.HTML,
    )

    # Уведомляем владельца с причиной
    if assignment:
        member = db.get_member(assignment["member_id"])
        if member:
            name = member.get("name") or f"@{member['username']}"
            role = f" ({member['role']})" if member.get("role") else ""
            try:
                await ctx.bot.send_message(
                    chat_id=assignment["owner_id"],
                    text=(
                        f"❌ <b>{name}{role}</b> отказался от задания:\n"
                        f"<i>{assignment['title']}</i>\n\n"
                        f"📝 Причина: {reason}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить владельца: {e}")
    return True


# ─── Build app ─────────────────────────────────────────────────────────────────

def build_app(token: str, sched: TaskScheduler = None, persistence_path: str = None) -> Application:
    global scheduler
    scheduler = sched

    builder = Application.builder().token(token)

    if persistence_path:
        from telegram.ext import PicklePersistence
        persistence = PicklePersistence(filepath=persistence_path)
        builder = builder.persistence(persistence)

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_action, pattern="^(done|irrelevant|delete|del_confirm|del_cancel|dup|reschedule|rq):"))
    app.add_handler(CallbackQueryHandler(handle_assignment_action, pattern="^(assign_accept|assign_decline):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        print("❌ Укажи BOT_TOKEN в файле .env")
        exit(1)

    from scheduler import TaskScheduler
    import asyncio

    async def main():
        application = build_app(token)
        sched = TaskScheduler(application.bot, db)
        await sched.start()
        # Schedule missed tasks on startup
        all_pending = db.get_all_pending()
        for t in all_pending:
            sched.schedule_task(t)
        print("🤖 Бот запущен!")
        await application.run_polling(allowed_updates=Update.ALL_TYPES)

    asyncio.run(main())
