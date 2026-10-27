"""SquadUp Telegram bot: in-chat registration, team matching, invite loops and reminders.

Run from the repo root (the API must be running):
    export TELEGRAM_BOT_TOKEN=123:abc
    export TELEGRAM_BOT_USERNAME=YourBotName      # same value the API uses to build links
    python -m bot.telegram_bot
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

from bot.i18n import (
    EXPERIENCE_ORDER,
    ROLE_EMOJI,
    ROLE_ORDER,
    escape_md,
    experience_label,
    render_team_card,
    role_label,
    t,
)

API_URL = os.getenv("RADAR_API_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TZ = ZoneInfo(os.getenv("EVENT_TIMEZONE", "Asia/Kolkata"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
PERSISTENCE_FILE = os.getenv("BOT_PERSISTENCE_FILE", "bot_state.pickle")
REMINDERS: tuple[tuple[str, int], ...] = (("d7", 7), ("d2", 2), ("d1", 1))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("squadup.bot")


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class RadarApi:
    """Thin async wrapper around the FastAPI backend. Returns None on 404."""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    async def request(self, method: str, path: str, **kwargs: Any) -> Optional[Any]:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get(self, path: str, **kwargs: Any) -> Optional[Any]:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Optional[Any]:
        return await self.request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> Optional[Any]:
        return await self.request("PATCH", path, **kwargs)

    async def close(self) -> None:
        await self.client.aclose()


api: RadarApi = None  # type: ignore[assignment]  # created in post_init, on the bot's event loop


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def lang_of(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("lang", "en")  # type: ignore[union-attr]


def share_url(link: str, text: str) -> str:
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"


def menu_keyboard(lang: str, participant: dict[str, Any]) -> InlineKeyboardMarkup:
    event_name = participant.get("event_name", "")
    rows = [
        [InlineKeyboardButton(t(lang, "invite_cta"),
                              url=share_url(participant["invite_link"],
                                            t(lang, "invite_share_text", event=event_name)))],
        [InlineKeyboardButton(t(lang, "btn_my_team"), callback_data="team")],
    ]
    if participant["status"] == "registered":
        rows.append([InlineKeyboardButton(t(lang, "btn_confirm"), callback_data=f"confirm:{participant['id']}")])
    return InlineKeyboardMarkup(rows)


def reminder_keyboard(lang: str, participant_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_confirm"), callback_data=f"confirm:{participant_id}")],
        [InlineKeyboardButton(t(lang, "btn_cant"), callback_data=f"cant:{participant_id}")],
    ])


def card_text(lang: str, participant: dict[str, Any]) -> str:
    return render_team_card(lang, participant["team"]) if participant.get("team") else ""


async def current_participant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict[str, Any]]:
    """The caller's active registration: the event they last opened, else their newest one."""
    user_id = update.effective_user.id
    event_id = context.user_data.get("event_id")
    params: dict[str, Any] = {"telegram_id": user_id}
    if event_id:
        params["event_id"] = event_id
    participant = await api.get("/api/participants/lookup", params=params)
    if participant is None and event_id:
        participant = await api.get("/api/participants/lookup", params={"telegram_id": user_id})
    if participant and participant["status"] == "withdrawn":
        return None
    if participant:
        context.user_data["participant_id"] = participant["id"]
        context.user_data["event_id"] = participant["event_id"]
    return participant


async def owned_participant(update: Update, participant_id: int) -> Optional[dict[str, Any]]:
    """Fetch a participant only if it belongs to the Telegram user pressing the button."""
    participant = await api.get(f"/api/participants/{participant_id}")
    if not participant or participant.get("telegram_id") != update.effective_user.id:
        return None
    return participant


# --------------------------------------------------------------------------- #
# Reminders (drop-off prevention)
# --------------------------------------------------------------------------- #
def cancel_reminders(application: Application, participant_id: int) -> None:
    for key, _ in REMINDERS:
        for job in application.job_queue.get_jobs_by_name(f"rem:{participant_id}:{key}"):
            job.schedule_removal()


