"""Telegram handlers — commands, site-picker callbacks, document upload."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, storage
from .dashboard import format_start_dashboard, format_scan_dashboard
from .formatting import (
    BOT_CREDIT,
    format_hit,
    format_outcome,
    format_summary,
    format_delivery_summary,
    _detect_plan_label,
)
from .scanner import ScanOutcome, dump_netscape, scan_site


# ── Inline keyboards ─────────────────────────────────────────


def _sites_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for site in config.SUPPORTED_SITES:
        row.append(
            InlineKeyboardButton(
                f"{site['emoji']} {site['label']}",
                callback_data=f"site:{site['id']}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        InlineKeyboardButton("ℹ️ Help",     callback_data="help"),
    ])
    return InlineKeyboardMarkup(rows)



def _settings_keyboard(hit_on: bool) -> InlineKeyboardMarkup:
    toggle_label = (
        "🔔 Hit notifications: ON"
        if hit_on
        else "🔕 Hit notifications: OFF"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="toggle:hit_notifications")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="home")]
    ])


def _free_cookies_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, send free cookies", callback_data="send_free"),
            InlineKeyboardButton("❌ No thanks",              callback_data="home"),
        ]
    ])


# ── Static copy ──────────────────────────────────────────────

_HELP_TEXT = (
    "<b>How to use</b>\n"
    "1. Tap a site button (or use /check).\n"
    "2. Send a cookie export for that site:\n"
    "   • EditThisCookie / Cookie-Editor <code>.json</code>\n"
    "   • Netscape <code>cookies.txt</code> (yt-dlp format)\n"
    "   • Raw <code>Cookie:</code> header text\n"
    "   • A <code>.zip</code> of any of the above\n"
    "3. I check every cookie and reply with ALIVE / DEAD + full account info.\n"
    "4. If <i>hit notifications</i> is ON, each ALIVE cookie file is instantly "
    "sent as a <code>.txt</code> attachment.\n"
    "5. At the end I bundle all premium (paid-plan) cookies into a <code>.zip</code> "
    "and ask if you also want the free-tier ones.\n\n"
    "<b>Commands</b>\n"
    "/start — Site picker + live dashboard\n"
    "/check — Same as /start\n"
    "/sites — List supported sites\n"
    "/settings — Toggle hit notifications\n"
    "/help — This message\n"
    "/about — Credits"
)

_ABOUT_TEXT = (
    "<b>AIO Cookies Bot</b>\n"
    "Cookie validity + account info checker for 14 sites.\n\n"
    "👨‍💻 <b>Built &amp; maintained by</b> "
    '<a href="https://t.me/akaza_isnt">@akaza_isnt</a> (akaza).\n\n'
    "Every reply, exported <code>cookies.txt</code>, and hit notification "
    "carries the credit line so the bot stays attributable when shared.\n\n"
    + BOT_CREDIT
)



def _sites_text() -> str:
    lines = ["<b>Supported sites</b>"]
    for s in config.SUPPORTED_SITES:
        lines.append(f"  {s['emoji']} {s['label']} — <code>{s['id']}</code>")
    return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────


async def _reply_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if update.callback_query:
        q = update.callback_query
        try:
            await q.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except BadRequest:
            assert q.message is not None
            await q.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
    elif update.message is not None:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )


def _selected_site(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if context.user_data is None:
        return None
    site = context.user_data.get("selected_site")
    return str(site) if site else None


def _set_selected_site(context: ContextTypes.DEFAULT_TYPE, site_id: str) -> None:
    if context.user_data is not None:
        context.user_data["selected_site"] = site_id


def _set_pending_free(context: ContextTypes.DEFAULT_TYPE, outcomes: list[ScanOutcome]) -> None:
    """Stash free-tier outcomes so the 'yes' button can deliver them later."""
    if context.user_data is not None:
        context.user_data["pending_free_outcomes"] = outcomes


def _pop_pending_free(context: ContextTypes.DEFAULT_TYPE) -> list[ScanOutcome]:
    if context.user_data is None:
        return []
    return context.user_data.pop("pending_free_outcomes", []) or []



# ── Commands ─────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = await storage.get_dashboard_stats()
    await _reply_or_edit(update, format_start_dashboard(stats), _sites_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_or_edit(update, _HELP_TEXT, _back_keyboard())


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_or_edit(update, _ABOUT_TEXT, _back_keyboard())


async def cmd_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_or_edit(update, _sites_text(), _sites_keyboard())


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    hit_on = await storage.get_hit_notifications(user.id)
    text = (
        "<b>⚙️ Settings</b>\n\n"
        "When <i>hit notifications</i> is <b>ON</b>, every ✅ ALIVE result "
        "instantly sends the cookie file as a <code>.txt</code> attachment "
        "with a hit card showing email + plan + renewal date.\n\n"
        f"Current status: <b>{'🔔 ON' if hit_on else '🔕 OFF'}</b>"
    )
    await _reply_or_edit(update, text, _settings_keyboard(hit_on))


# ── Callback queries ──────────────────────────────────────────


async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None:
        return
    await q.answer()

    data = q.data
    user = update.effective_user

    if data == "home":
        await cmd_start(update, context)
        return
    if data == "help":
        await cmd_help(update, context)
        return
    if data == "settings":
        await cmd_settings(update, context)
        return

    if data.startswith("toggle:"):
        if user is None:
            return
        key = data.split(":", 1)[1]
        if key == "hit_notifications":
            new_val = await storage.toggle_bool(
                user.id, "hit_notifications",
                default=config.HIT_NOTIFICATIONS_DEFAULT,
            )
            text = (
                "<b>⚙️ Settings</b>\n\n"
                f"Hit notifications are now <b>{'🔔 ON' if new_val else '🔕 OFF'}</b>.\n\n"
                + (
                    "You will receive an instant alert + cookie file for every "
                    "ALIVE result." if new_val else
                    "No hit alerts will be sent."
                )
            )
            await _reply_or_edit(update, text, _settings_keyboard(new_val))
        return

    if data.startswith("site:"):
        site_id = data.split(":", 1)[1]
        if site_id not in config.SUPPORTED_SITE_IDS:
            await _reply_or_edit(update, "❌ Unknown site.", _sites_keyboard())
            return
        _set_selected_site(context, site_id)
        emoji = config.site_emoji(site_id)
        text = (
            f"{emoji} <b>{config.site_label(site_id)}</b> selected.\n\n"
            "Send me your cookie file:\n"
            "  • <code>.json</code> (EditThisCookie / Cookie-Editor)\n"
            "  • <code>.txt</code> (Netscape / yt-dlp)\n"
            "  • <code>.zip</code> of multiple cookie files"
        )
        await _reply_or_edit(update, text, _back_keyboard())
        return

    # ── "Yes, send free cookies" button ──────────────────────
    if data == "send_free":
        free_outcomes = _pop_pending_free(context)
        if not free_outcomes:
            await _reply_or_edit(update, "⚠️ No pending free cookies found.", _back_keyboard())
            return
        msg = q.message
        if msg is None:
            return
        await q.edit_message_reply_markup(reply_markup=None)  # remove buttons
        site_id = free_outcomes[0].site
        await _send_cookie_zip(msg, free_outcomes, site_id, label="free")
        await msg.reply_text(
            f"✅ Sent <b>{len(free_outcomes)}</b> free cookie file(s) above.",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
        )
        return



# ── Document upload ───────────────────────────────────────────


_ALLOWED_EXTS = {".json", ".txt", ".cookie", ".cookies", ".zip", ".header"}


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user
    if msg is None or msg.document is None or user is None:
        return

    site_id = _selected_site(context)
    if not site_id:
        await msg.reply_text(
            "ℹ️ Pick a site first — use /start or /check.",
            reply_markup=_sites_keyboard(),
        )
        return

    doc      = msg.document
    filename = doc.file_name or "cookies"
    ext      = Path(filename).suffix.lower()
    if ext and ext not in _ALLOWED_EXTS:
        await msg.reply_text(
            f"❌ Unsupported file type: <code>{ext}</code>.\nSend a .json, .txt, or .zip.",
            parse_mode=ParseMode.HTML,
        )
        return

    if doc.file_size and doc.file_size > config.MAX_FILE_BYTES:
        await msg.reply_text(
            f"❌ File too large ({doc.file_size} bytes). Max: {config.MAX_FILE_BYTES} bytes."
        )
        return

    # ── Initial status message ────────────────────────────────
    status_msg = await msg.reply_text(
        f"⏳ Downloading <b>{filename}</b>…",
        parse_mode=ParseMode.HTML,
    )

    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=ext or "", dir=str(config.TEMP_DIR)
    ) as tmp:
        tmp_path = tmp.name

    try:
        # Download
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(tmp_path)
        except Exception:
            logger.exception("Failed to download document for user {}", user.id)
            await status_msg.edit_text("❌ Failed to download your file from Telegram.")
            return

        # ── Is this a zip? Scan with live progress ────────────
        is_zip = ext == ".zip" or zipfile.is_zipfile(tmp_path)

        if is_zip:
            outcomes = await _scan_zip_with_progress(
                status_msg, site_id, tmp_path, filename, user, context
            )
        else:
            await status_msg.edit_text(
                f"⚙️ Checking <b>{filename}</b> against "
                f"<b>{config.site_label(site_id)}</b>…",
                parse_mode=ParseMode.HTML,
            )
            try:
                outcomes = await asyncio.wait_for(
                    scan_site(site_id, tmp_path, filename),
                    timeout=config.JOB_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text(
                    f"⏱️ Scan timed out after {config.JOB_TIMEOUT_SECONDS}s."
                )
                return
            except Exception as exc:
                logger.exception("Scan failed for user {}", user.id)
                await status_msg.edit_text(f"❌ Scan failed: {exc}")
                return

        # Delete status message
        try:
            await status_msg.delete()
        except BadRequest:
            pass

        # Record stats
        try:
            await storage.record_scan_outcomes(outcomes)
        except Exception:
            logger.exception("Failed to update dashboard stats")

        # Deliver results
        await _deliver_outcomes(update, context, site_id, outcomes)

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass



# ── Live-progress zip scanner ─────────────────────────────────


async def _scan_zip_with_progress(
    status_msg,
    site_id: str,
    zip_path: str,
    display_name: str,
    user,
    context: ContextTypes.DEFAULT_TYPE,
) -> list[ScanOutcome]:
    """Scan every file in a zip, editing the status message after each result."""
    import zipfile as _zf

    # Count files first
    try:
        with _zf.ZipFile(zip_path) as zf:
            names = [
                n for n in zf.namelist()
                if not n.endswith("/") and Path(n).suffix.lower() in _ALLOWED_EXTS
            ]
    except Exception:
        names = []

    total_files = max(len(names), 1)
    outcomes: list[ScanOutcome] = []
    plan_counts: dict[str, int] = {}
    alive_count = 0
    dead_count  = 0
    hit_on      = await storage.get_hit_notifications(user.id)

    # Update dashboard every N results to avoid flood-wait
    _UPDATE_EVERY = 3
    _last_edit    = 0.0

    import time as _time

    async def _maybe_update_dashboard(current_file: str = "") -> None:
        nonlocal _last_edit
        now = _time.monotonic()
        if now - _last_edit < 1.5 and current_file:
            return
        _last_edit = now
        text = format_scan_dashboard(
            site_id, len(outcomes), total_files,
            alive_count, dead_count, plan_counts, current_file,
        )
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except BadRequest:
            pass

    await _maybe_update_dashboard("starting…")

    # Run the full zip scan in a thread, but intercept each result
    # by using the existing scan_site path (which handles zip internally).
    # We re-implement per-file scanning here for live updates.
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(config.TEMP_DIR)) as extract_dir:
        try:
            with _zf.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except Exception as exc:
            return [ScanOutcome(site=site_id, filename=display_name, alive=False,
                                error=f"bad zip: {exc}")]

        from .scanner import load_cookies_from_path, scan_one_sync

        cookie_files = [
            str(Path(extract_dir) / n)
            for n in names
        ] or [
            str(p) for p in Path(extract_dir).rglob("*")
            if p.is_file() and p.suffix.lower() in _ALLOWED_EXTS
        ]

        if not cookie_files:
            return [ScanOutcome(site=site_id, filename=display_name, alive=False,
                                error="no cookie files found inside zip")]

        for fp in cookie_files:
            fname = Path(fp).name
            await _maybe_update_dashboard(fname)

            try:
                cookies = await asyncio.to_thread(load_cookies_from_path, fp)
                outcome = await asyncio.to_thread(
                    scan_one_sync, site_id, cookies, fname, None
                )
            except Exception as exc:
                outcome = ScanOutcome(site=site_id, filename=fname, alive=False,
                                      error=f"load/scan error: {exc}")

            outcomes.append(outcome)

            if outcome.alive:
                alive_count += 1
                plan = _detect_plan_label(site_id, outcome.info or {})
                if plan:
                    plan_counts[plan] = plan_counts.get(plan, 0) + 1
                # ── INSTANT HIT NOTIFICATION (only when ON) ──────────
                if hit_on:
                    try:
                        await _send_hit(status_msg, outcome)
                    except Exception:
                        logger.exception("Failed to send hit notification")
            else:
                dead_count += 1

            await _maybe_update_dashboard(fname)

    # Final live dashboard state
    await _maybe_update_dashboard("")
    return outcomes



# ── Outcome delivery ──────────────────────────────────────────


async def _deliver_outcomes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    site_id: str,
    outcomes: list[ScanOutcome],
) -> None:
    msg  = update.message
    user = update.effective_user
    if msg is None or user is None:
        return

    # ── FIX: read hit_notifications ONCE and use it consistently ──
    hit_on = await storage.get_hit_notifications(user.id)

    # ── Summary card (multi-file only) ────────────────────────
    if len(outcomes) > 1:
        await msg.reply_text(
            format_summary(outcomes, site_id),
            parse_mode=ParseMode.HTML,
        )

    # ── Per-outcome messages ───────────────────────────────────
    # For single files, show detailed outcome. For large zips keep it brief.
    show_individual = len(outcomes) <= 20
    for outcome in outcomes:
        if show_individual:
            await msg.reply_text(
                format_outcome(outcome),
                parse_mode=ParseMode.HTML,
                reply_markup=_back_keyboard() if len(outcomes) == 1 else None,
            )

        # Hit notification — ONLY send when hit_on is True
        # (This is the bug fix: previously this ran regardless)
        if outcome.alive and hit_on:
            await _send_hit(msg, outcome)

    if not show_individual:
        await msg.reply_text(
            f"ℹ️ <b>{len(outcomes)}</b> results total — summary above shows all details.",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
        )

    # ── Split into premium / free ─────────────────────────────
    premium_outcomes = [o for o in outcomes if o.alive and _is_premium(site_id, o)]
    free_outcomes    = [o for o in outcomes if o.alive and not _is_premium(site_id, o)]

    # ── Send premium .zip ──────────────────────────────────────
    if premium_outcomes:
        await _send_cookie_zip(msg, premium_outcomes, site_id, label="premium")

    # ── Record premium / free counts ──────────────────────────
    try:
        await storage.record_tier_counts(
            site_id,
            premium=len(premium_outcomes),
            free=len(free_outcomes),
        )
    except Exception:
        logger.exception("Failed to record tier counts")

    # ── Delivery summary + free cookies offer ─────────────────
    summary_text = format_delivery_summary(
        site_id, outcomes,
        premium_sent=bool(premium_outcomes),
        free_available=len(free_outcomes),
    )

    if free_outcomes:
        _set_pending_free(context, free_outcomes)
        await msg.reply_text(
            summary_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_free_cookies_keyboard(),
        )
    else:
        await msg.reply_text(
            summary_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
        )


def _is_premium(site_id: str, outcome: ScanOutcome) -> bool:
    """Return True if this alive account is on a paid / non-free plan."""
    plan = _detect_plan_label(site_id, outcome.info or {})
    if not plan:
        return False
    return plan.lower() not in {"free", "unknown", "trial"}



# ── Hit / zip helpers ─────────────────────────────────────────


async def _send_hit(msg_obj, outcome: ScanOutcome) -> None:
    """Send a single ALIVE cookie file as a hit notification."""
    netscape = dump_netscape(outcome.cookies, default_domain=outcome.site)
    if not netscape.strip():
        return
    bio      = io.BytesIO(netscape.encode("utf-8"))
    bio.name = _hit_filename(outcome)
    await msg_obj.reply_document(
        document=InputFile(bio, filename=bio.name),
        caption=format_hit(outcome),
        parse_mode=ParseMode.HTML,
    )


async def _send_cookie_zip(
    msg_obj,
    outcomes: list[ScanOutcome],
    site_id: str,
    label: str = "cookies",
) -> None:
    """Bundle all outcomes into a .zip and send it."""
    if not outcomes:
        return

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for outcome in outcomes:
            netscape = dump_netscape(outcome.cookies, default_domain=outcome.site)
            if not netscape.strip():
                continue
            fname = _hit_filename(outcome)
            zf.writestr(fname, netscape.encode("utf-8"))

    buf.seek(0)
    if buf.getbuffer().nbytes == 0:
        return

    site_slug = site_id.replace(".", "_")
    zip_name  = f"@akaza_{site_slug}_{label}.zip"
    count     = len(outcomes)
    emoji     = config.site_emoji(site_id)

    caption = (
        f"📦 <b>{emoji} {config.site_label(site_id)} — {label.title()} Cookies</b>\n"
        f"  🗂 <b>{count}</b> account(s) inside\n\n"
        + BOT_CREDIT
    )

    await msg_obj.reply_document(
        document=InputFile(buf, filename=zip_name),
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


def _hit_filename(outcome: ScanOutcome) -> str:
    import hashlib
    site_slug = outcome.site.replace(".", "_")
    payload   = "|".join(
        f"{c.get('name', '')}={c.get('value', '')}"
        for c in outcome.cookies
    )
    short = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
    return f"@akaza_{site_slug}_{short}.txt"


# ── Catch-all ─────────────────────────────────────────────────


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    site_id = _selected_site(context)
    if site_id:
        await update.message.reply_text(
            f"ℹ️ I'm waiting for your <b>{config.site_label(site_id)}</b> "
            "cookie file (.json / .txt / .zip).",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_keyboard(),
        )
    else:
        await update.message.reply_text(
            "ℹ️ Pick a site first — use /start or /check.",
            reply_markup=_sites_keyboard(),
        )


# ── Registration ──────────────────────────────────────────────


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("check",    cmd_start))
    app.add_handler(CommandHandler("sites",    cmd_sites))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("about",    cmd_about))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
