#!/usr/bin/env python3
"""
Планировщик напоминаний — запускается через CRON каждые 5 минут.
Проверяет задачи и отправляет напоминания в Telegram.

Добавь в Планировщик CRON на hoster.by:
*/5 * * * * /usr/bin/python3 /www/meduzacrystal.by/taskbot/cron_scheduler.py
"""
import sys
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Путь к папке с ботом
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BOT_DIR)

# Загружаем .env
def load_env():
    env_path = os.path.join(BOT_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ[key.strip()] = val.strip()

load_env()

TIMEZONE = ZoneInfo(os.environ.get('TIMEZONE', 'Europe/Moscow'))
UTC = ZoneInfo('UTC')

PRIORITY_LABELS = {
    "critical": "🔴 КРИТИЧНО",
    "high":     "🟠 Высокий",
    "medium":   "🟡 Средний",
    "low":      "🟢 Низкий",
}

def now():
    return datetime.now(UTC)

def parse_dt(dt_str: str) -> datetime:
    """Парсит дату из базы — поддерживает Z и +offset форматы, возвращает UTC."""
    if not dt_str:
        return None
    # Заменяем Z на +00:00 для fromisoformat
    s = dt_str.replace('Z', '+00:00')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

def fmt_local(dt_str: str) -> str:
    """Форматирует время для показа пользователю в местном часовом поясе."""
    dt = parse_dt(dt_str)
    if not dt:
        return ''
    return dt.astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M')


async def run():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        print("❌ BOT_TOKEN не найден!")
        return

    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from database import Database

    db = Database(os.path.join(BOT_DIR, 'tasks.db'))
    bot = Bot(token=token)

    current_time = now()  # UTC
    # Для сравнения со строками в БД используем UTC без суффикса
    def utc_str(dt):
        return dt.strftime('%Y-%m-%dT%H:%M:%S')

    current_hour_local = current_time.astimezone(TIMEZONE).hour
    current_minute_local = current_time.astimezone(TIMEZONE).minute

    # Все задачи из базы — фильтруем вручную чтобы избежать проблем с форматами
    all_pending = db.get_all_pending()

    for task in all_pending:
        if task['status'] != 'pending':
            continue

        task_dt = parse_dt(task['scheduled_at'])
        if not task_dt:
            continue

        diff_minutes = (task_dt - current_time).total_seconds() / 60

        p_label = PRIORITY_LABELS.get(task['priority'], '🟡')
        time_display = fmt_local(task['scheduled_at'])

        # ── 1. Напоминание за 30 минут ──────────────────────────────────────
        if 25 <= diff_minutes <= 35:
            flag_file = os.path.join(BOT_DIR, f'sent_pre_{task["id"]}.flag')
            if not os.path.exists(flag_file):
                text = (
                    f"⏰ <b>Через 30 минут!</b>\n\n"
                    f"{p_label} <b>{task['title']}</b>\n"
                    f"📅 {time_display}"
                )
                if task.get('note'):
                    text += f"\n\n📝 <i>{task['note']}</i>"

                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Выполнено",    callback_data=f"done:{task['id']}"),
                    InlineKeyboardButton("🚫 Неактуально", callback_data=f"irrelevant:{task['id']}"),
                ]])
                try:
                    await bot.send_message(chat_id=task['user_id'], text=text,
                                           parse_mode=ParseMode.HTML, reply_markup=kb)
                    open(flag_file, 'w').close()
                    print(f"✅ Pre-reminder sent for task {task['id']}")
                except Exception as e:
                    print(f"Ошибка pre-reminder задачи {task['id']}: {e}")

        # ── 2. Напоминание в момент задачи (0-6 минут назад) ────────────────
        elif -6 <= diff_minutes <= 0:
            flag_file = os.path.join(BOT_DIR, f'sent_due_{task["id"]}.flag')
            if not os.path.exists(flag_file):
                deadline_line = ""
                if task.get('deadline'):
                    deadline_line = f"\n⏳ Дедлайн: <b>{fmt_local(task['deadline'])}</b>"

                text = (
                    f"🔔 <b>Пора выполнять задачу!</b>\n\n"
                    f"{p_label} <b>{task['title']}</b>\n"
                    f"📅 {time_display}"
                    f"{deadline_line}"
                )
                if task.get('note'):
                    text += f"\n\n📝 <i>{task['note']}</i>"
                if task.get('link'):
                    text += f"\n🔗 <a href='{task['link']}'>Ссылка</a>"

                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Выполнено",    callback_data=f"done:{task['id']}"),
                    InlineKeyboardButton("🚫 Неактуально", callback_data=f"irrelevant:{task['id']}"),
                    InlineKeyboardButton("📅 Перенести",   callback_data=f"reschedule:{task['id']}"),
                ]])
                try:
                    await bot.send_message(chat_id=task['user_id'], text=text,
                                           parse_mode=ParseMode.HTML, reply_markup=kb)
                    open(flag_file, 'w').close()
                    print(f"✅ Due-reminder sent for task {task['id']}")
                except Exception as e:
                    print(f"Ошибка due-reminder задачи {task['id']}: {e}")

    # ── 3. Ежедневное утреннее напоминание о просроченных (в 10:00 по местному) ──
    if current_hour_local == 10 and current_minute_local < 5:
        overdue = db.get_overdue_pending()
        for task in overdue:
            flag_file = os.path.join(BOT_DIR,
                f'sent_overdue_{task["id"]}_{current_time.astimezone(TIMEZONE).date()}.flag')
            if os.path.exists(flag_file):
                continue

            p_label = PRIORITY_LABELS.get(task['priority'], '🟡')
            text = (
                f"⚠️ <b>Невыполненная задача!</b>\n\n"
                f"{p_label} <b>{task['title']}</b>\n"
                f"📅 Была запланирована на {fmt_local(task['scheduled_at'])}"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Выполнено",    callback_data=f"done:{task['id']}"),
                InlineKeyboardButton("🚫 Неактуально", callback_data=f"irrelevant:{task['id']}"),
                InlineKeyboardButton("📅 Перенести",   callback_data=f"reschedule:{task['id']}"),
            ]])
            try:
                await bot.send_message(chat_id=task['user_id'], text=text,
                                       parse_mode=ParseMode.HTML, reply_markup=kb)
                open(flag_file, 'w').close()
            except Exception as e:
                print(f"Ошибка overdue задачи {task['id']}: {e}")

    # ── 4. Чистим старые флаги (раз в день в 3:00) ───────────────────────────
    if current_hour_local == 3 and current_minute_local < 5:
        cutoff = current_time - timedelta(days=7)
        for fname in os.listdir(BOT_DIR):
            if fname.endswith('.flag'):
                fpath = os.path.join(BOT_DIR, fname)
                if os.path.getmtime(fpath) < cutoff.timestamp():
                    os.remove(fpath)

    print(f"✅ CRON выполнен: {current_time.astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M')}")

    # ── 5. Отправка заданий сотрудникам ──────────────────────────────────────
    pending_assignments = db.get_pending_assignments_to_send()
    for asgn in pending_assignments:
        member_user_id = asgn.get('member_user_id')
        if not member_user_id:
            # Сотрудник ещё не писал боту — пропускаем
            print(f"⚠️  Assignment {asgn['id']}: сотрудник @{asgn['member_username']} не активировал бота")
            continue

        time_display = fmt_local(asgn['scheduled_at'])
        member_name = asgn.get('member_name') or f"@{asgn['member_username']}"
        member_role = asgn.get('member_role', '')

        text = (
            f"📋 <b>Вам поставлена задача!</b>\n\n"
            f"<b>{asgn['title']}</b>\n"
            f"📅 {time_display}"
        )
        if asgn.get('note'):
            text += f"\n\n📝 <i>{asgn['note']}</i>"

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Принял",   callback_data=f"assign_accept:{asgn['id']}"),
            InlineKeyboardButton("❌ Не могу",  callback_data=f"assign_decline:{asgn['id']}"),
        ]])

        try:
            await bot.send_message(
                chat_id=member_user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            db.mark_assignment_sent(asgn['id'])
            print(f"✅ Assignment {asgn['id']} отправлен пользователю {member_user_id}")
        except Exception as e:
            print(f"❌ Ошибка отправки assignment {asgn['id']}: {e}")


if __name__ == '__main__':
    asyncio.run(run())