def schedule_reminders(application: Application, participant: dict[str, Any]) -> int:
    """Queue 7/2/1-day nudges at REMINDER_HOUR local time. Returns how many were scheduled."""
    pid = participant.get("participant_id") or participant["id"]
    chat_id = participant.get("telegram_id")
    start = participant.get("start_date")
    if not chat_id or not start:
        return 0
    try:
        event_day = date.fromisoformat(start[:10])
    except ValueError:
        return 0

    cancel_reminders(application, pid)
    now = datetime.now(TZ)
    scheduled = 0
    for key, days in REMINDERS:
        when = datetime.combine(event_day - timedelta(days=days), time(REMINDER_HOUR, tzinfo=TZ))
        if when <= now:
            continue
        application.job_queue.run_once(
            reminder_job, when=when, chat_id=chat_id, name=f"rem:{pid}:{key}",
            data={"participant_id": pid, "key": key},
        )
        scheduled += 1
    return scheduled


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    pid, key = job.data["participant_id"], job.data["key"]
    try:
        participant = await api.get(f"/api/participants/{pid}")
    except httpx.HTTPError:
        log.exception("Reminder %s for participant %s: API unavailable", key, pid)
        return
    if not participant or participant["status"] == "withdrawn":
        return

    lang = participant["language"]
    fields = {
        "name": escape_md(participant["name"].split()[0]),
        "event": escape_md(participant["event_name"]),
        "team": participant.get("team_number") or "—",
    }
    if participant["status"] == "confirmed":
        if key != "d1":
            return  # don't nag people who already said yes
        await context.bot.send_message(job.chat_id, t(lang, "reminder_confirmed", **fields),
                                       parse_mode=ParseMode.MARKDOWN)
        return
    await context.bot.send_message(
        job.chat_id, t(lang, f"reminder_{key}", **fields),
        parse_mode=ParseMode.MARKDOWN, reply_markup=reminder_keyboard(lang, pid),
    )


# --------------------------------------------------------------------------- #
# Onboarding: /start -> language -> role -> experience -> team card
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payload = context.args[0] if context.args else None
    ud = context.user_data
    info = await api.get("/api/ref/resolve", params={"payload": payload} if payload else None)
    if info is None:
        await update.message.reply_text(t(lang_of(context), "no_events"))
        return

    event = info["event"]
    ud.update(event_id=event["id"], event_name=event["name"], payload=payload)
    ud.pop("awaiting_college", None)

    existing = await api.get("/api/participants/lookup",
                             params={"telegram_id": update.effective_user.id, "event_id": event["id"]})
    if existing and existing["status"] != "withdrawn":
        lang = existing["language"]
        ud.update(lang=lang, participant_id=existing["id"])
        await update.message.reply_text(
            t(lang, "already", event=escape_md(event["name"])) + "\n\n" + card_text(lang, existing),
            parse_mode=ParseMode.MARKDOWN, reply_markup=menu_keyboard(lang, existing),
        )
        return

    if payload:
        await api.post("/api/ref/click", params={"payload": payload})
    ud["attribution"] = {
        "kind": info["kind"],
        "community": info.get("community_name"),
        "referrer": info.get("referrer"),
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("English", callback_data="lang:en"),
        InlineKeyboardButton("हिंदी", callback_data="lang:hi"),
    ]])
    await update.message.reply_text(
        t("en", "choose_language", event=escape_md(event["name"])) + "\nभाषा चुनें 👇",
        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
    )


async def on_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    context.user_data["lang"] = lang

    attribution = context.user_data.get("attribution") or {}
    intro = ""
    if attribution.get("kind") == "community" and attribution.get("community"):
        intro = t(lang, "via_community", community=escape_md(attribution["community"])) + "\n\n"
    elif attribution.get("kind") == "referral" and attribution.get("referrer"):
        ref = attribution["referrer"]
        intro = t(lang, "via_referral", name=escape_md(ref["first_name"]),
                  team=ref.get("team_number") or "—") + "\n\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ROLE_EMOJI[r]} {role_label(lang, r)}", callback_data=f"role:{r}")
         for r in ROLE_ORDER[:2]],
        [InlineKeyboardButton(f"{ROLE_EMOJI[r]} {role_label(lang, r)}", callback_data=f"role:{r}")
         for r in ROLE_ORDER[2:]],
    ])
    await query.edit_message_text(intro + t(lang, "ask_role"), parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=keyboard)


