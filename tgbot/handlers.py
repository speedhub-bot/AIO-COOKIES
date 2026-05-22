"""Telegram handlers — commands, site-picker callbacks, document upload."""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, storage
from .constants import BOT_CREDIT
from .dashboard import format_start_dashboard, format_scan_dashboard
from .formatting import (
    _detect_plan_label,
    format_delivery_summary,
    format_hit,
    format_outcome,
    format_summary,
)
from .scanner import ScanOutcome, dump_netscape, scan_site


# ═══════════════════════════════════════════════════════════════
#  Channel-membership gate
# ═══════════════════════════════════════════════════════════════

async def _is_member(bot, user_id: int) -> bool:
    """Return True if *user_id* is a member of the required channel.

    Fail-closed: if Telegram returns any error checking membership
    (channel doesn't exist, bot isn't admin in the channel, etc.) we
    return False so the user is shown the join screen. Otherwise the
    gate becomes a no-op and new users sail straight through.

    The admin-id (config.ADMIN_ID) is always exempt — checked by callers
    so the bot owner can never lock themselves out by misconfiguring
    REQUIRED_CHANNEL.
    """
    if not config.REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(config.REQUIRED_CHANNEL, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as exc:
        # Channel-not-found / bot-not-admin / private-chat-not-supported all
        # land here. Log once per error so the admin can see why no one is
        # being let through; default behaviour is fail-closed (show join btn).
        logger.warning(
            "Channel gate: get_chat_member({}, {}) failed: {}",
            config.REQUIRED_CHANNEL, user_id, exc,
        )
        return False


def _join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔗 {config.CHANNEL_DISPLAY_NAME}",
            url=config.REQUIRED_CHANNEL_INVITE,
        )],
        [InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_membership")],
    ])



# ═══════════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════════

def _sites_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row:  list[InlineKeyboardButton]       = []
    for site in config.SUPPORTED_SITES:
        row.append(InlineKeyboardButton(
            f"{site['emoji']} {site['label']}",
            callback_data=f"site:{site['id']}",
        ))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        InlineKeyboardButton("ℹ️ Help",     callback_data="help"),
        InlineKeyboardButton("👤 Profile",  callback_data="profile"),
    ])
    return InlineKeyboardMarkup(rows)


def _settings_keyboard(hit_on: bool) -> InlineKeyboardMarkup:
    lbl = "🔔 Hit notifications: ON" if hit_on else "🔕 Hit notifications: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl, callback_data="toggle:hit_notifications")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])


def _free_cookies_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, send free cookies", callback_data="send_free"),
            InlineKeyboardButton("❌ No thanks",              callback_data="home"),
        ]
    ])



# ═══════════════════════════════════════════════════════════════
#  Static copy
# ═══════════════════════════════════════════════════════════════

_HELP_TEXT = (
    "<b>How to use</b>\n"
    "1. Tap a site button (or /check).\n"
    "2. Send a cookie export:\n"
    "   • <code>.json</code> (EditThisCookie / Cookie-Editor)\n"
    "   • <code>.txt</code> (Netscape / yt-dlp)\n"
    "   • <code>.zip</code> of multiple cookie files\n"
    "3. I check every file and reply ALIVE / DEAD + full account info.\n"
    "4. If <i>hit notifications</i> is ON, each ALIVE file is instantly sent.\n"
    "5. Premium cookies are bundled into a <code>.zip</code> at the end.\n\n"
    "<b>Commands</b>\n"
    "/start — Dashboard + site picker\n"
    "/profile — Your stats and role\n"
    "/settings — Toggle hit notifications\n"
    "/help — This message"
)

_ABOUT_TEXT = (
    "<b>AIO Cookies Bot</b>\n"
    "Cookie validity checker for 14 services.\n\n"
    '👨‍💻 <b>By</b> <a href="https://t.me/akaza_isnt">@akaza_isnt</a>\n\n'
    + BOT_CREDIT
)


def _esc(v: Any) -> str:
    return html.escape(str(v), quote=False)


def _sites_text() -> str:
    lines = ["<b>Supported sites</b>"]
    for s in config.SUPPORTED_SITES:
        lines.append(f"  {s['emoji']} {s['label']} — <code>{s['id']}</code>")
    return "\n".join(lines)


