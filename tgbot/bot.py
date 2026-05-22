"""Entry point — builds the python-telegram-bot Application and runs it."""

from __future__ import annotations

import sys

from loguru import logger
from telegram import BotCommand
from telegram.ext import Application, ContextTypes

from . import config, handlers


# ── Logging ─────────────────────────────────────────────────


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
    )
    logger.add(
        config.LOG_FILE,
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {module}:{function} | {message}",
    )


# ── Error handler ───────────────────────────────────────────


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: {}", context.error, exc_info=context.error)
    if config.ADMIN_ID:
        try:
            await context.bot.send_message(
                config.ADMIN_ID,
                f"\U0001f6a8 Bot error\n{type(context.error).__name__}: {context.error}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify admin about error")


async def _post_init(app: Application) -> None:
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start",     "Dashboard + site picker"),
                BotCommand("check",     "Check cookies for a site"),
                BotCommand("profile",   "Your profile, role & stats"),
                BotCommand("settings",  "Toggle hit notifications"),
                BotCommand("sites",     "List supported sites"),
                BotCommand("help",      "How to use the bot"),
                BotCommand("about",     "About this bot"),
            ]
        )
        # Admin-only commands (shown only to ADMIN_ID via scope)
        from telegram import BotCommandScopeChat
        from . import config as _cfg
        if _cfg.ADMIN_ID:
            await app.bot.set_my_commands(
                [
                    BotCommand("admin",     "Admin panel"),
                    BotCommand("give",      "Give role: /give <id> premium|free|admin"),
                    BotCommand("ban",       "Ban a user: /ban <id> [reason]"),
                    BotCommand("unban",     "Unban a user: /unban <id>"),
                    BotCommand("userinfo",  "User details: /userinfo <id>"),
                    BotCommand("users",     "List all users"),
                    BotCommand("broadcast", "Broadcast a message"),
                ],
                scope=BotCommandScopeChat(chat_id=_cfg.ADMIN_ID),
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to register slash-command menu")
    logger.info("Bot initialised \u2014 polling for updates")


# ── Main ────────────────────────────────────────────────────


def main() -> None:
    _setup_logging()
    if not config.BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set \u2014 refusing to start")
        sys.exit(1)

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )

    handlers.register(app)
    app.add_error_handler(_error_handler)

    logger.info("Starting AIO Cookies bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