async def on_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["role"] = query.data.split(":", 1)[1]
    lang = lang_of(context)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(experience_label(lang, e), callback_data=f"exp:{e}")] for e in EXPERIENCE_ORDER]
    )
    await query.edit_message_text(t(lang, "ask_experience"), reply_markup=keyboard)


async def on_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    lang = lang_of(context)
    if "event_id" not in ud or "role" not in ud:
        await query.edit_message_text(t(lang, "not_registered"))
        return

    user = update.effective_user
    participant = await api.post("/api/register", json={
        "event_id": ud["event_id"],
        "name": user.full_name or user.first_name,
        "role": ud["role"],
        "experience": query.data.split(":", 1)[1],
        "language": lang,
        "telegram_id": user.id,
        "ref_payload": ud.get("payload"),
    })
    ud["participant_id"] = participant["id"]
    ud.pop("role", None)

    await query.edit_message_text(
        t(lang, "registered", name=escape_md(user.first_name)) + "\n\n" + card_text(lang, participant),
        parse_mode=ParseMode.MARKDOWN, reply_markup=menu_keyboard(lang, participant),
    )
    schedule_reminders(context.application, participant)

    ud["awaiting_college"] = participant["id"]
    await context.bot.send_message(
        update.effective_chat.id, t(lang, "ask_college"),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_skip"), callback_data="skipcollege")]]),
    )


# --------------------------------------------------------------------------- #
# Post-registration actions
# --------------------------------------------------------------------------- #
async def on_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
    lang = lang_of(context)
    participant = await current_participant(update, context)
    target = update.effective_message
    if not participant:
        await target.reply_text(t(lang, "not_registered"))
        return
    await target.reply_text(card_text(lang, participant), parse_mode=ParseMode.MARKDOWN,
                            reply_markup=menu_keyboard(lang, participant))


async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = lang_of(context)
    participant = await current_participant(update, context)
    if not participant:
        await update.message.reply_text(t(lang, "not_registered"))
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        t(lang, "invite_cta"),
        url=share_url(participant["invite_link"], t(lang, "invite_share_text", event=participant["event_name"])),
    )]])
    await update.message.reply_text(t(lang, "invite_explain", link=participant["invite_link"]),
                                    reply_markup=keyboard, disable_web_page_preview=True)


async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = lang_of(context)
    participant = await current_participant(update, context)
    if not participant:
        await update.message.reply_text(t(lang, "not_registered"))
        return
    await api.post(f"/api/participants/{participant['id']}/confirm")
    await update.message.reply_text(t(lang, "confirmed"))


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = lang_of(context)
    participant = await owned_participant(update, int(query.data.split(":", 1)[1]))
    if not participant or participant["status"] == "withdrawn":
        await query.message.reply_text(t(lang, "not_registered"))
        return
    await api.post(f"/api/participants/{participant['id']}/confirm")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(t(participant["language"], "confirmed"))


async def on_cant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split(":", 1)[1])
    lang = lang_of(context)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_yes_withdraw"), callback_data=f"withdraw:{pid}")],
        [InlineKeyboardButton(t(lang, "btn_keep"), callback_data="keep")],
    ])
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(t(lang, "ask_withdraw"), reply_markup=keyboard)


async def on_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = lang_of(context)
    participant = await owned_participant(update, int(query.data.split(":", 1)[1]))
    if not participant:
        await query.edit_message_text(t(lang, "not_registered"))
        return
    await api.post(f"/api/participants/{participant['id']}/withdraw")
    cancel_reminders(context.application, participant["id"])
    context.user_data.pop("participant_id", None)
    await query.edit_message_text(t(lang, "withdrawn"))


async def on_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(t(lang_of(context), "kept"))