def _role_badge(role: str) -> str:
    return {"admin": "👑 Admin", "premium": "💎 Premium", "free": "🆓 Free"}.get(role, role)



# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

async def _reply_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if update.callback_query:
        q = update.callback_query
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=reply_markup,
            )
        except BadRequest:
            assert q.message is not None
            await q.message.reply_text(
                text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=reply_markup,
            )
    elif update.message is not None:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=reply_markup,
        )


def _selected_site(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    if ctx.user_data is None: return None
    s = ctx.user_data.get("selected_site")
    return str(s) if s else None


def _set_selected_site(ctx: ContextTypes.DEFAULT_TYPE, site_id: str) -> None:
    if ctx.user_data is not None:
        ctx.user_data["selected_site"] = site_id


def _set_pending_free(ctx: ContextTypes.DEFAULT_TYPE, outcomes: list[ScanOutcome]) -> None:
    if ctx.user_data is not None:
        ctx.user_data["pending_free_outcomes"] = outcomes


def _pop_pending_free(ctx: ContextTypes.DEFAULT_TYPE) -> list[ScanOutcome]:
    if ctx.user_data is None: return []
    return ctx.user_data.pop("pending_free_outcomes", []) or []


async def _register_user(update: Update) -> None:
    """Upsert user in registry from any update."""
    u = update.effective_user
    if u is None: return
    await storage.get_or_create_user(
        u.id,
        username=u.username or "",
        first_name=u.first_name or "",
    )


async def _gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if user is allowed (joined channel + not banned).
    Sends the appropriate message and returns False if blocked."""
    user = update.effective_user
    if user is None:
        return False

    # 1) Register / upsert
    await _register_user(update)

    # 2) Ban check
    if await storage.is_banned(user.id):
        entry = await storage.get_user(user.id)
        reason = (entry or {}).get("ban_reason", "")
        text = (
            "🚫 <b>You are banned from this bot.</b>"
            + (f"\n\nReason: <i>{_esc(reason)}</i>" if reason else "")
        )
        await _reply_or_edit(update, text)
        return False

    # 3) Channel membership check (skip for ADMIN_ID)
    if user.id != config.ADMIN_ID:
        if not await _is_member(context.bot, user.id):
            text = (
                "👋 <b>Welcome to AIO Cookies Bot!</b>\n\n"
                "To use this bot you must join our channel first.\n\n"
                f"1️⃣ Click <b>{_esc(config.CHANNEL_DISPLAY_NAME)}</b> below to join\n"
                "2️⃣ Come back and tap <b>✅ I've Joined — Verify</b>"
            )
            await _reply_or_edit(update, text, _join_keyboard())
            return False

    return True



# ═══════════════════════════════════════════════════════════════
#  Commands
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context):
        return
    stats = await storage.get_dashboard_stats()
    await _reply_or_edit(update, format_start_dashboard(stats), _sites_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    await _reply_or_edit(update, _HELP_TEXT, _back_keyboard())


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    await _reply_or_edit(update, _ABOUT_TEXT, _back_keyboard())


async def cmd_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    await _reply_or_edit(update, _sites_text(), _sites_keyboard())


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    user = update.effective_user
    if user is None: return
    hit_on = await storage.get_hit_notifications(user.id)
    text = (
        "<b>⚙️ Settings</b>\n\n"
        "When <i>hit notifications</i> is <b>ON</b>, every ✅ ALIVE result "
        "instantly sends the cookie file as a <code>.txt</code> attachment.\n\n"
        f"Current status: <b>{'🔔 ON' if hit_on else '🔕 OFF'}</b>"
    )
    await _reply_or_edit(update, text, _settings_keyboard(hit_on))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    user = update.effective_user
    if user is None: return
    u = await storage.get_or_create_user(
        user.id, user.username or "", user.first_name or "")
    role   = u.get("role", config.ROLE_FREE)
    # admin override
    if user.id == config.ADMIN_ID:
        role = config.ROLE_ADMIN
    badge  = _role_badge(role)
    joined = u.get("joined_at","")
    try:
        joined_fmt = datetime.fromisoformat(joined).strftime("%d %b %Y") if joined else "—"
    except Exception:
        joined_fmt = str(joined)
    prem_since = u.get("premium_since")
    try:
        prem_fmt = datetime.fromisoformat(prem_since).strftime("%d %b %Y") if prem_since else "—"
    except Exception:
        prem_fmt = str(prem_since)

    svcs: dict = u.get("services_used") or {}
    top_svcs = sorted(svcs.items(), key=lambda x: -x[1])[:5]

    lines = [
        f"👤 <b>Profile</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  🏷 Name        : <b>{_esc(user.first_name or 'Unknown')}</b>",
        f"  🆔 User ID     : <code>{user.id}</code>",
        f"  📛 Username    : {'@'+_esc(user.username) if user.username else '—'}",
        f"  🎭 Role        : <b>{badge}</b>",
        f"  📅 Member since: <b>{joined_fmt}</b>",
    ]
    if prem_since:
        lines.append(f"  💎 Premium since: <b>{prem_fmt}</b>")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  🧮 Total checked: <b>{u.get('total_checked',0)}</b>",
        f"  ✅ Total hits   : <b>{u.get('total_hits',0)}</b>",
    ]
    if top_svcs:
        lines.append("  📋 Top services :")
        for sid, cnt in top_svcs:
            emoji = config.site_emoji(sid)
            lines.append(f"      {emoji} {_esc(sid)} — <b>{cnt}</b> checked")
    recent: list = u.get("recent_checks") or []
    if recent:
        lines.append("  🕒 Recent checks:")
        for r in reversed(recent[-5:]):
            icon = "✅" if r.get("alive") else "❌"
            ts   = (r.get("ts","")[:10])
            lines.append(f"      {icon} {_esc(r.get('site','?'))}  <code>{_esc(r.get('filename',''))}</code>  {ts}")
    lines += ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "", BOT_CREDIT]
    await _reply_or_edit(update, "\n".join(lines), _back_keyboard())



# ═══════════════════════════════════════════════════════════════
#  Callback router
# ═══════════════════════════════════════════════════════════════

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None: return
    await q.answer()
    data = q.data
    user = update.effective_user

    # ── verify membership button ──────────────────────────────
    if data == "verify_membership":
        if user and await _is_member(context.bot, user.id):
            await _reply_or_edit(update, "✅ Verified! Welcome.", _sites_keyboard())
            stats = await storage.get_dashboard_stats()
            await _reply_or_edit(update, format_start_dashboard(stats), _sites_keyboard())
        else:
            await q.answer("❌ You haven't joined yet!", show_alert=True)
        return

    # ── gate all other callbacks ───────────────────────────────
    if not await _gate(update, context): return

    if data == "home":
        await cmd_start(update, context); return
    if data == "help":
        await cmd_help(update, context); return
    if data == "about":
        await cmd_about(update, context); return
    if data == "settings":
        await cmd_settings(update, context); return
    if data == "profile":
        await cmd_profile(update, context); return

    if data.startswith("toggle:"):
        if user is None: return
        key = data.split(":", 1)[1]
        if key == "hit_notifications":
            new_val = await storage.toggle_bool(
                user.id, "hit_notifications",
                default=config.HIT_NOTIFICATIONS_DEFAULT,
            )
            text = (
                "<b>⚙️ Settings</b>\n\n"
                f"Hit notifications are now <b>{'🔔 ON' if new_val else '🔕 OFF'}</b>.\n\n"
                + ("Instant alert + file for every ALIVE result." if new_val
                   else "No hit alerts will be sent.")
            )
            await _reply_or_edit(update, text, _settings_keyboard(new_val))
        return

    if data.startswith("site:"):
        site_id = data.split(":", 1)[1]
        if site_id not in config.SUPPORTED_SITE_IDS:
            await _reply_or_edit(update, "❌ Unknown site.", _sites_keyboard()); return
        _set_selected_site(context, site_id)
        text = (
            f"{config.site_emoji(site_id)} <b>{config.site_label(site_id)}</b> selected.\n\n"
            "Send your cookie file:\n"
            "  • <code>.json</code>  • <code>.txt</code>  • <code>.zip</code>"
        )
        await _reply_or_edit(update, text, _back_keyboard()); return

    if data == "send_free":
        free_outcomes = _pop_pending_free(context)
        if not free_outcomes:
            await _reply_or_edit(update, "⚠️ No pending free cookies.", _back_keyboard()); return
        msg = q.message
        if msg is None: return
        await q.edit_message_reply_markup(reply_markup=None)
        site_id = free_outcomes[0].site
        await _send_cookie_zip(msg, free_outcomes, site_id, label="free")
        await msg.reply_text(
            f"✅ Sent <b>{len(free_outcomes)}</b> free cookie file(s).",
            parse_mode=ParseMode.HTML, reply_markup=_back_keyboard())
        return



# ═══════════════════════════════════════════════════════════════
#  Document upload
# ═══════════════════════════════════════════════════════════════

_ALLOWED_EXTS = {".json", ".txt", ".cookie", ".cookies", ".zip", ".header"}


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    msg  = update.message
    user = update.effective_user
    if msg is None or msg.document is None or user is None: return

    site_id = _selected_site(context)
    if not site_id:
        await msg.reply_text("ℹ️ Pick a site first — use /start.",
                             reply_markup=_sites_keyboard()); return

    doc      = msg.document
    filename = doc.file_name or "cookies"
    ext      = Path(filename).suffix.lower()
    if ext and ext not in _ALLOWED_EXTS:
        await msg.reply_text(f"❌ Unsupported: <code>{ext}</code>.",
                             parse_mode=ParseMode.HTML); return
    if doc.file_size and doc.file_size > config.MAX_FILE_BYTES:
        await msg.reply_text(f"❌ File too large."); return

    status_msg = await msg.reply_text(
        f"⏳ Downloading <b>{_esc(filename)}</b>…", parse_mode=ParseMode.HTML)

    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=ext or "", dir=str(config.TEMP_DIR)
    ) as tmp:
        tmp_path = tmp.name

    try:
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(tmp_path)
        except Exception:
            logger.exception("Download failed for user {}", user.id)
            await status_msg.edit_text("❌ Failed to download your file."); return

        is_zip = ext == ".zip" or zipfile.is_zipfile(tmp_path)
        if is_zip:
            outcomes, hits_already_sent = await _scan_zip_with_progress(
                status_msg, site_id, tmp_path, filename, user, context)
        else:
            await status_msg.edit_text(
                f"⚙️ Checking <b>{_esc(filename)}</b>…", parse_mode=ParseMode.HTML)
            try:
                outcomes = await asyncio.wait_for(
                    scan_site(site_id, tmp_path, filename),
                    timeout=config.JOB_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text(f"⏱️ Timed out after {config.JOB_TIMEOUT_SECONDS}s."); return
            except Exception as exc:
                logger.exception("Scan failed for user {}", user.id)
                await status_msg.edit_text(f"❌ Scan error: {exc}"); return
            hits_already_sent = set()

        try: await status_msg.delete()
        except BadRequest: pass

        try: await storage.record_scan_outcomes(outcomes)
        except Exception: logger.exception("Dashboard update failed")

        # Record per-user stats
        try: await storage.record_user_scan(user.id, site_id, outcomes)
        except Exception: logger.exception("User scan record failed")

        await _deliver_outcomes(update, context, site_id, outcomes,
                                hits_already_sent=hits_already_sent)
    finally:
        try: os.remove(tmp_path)
        except OSError: pass



# ═══════════════════════════════════════════════════════════════
#  Live-progress zip scanner
# ═══════════════════════════════════════════════════════════════

async def _scan_zip_with_progress(
    status_msg,
    site_id: str,
    zip_path: str,
    display_name: str,
    user,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[list[ScanOutcome], set[int]]:
    """Scan a zip with live progress.

    Returns ``(outcomes, hit_indices_already_sent)`` so the caller can
    avoid re-sending the same hit notifications during _deliver_outcomes.
    """
    import zipfile as _zf, time as _t

    try:
        with _zf.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist()
                     if not n.endswith("/") and Path(n).suffix.lower() in _ALLOWED_EXTS]
    except Exception:
        names = []

    total_files = max(len(names), 1)
    outcomes: list[ScanOutcome] = []
    plan_counts: dict[str, int] = {}
    alive_count = dead_count = err_count = 0
    hit_on      = await storage.get_hit_notifications(user.id)
    hits_sent: set[int] = set()
    _last_edit  = 0.0
    scan_started = _t.monotonic()

    async def _upd(current_file: str = "") -> None:
        nonlocal _last_edit
        now = _t.monotonic()
        if now - _last_edit < 1.5 and current_file: return
        _last_edit = now
        # checks-per-minute over the active scan window
        elapsed = max(now - scan_started, 0.001)
        cpm = (len(outcomes) / elapsed) * 60.0 if outcomes else 0.0
        text = format_scan_dashboard(
            site_id, len(outcomes), total_files,
            alive_count, dead_count, plan_counts, current_file,
            errored=err_count, cpm=cpm)
        try: await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except BadRequest: pass

    await _upd("starting…")
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(config.TEMP_DIR)) as extract_dir:
        try:
            with _zf.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except Exception as exc:
            return ([ScanOutcome(site=site_id, filename=display_name,
                                 alive=False, error=f"bad zip: {exc}")], hits_sent)

        from .scanner import load_cookies_from_path, scan_one_sync
        cookie_files = (
            [str(Path(extract_dir) / n) for n in names]
            or [str(p) for p in Path(extract_dir).rglob("*")
                if p.is_file() and p.suffix.lower() in _ALLOWED_EXTS]
        )
        if not cookie_files:
            return ([ScanOutcome(site=site_id, filename=display_name,
                                 alive=False, error="no cookie files in zip")], hits_sent)

        for idx, fp in enumerate(cookie_files):
            fname = Path(fp).name
            await _upd(fname)
            # round-robin proxy for each file
            proxy = await storage.get_next_proxy() or config.DEFAULT_PROXY or None
            try:
                cookies = await asyncio.to_thread(load_cookies_from_path, fp)
                outcome = await asyncio.to_thread(scan_one_sync, site_id, cookies, fname, proxy)
            except Exception as exc:
                outcome = ScanOutcome(site=site_id, filename=fname,
                                      alive=False, error=f"error: {exc}")
            outcomes.append(outcome)
            if outcome.alive:
                alive_count += 1
                plan = _detect_plan_label(site_id, outcome.info or {})
                if plan: plan_counts[plan] = plan_counts.get(plan, 0) + 1
                if hit_on:
                    try:
                        await _send_hit(status_msg, outcome)
                        # Remember this index so _deliver_outcomes doesn't
                        # send the same hit a second time.
                        hits_sent.add(idx)
                    except Exception:
                        logger.exception("hit notification failed")
            elif outcome.error:
                err_count += 1
            else:
                dead_count += 1
            await _upd(fname)
            # Throttle a little so we don't hammer the target API and so
            # the Telegram dashboard edits stay readable.
            await asyncio.sleep(config.SCAN_DELAY_SECONDS)

    await _upd("")
    return outcomes, hits_sent



# ═══════════════════════════════════════════════════════════════
#  Delivery
# ═══════════════════════════════════════════════════════════════

async def _deliver_outcomes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    site_id: str,
    outcomes: list[ScanOutcome],
    hits_already_sent: set[int] | None = None,
) -> None:
    msg  = update.message
    user = update.effective_user
    if msg is None or user is None: return

    if hits_already_sent is None:
        hits_already_sent = set()

    hit_on = await storage.get_hit_notifications(user.id)

    # NOTE: we intentionally don't send `format_summary` for multi-file
    # scans anymore — `format_delivery_summary` (sent below) covers the
    # same info and was being duplicated. Single-file scans get the
    # detailed `format_outcome` and that's it.
    show_individual = len(outcomes) <= 20
    for idx, outcome in enumerate(outcomes):
        if show_individual:
            await msg.reply_text(
                format_outcome(outcome), parse_mode=ParseMode.HTML,
                reply_markup=_back_keyboard() if len(outcomes) == 1 else None,
            )
        # Only send the hit document if the zip-scanner didn't already
        # send it during the live progress phase. Prevents the duplicate
        # "alive cookie file + hit caption" message users were seeing.
        if outcome.alive and hit_on and idx not in hits_already_sent:
            await _send_hit(msg, outcome)

    if not show_individual:
        await msg.reply_text(
            f"ℹ️ <b>{len(outcomes)}</b> results — see summary below.",
            parse_mode=ParseMode.HTML, reply_markup=_back_keyboard())

    premium_outcomes = [o for o in outcomes if o.alive and _is_premium(site_id, o)]
    free_outcomes    = [o for o in outcomes if o.alive and not _is_premium(site_id, o)]

    if premium_outcomes:
        await _send_cookie_zip(msg, premium_outcomes, site_id, label="premium")

    try:
        await storage.record_tier_counts(
            site_id, premium=len(premium_outcomes), free=len(free_outcomes))
    except Exception: logger.exception("tier count failed")

    summary_text = format_delivery_summary(
        site_id, outcomes,
        premium_sent=bool(premium_outcomes),
        free_available=len(free_outcomes),
    )
    if free_outcomes:
        _set_pending_free(context, free_outcomes)
        await msg.reply_text(summary_text, parse_mode=ParseMode.HTML,
                             reply_markup=_free_cookies_keyboard())
    else:
        await msg.reply_text(summary_text, parse_mode=ParseMode.HTML,
                             reply_markup=_back_keyboard())


def _is_premium(site_id: str, outcome: ScanOutcome) -> bool:
    plan = _detect_plan_label(site_id, outcome.info or {})
    if not plan: return False
    return plan.lower() not in {"free", "unknown", "trial"}


# ── Hit / zip file helpers ────────────────────────────────────

async def _send_hit(msg_obj, outcome: ScanOutcome) -> None:
    netscape = dump_netscape(outcome.cookies, default_domain=outcome.site)
    if not netscape.strip(): return
    bio      = io.BytesIO(netscape.encode("utf-8"))
    bio.name = _hit_filename(outcome)
    await msg_obj.reply_document(
        document=InputFile(bio, filename=bio.name),
        caption=format_hit(outcome), parse_mode=ParseMode.HTML,
    )


async def _send_cookie_zip(msg_obj, outcomes: list[ScanOutcome],
                           site_id: str, label: str = "cookies") -> None:
    if not outcomes: return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for o in outcomes:
            ns = dump_netscape(o.cookies, default_domain=o.site)
            if ns.strip():
                zf.writestr(_hit_filename(o), ns.encode("utf-8"))
    buf.seek(0)
    if buf.getbuffer().nbytes == 0: return
    slug     = site_id.replace(".", "_")
    zip_name = f"@akaza_{slug}_{label}.zip"
    caption  = (
        f"📦 <b>{config.site_emoji(site_id)} {config.site_label(site_id)} "
        f"— {label.title()} Cookies</b>\n"
        f"  🗂 <b>{len(outcomes)}</b> account(s)\n\n{BOT_CREDIT}"
    )
    await msg_obj.reply_document(
        document=InputFile(buf, filename=zip_name),
        caption=caption, parse_mode=ParseMode.HTML,
    )


def _hit_filename(outcome: ScanOutcome) -> str:
    payload = "|".join(f"{c.get('name','')}={c.get('value','')}" for c in outcome.cookies)
    short   = hashlib.sha1(payload.encode()).hexdigest()[:8]
    slug    = outcome.site.replace(".", "_")
    return f"@akaza_{slug}_{short}.txt"


# ── Catch-all ─────────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update, context): return
    if update.message is None: return
    site_id = _selected_site(context)
    if site_id:
        await update.message.reply_text(
            f"ℹ️ Waiting for your <b>{config.site_label(site_id)}</b> cookie file.",
            parse_mode=ParseMode.HTML, reply_markup=_back_keyboard())
    else:
        await update.message.reply_text(
            "ℹ️ Pick a site first — use /start.", reply_markup=_sites_keyboard())


# ═══════════════════════════════════════════════════════════════
#  Registration
# ═══════════════════════════════════════════════════════════════

def register(app: Application) -> None:
    from . import admin as _admin
    _admin.register(app)          # admin handlers registered first (priority)

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("check",    cmd_start))
    app.add_handler(CommandHandler("sites",    cmd_sites))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("about",    cmd_about))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("profile",  cmd_profile))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
