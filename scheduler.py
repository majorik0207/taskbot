import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
from typing import Dict, Optional

from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

PRIORITY_LABELS = {
    "critical": "🔴 КРИТИЧНО",
    "high":     "🟠 Высокий",
    "medium":   "🟡 Средний",
    "low":      "🟢 Низкий",
}


def now() -> datetime:
    return datetime.now(TIMEZONE)


class TaskScheduler:
    """
    Simple asyncio-based scheduler.
    For each task we store a asyncio.Task handle so we can cancel it.
    """

    def __init__(self, bot: Bot, db):
        self.bot = bot
        self.db = db
        self._handles: Dict[int, asyncio.Task] = {}
        self._overdue_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the overdue checker that runs every morning."""
        self._overdue_task = asyncio.create_task(self._overdue_loop())
        logger.info("Scheduler started.")

    def schedule_task(self, task: dict):
        """Schedule reminder(s) for a single task."""
        tid = task["id"]
        # Cancel existing handle if any
        self.cancel_task(tid)

        handle = asyncio.create_task(self._task_reminder_loop(task))
        self._handles[tid] = handle

    def cancel_task(self, task_id: int):
        handle = self._handles.pop(task_id, None)
        if handle and not handle.done():
            handle.cancel()

    # ── Private ────────────────────────────────────────────────────────────────

    async def _task_reminder_loop(self, task: dict):
        """
        Send reminder at scheduled_at.
        Then every morning at 10:00 until task is done/irrelevant.
        """
        tid = task["id"]
        try:
            scheduled = datetime.fromisoformat(task["scheduled_at"]).replace(tzinfo=TIMEZONE)
            delay = (scheduled - now()).total_seconds()

            if delay > 0:
                # Send a 30-min heads-up if there's time
                pre_delay = delay - 1800
                if pre_delay > 60:
                    await asyncio.sleep(pre_delay)
                    await self._send_reminder(task, pre=True)
                    remaining = (scheduled - now()).total_seconds()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                else:
                    await asyncio.sleep(delay)
            # Send main reminder
            await self._send_reminder(task, pre=False)

            # Now repeat every day at 10:00 until done
            while True:
                current = self.db.get_task(tid)
                if not current or current["status"] != "pending":
                    break
                # Sleep until next 10:00
                nxt = self._next_10am()
                delay_next = (nxt - now()).total_seconds()
                if delay_next < 60:
                    delay_next += 86400
                await asyncio.sleep(delay_next)
                current = self.db.get_task(tid)
                if not current or current["status"] != "pending":
                    break
                await self._send_overdue_reminder(current)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Reminder loop error for task {tid}: {e}")

    async def _send_reminder(self, task: dict, pre: bool = False):
        try:
            fresh = self.db.get_task(task["id"])
            if not fresh or fresh["status"] != "pending":
                return
            p_label = PRIORITY_LABELS.get(fresh["priority"], "🟡")
            scheduled = datetime.fromisoformat(fresh["scheduled_at"]).replace(tzinfo=TIMEZONE)
            header = "⏰ <b>Через 30 минут!</b>" if pre else "🔔 <b>Напоминание о задаче</b>"

            deadline_line = ""
            if fresh.get("deadline"):
                ddl = datetime.fromisoformat(fresh["deadline"]).replace(tzinfo=TIMEZONE)
                deadline_line = f"\n⏳ Дедлайн: <b>{ddl.strftime('%H:%M')}</b>"

            text = (
                f"{header}\n\n"
                f"{p_label} <b>{fresh['title']}</b>\n"
                f"📅 {scheduled.strftime('%d.%m.%Y %H:%M')}"
                f"{deadline_line}"
            )
            if fresh.get("note"):
                text += f"\n\n📝 <i>{fresh['note']}</i>"

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Выполнено",     callback_data=f"done:{fresh['id']}"),
                InlineKeyboardButton("🚫 Неактуально",  callback_data=f"irrelevant:{fresh['id']}"),
            ]])
            await self.bot.send_message(
                chat_id=fresh["user_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as e:
            logger.error(f"send_reminder error: {e}")

    async def _send_overdue_reminder(self, task: dict):
        try:
            p_label = PRIORITY_LABELS.get(task["priority"], "🟡")
            scheduled = datetime.fromisoformat(task["scheduled_at"]).replace(tzinfo=TIMEZONE)
            text = (
                f"⚠️ <b>Невыполненная задача!</b>\n\n"
                f"{p_label} <b>{task['title']}</b>\n"
                f"📅 Была запланирована на {scheduled.strftime('%d.%m.%Y %H:%M')}"
            )
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Выполнено",     callback_data=f"done:{task['id']}"),
                InlineKeyboardButton("🚫 Неактуально",  callback_data=f"irrelevant:{task['id']}"),
                InlineKeyboardButton("📅 Перенести",    callback_data=f"reschedule:{task['id']}"),
            ]])
            await self.bot.send_message(
                chat_id=task["user_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as e:
            logger.error(f"overdue_reminder error: {e}")

    async def _overdue_loop(self):
        """Every day at 10:00 send reminders for ALL overdue tasks."""
        while True:
            try:
                nxt = self._next_10am()
                delay = (nxt - now()).total_seconds()
                await asyncio.sleep(delay)
                overdue = self.db.get_overdue_pending()
                for task in overdue:
                    # Only send if not already handled by individual loops
                    if task["id"] not in self._handles:
                        await self._send_overdue_reminder(task)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Overdue loop error: {e}")
                await asyncio.sleep(3600)

    @staticmethod
    def _next_10am() -> datetime:
        t = now().replace(hour=10, minute=0, second=0, microsecond=0)
        if t <= now():
            t += timedelta(days=1)
        return t