async def on_skip_college(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting_college", None)
    await query.edit_message_text(t(lang_of(context), "skipped"))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = lang_of(context)
    pid = context.user_data.get("awaiting_college")
    if pid:
        college = update.message.text.strip()[:120]
        await api.patch(f"/api/participants/{pid}", json={"college": college})
        context.user_data.pop("awaiting_college", None)
        await update.message.reply_text(t(lang, "college_saved", college=college))
        return
    await update.message.reply_text(t(lang, "help"), parse_mode=ParseMode.MARKDOWN)


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("English", callback_data="setlang:en"),
        InlineKeyboardButton("हिंदी", callback_data="setlang:hi"),
    ]])
    await update.message.reply_text("Choose your language / अपनी भाषा चुनें", reply_markup=keyboard)


async def on_set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    context.user_data["lang"] = lang
    participant = await current_participant(update, context)
    if participant:
        await api.patch(f"/api/participants/{participant['id']}", json={"language": lang})
    await query.edit_message_text(t(lang, "lang_set"))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(t(lang_of(context), "help"), parse_mode=ParseMode.MARKDOWN)


async def demo_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fire the 1-day reminder in 10 seconds — handy for live demos."""
    lang = lang_of(context)
    participant = await current_participant(update, context)
    if not participant:
        await update.message.reply_text(t(lang, "not_registered"))
        return
    context.job_queue.run_once(
        reminder_job, when=10, chat_id=update.effective_chat.id,
        name=f"demo:{participant['id']}", data={"participant_id": participant["id"], "key": "d1"},
    )
    await update.message.reply_text(t(lang, "demo_scheduled"))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        lang = context.user_data.get("lang", "en") if context.user_data else "en"
        try:
            await context.bot.send_message(update.effective_chat.id, t(lang, "error"))
        except Exception:  # noqa: BLE001 — never let the error handler raise
            log.exception("Could not deliver error message")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def post_init(application: Application) -> None:
    global api
    api = RadarApi(API_URL)
    await application.bot.set_my_commands([
        BotCommand("start", "Register or see your squad"),
        BotCommand("team", "Your team card"),
        BotCommand("invite", "Invite teammates"),
        BotCommand("confirm", "Confirm attendance"),
        BotCommand("lang", "English / हिंदी"),
        BotCommand("help", "All commands"),
    ])
    # Job queues live in memory, so rebuild every pending reminder from the database on boot.
    try:
        targets = await api.get("/api/reminders") or []
        total = sum(schedule_reminders(application, p) for p in targets)
        log.info("Rescheduled %d reminders for %d participants", total, len(targets))
    except httpx.HTTPError as exc:
        log.warning("Could not reach the API at %s to restore reminders: %s", API_URL, exc)


async def post_shutdown(application: Application) -> None:
    if api is not None:
        await api.close()


def build_application() -> Application:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (get one from @BotFather) before starting the bot.")
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .persistence(PicklePersistence(filepath=PERSISTENCE_FILE))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("team", on_team))
    application.add_handler(CommandHandler("invite", invite_cmd))
    application.add_handler(CommandHandler("confirm", confirm_cmd))
    application.add_handler(CommandHandler("lang", lang_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("demo_reminder", demo_reminder))

    application.add_handler(CallbackQueryHandler(on_language, pattern=r"^lang:(en|hi)$"))
    application.add_handler(CallbackQueryHandler(on_set_language, pattern=r"^setlang:(en|hi)$"))
    application.add_handler(CallbackQueryHandler(on_role, pattern=r"^role:(developer|designer|domain_expert|presenter)$"))
    application.add_handler(CallbackQueryHandler(on_experience, pattern=r"^exp:(first_time|some|veteran)$"))
    application.add_handler(CallbackQueryHandler(on_team, pattern=r"^team$"))
    application.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(on_cant, pattern=r"^cant:\d+$"))
    application.add_handler(CallbackQueryHandler(on_withdraw, pattern=r"^withdraw:\d+$"))
    application.add_handler(CallbackQueryHandler(on_keep, pattern=r"^keep$"))
    application.add_handler(CallbackQueryHandler(on_skip_college, pattern=r"^skipcollege$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    if not os.getenv("TELEGRAM_BOT_USERNAME"):
        log.warning("TELEGRAM_BOT_USERNAME is not set — make sure the API uses your bot's username for links.")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
