"""Admin panel — commands and callbacks for bot admins.

All handlers are only executable by ADMIN_ID (set in config).
Commands:
  /admin          — main admin panel
  /broadcast      — send a message to all users
  /give <id> premium|free|admin  — set user role
  /ban <id> [reason]             — ban a user
  /unban <id>                    — unban a user
  /userinfo <id>                 — full user profile
  /users                         — list all users (paginated)

Callback data prefixes used here:
  adm:           — main panel navigation
  adm_user:      — per-user actions
  adm_page:      — pagination
"""

from __future__ import annotations

import html
import asyncio
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import config, storage
from .constants import BOT_CREDIT



# ── Conversation states ───────────────────────────────────────
BROADCAST_TEXT = 1
BROADCAST_CONFIRM = 2

# ── Helpers ───────────────────────────────────────────────────

def _esc(v: Any) -> str:
    return html.escape(str(v), quote=False)

def _role_badge(role: str) -> str:
    return {"admin": "👑 Admin", "premium": "💎 Premium", "free": "🆓 Free"}.get(role, role)

def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID

def _admin_only(func):
    """Decorator — silently ignore non-admins."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not _is_admin(user.id):
            return
        return await func(update, context)
    return wrapper

def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)



# ── Keyboards ─────────────────────────────────────────────────

def _admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Users List",      callback_data="adm:users:0"),
            InlineKeyboardButton("📊 Stats",            callback_data="adm:stats"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast",        callback_data="adm:broadcast"),
            InlineKeyboardButton("🔍 Find User",        callback_data="adm:find"),
        ],
        [InlineKeyboardButton("❌ Close",               callback_data="adm:close")],
    ])

def _user_action_keyboard(uid: int, role: str, banned: bool) -> InlineKeyboardMarkup:
    rows = []
    if role != config.ROLE_PREMIUM:
        rows.append([InlineKeyboardButton("💎 Give Premium", callback_data=f"adm_user:premium:{uid}")])
    if role != config.ROLE_FREE:
        rows.append([InlineKeyboardButton("🆓 Set Free",     callback_data=f"adm_user:free:{uid}")])
    if role != config.ROLE_ADMIN:
        rows.append([InlineKeyboardButton("👑 Make Admin",   callback_data=f"adm_user:admin:{uid}")])
    if banned:
        rows.append([InlineKeyboardButton("✅ Unban",        callback_data=f"adm_user:unban:{uid}")])
    else:
        rows.append([InlineKeyboardButton("🚫 Ban",          callback_data=f"adm_user:ban:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Back to list",    callback_data="adm:users:0")])
    return InlineKeyboardMarkup(rows)

def _back_to_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:main")]
    ])



# ── Formatters ────────────────────────────────────────────────

def _fmt_user_card(u: dict) -> str:
    uid   = u.get("user_id", "?")
    name  = _esc(u.get("first_name") or u.get("username") or str(uid))
    uname = f"@{_esc(u['username'])}" if u.get("username") else "—"
    role  = _role_badge(u.get("role", "free"))
    banned= "🚫 BANNED" if u.get("banned") else "✅ Active"
    lines = [
        f"👤 <b>{name}</b>  <code>{uid}</code>",
        f"  Username  : {uname}",
        f"  Role      : {role}",
        f"  Status    : {banned}",
        f"  Joined    : {_fmt_ts(u.get('joined_at'))}",
        f"  Premium ✦ : {_fmt_ts(u.get('premium_since'))}",
        f"  Checked   : <b>{u.get('total_checked', 0)}</b>  |  Hits: <b>{u.get('total_hits', 0)}</b>",
    ]
    if u.get("ban_reason"):
        lines.append(f"  Ban reason: <i>{_esc(u['ban_reason'])}</i>")
    svcs: dict = u.get("services_used") or {}
    if svcs:
        top = sorted(svcs.items(), key=lambda x: -x[1])[:5]
        lines.append("  Top sites : " + "  ".join(f"{s}×{c}" for s, c in top))
    recent: list = u.get("recent_checks") or []
    if recent:
        lines.append("  Last checks:")
        for r in reversed(recent[-5:]):
            icon = "✅" if r.get("alive") else "❌"
            lines.append(f"    {icon} {_esc(r.get('site','?'))}  <i>{_esc(r.get('filename',''))}</i>  {r.get('ts','')[:10]}")
    return "\n".join(lines)


async def _fmt_stats_card() -> str:
    stats = await storage.get_dashboard_stats()
    users = await storage.get_all_users()
    total_u   = len(users)
    premium_u = sum(1 for u in users if u.get("role") == config.ROLE_PREMIUM)
    banned_u  = sum(1 for u in users if u.get("banned"))
    admin_u   = sum(1 for u in users if u.get("role") == config.ROLE_ADMIN)
    lines = [
        "📊 <b>Bot Statistics</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  👥 Total users    : <b>{total_u}</b>",
        f"  💎 Premium        : <b>{premium_u}</b>",
        f"  👑 Admins         : <b>{admin_u}</b>",
        f"  🚫 Banned         : <b>{banned_u}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  🧮 Total scanned  : <b>{stats.get('total', 0)}</b>",
        f"  ✅ Total alive    : <b>{stats.get('alive', 0)}</b>",
        f"  ❌ Total dead     : <b>{stats.get('dead',  0)}</b>",
        f"  🕒 Last updated   : <code>{_esc(stats.get('last_updated') or 'never')}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)



# ── /admin command ────────────────────────────────────────────

@_admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = await storage.get_all_users()
    premium_c = sum(1 for u in users if u.get("role") == config.ROLE_PREMIUM)
    text = (
        "👑 <b>Admin Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  👥 Users   : <b>{len(users)}</b>\n"
        f"  💎 Premium : <b>{premium_c}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Use the buttons below or these commands:\n"
        "  <code>/give &lt;user_id&gt; premium|free|admin</code>\n"
        "  <code>/ban &lt;user_id&gt; [reason]</code>\n"
        "  <code>/unban &lt;user_id&gt;</code>\n"
        "  <code>/userinfo &lt;user_id&gt;</code>\n"
        "  <code>/broadcast</code>  — send a message to all users\n"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=_admin_main_keyboard())
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=_admin_main_keyboard())
        except BadRequest:
            pass


# ── /give command ─────────────────────────────────────────────

@_admin_only
async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None: return
    args = (context.args or [])
    if len(args) < 2:
        await msg.reply_text("Usage: <code>/give &lt;user_id&gt; premium|free|admin</code>",
                             parse_mode=ParseMode.HTML)
        return
    try:
        uid  = int(args[0])
        role = args[1].lower()
    except ValueError:
        await msg.reply_text("❌ Invalid user_id."); return
    if role not in (config.ROLE_FREE, config.ROLE_PREMIUM, config.ROLE_ADMIN):
        await msg.reply_text("❌ Role must be <code>premium</code>, <code>free</code>, or <code>admin</code>.",
                             parse_mode=ParseMode.HTML); return
    u = await storage.set_user_role(uid, role)
    badge = _role_badge(role)
    await msg.reply_text(f"✅ User <code>{uid}</code> is now <b>{badge}</b>.",
                         parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(
            uid,
            f"🎉 Your role has been updated to <b>{badge}</b> by the admin!",
            parse_mode=ParseMode.HTML,
        )
    except (Forbidden, BadRequest):
        pass



# ── /ban & /unban ─────────────────────────────────────────────

@_admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None: return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: <code>/ban &lt;user_id&gt; [reason]</code>",
                             parse_mode=ParseMode.HTML); return
    try:
        uid = int(args[0])
    except ValueError:
        await msg.reply_text("❌ Invalid user_id."); return
    if uid == config.ADMIN_ID:
        await msg.reply_text("❌ Cannot ban the owner."); return
    reason = " ".join(args[1:]) if len(args) > 1 else ""
    await storage.ban_user(uid, reason)
    await msg.reply_text(
        f"🚫 User <code>{uid}</code> has been <b>banned</b>."
        + (f"\nReason: <i>{_esc(reason)}</i>" if reason else ""),
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            uid, "🚫 You have been <b>banned</b> from this bot." +
            (f"\nReason: <i>{_esc(reason)}</i>" if reason else ""),
            parse_mode=ParseMode.HTML,
        )
    except (Forbidden, BadRequest):
        pass


@_admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None: return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>",
                             parse_mode=ParseMode.HTML); return
    try:
        uid = int(args[0])
    except ValueError:
        await msg.reply_text("❌ Invalid user_id."); return
    await storage.unban_user(uid)
    await msg.reply_text(f"✅ User <code>{uid}</code> has been <b>unbanned</b>.",
                         parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(uid, "✅ You have been <b>unbanned</b>. Welcome back!",
                                       parse_mode=ParseMode.HTML)
    except (Forbidden, BadRequest):
        pass


# ── /userinfo command ─────────────────────────────────────────

@_admin_only
async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None: return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: <code>/userinfo &lt;user_id&gt;</code>",
                             parse_mode=ParseMode.HTML); return
    try:
        uid = int(args[0])
    except ValueError:
        await msg.reply_text("❌ Invalid user_id."); return
    u = await storage.get_user(uid)
    if not u:
        await msg.reply_text(f"⚠️ No data for user <code>{uid}</code> yet.",
                             parse_mode=ParseMode.HTML); return
    await msg.reply_text(
        _fmt_user_card(u),
        parse_mode=ParseMode.HTML,
        reply_markup=_user_action_keyboard(uid, u.get("role","free"), bool(u.get("banned"))),
    )



# ── /users command (paginated list) ──────────────────────────

_PAGE_SIZE = 8

@_admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_users_page(update, context, 0)


async def _show_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    users   = await storage.get_all_users()
    users   = sorted(users, key=lambda u: u.get("joined_at") or "", reverse=True)
    total   = len(users)
    start   = page * _PAGE_SIZE
    chunk   = users[start : start + _PAGE_SIZE]

    lines = [f"👥 <b>Users</b> ({total} total) — page {page+1}/{max(1,(total-1)//_PAGE_SIZE+1)}",
             "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for u in chunk:
        uid  = u.get("user_id","?")
        name = _esc(u.get("first_name") or u.get("username") or str(uid))
        role = _role_badge(u.get("role","free"))
        ban  = " 🚫" if u.get("banned") else ""
        lines.append(f"  <code>{uid}</code>  {name}  {role}{ban}")
    text = "\n".join(lines)

    # per-user buttons
    rows = []
    for u in chunk:
        uid  = u.get("user_id","?")
        name = (u.get("first_name") or u.get("username") or str(uid))[:18]
        rows.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"adm_user:view:{uid}")])
    # pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm:users:{page-1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:users:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:main")])
    kb = InlineKeyboardMarkup(rows)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)



# ── Broadcast conversation ────────────────────────────────────

@_admin_only
async def cmd_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message or (update.callback_query and update.callback_query.message)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "📢 <b>Broadcast</b>\n\nSend me the message to broadcast to all users.\n"
            "Supports HTML. Type /cancel to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm:main")
            ]]),
        )
    else:
        await update.message.reply_text(
            "📢 <b>Broadcast</b>\n\nSend me the message to broadcast.\n"
            "Supports HTML. Send /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
    return BROADCAST_TEXT


async def broadcast_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    if msg is None: return BROADCAST_TEXT
    if context.user_data is not None:
        context.user_data["broadcast_text"] = msg.text_html or msg.text or ""
    preview = (msg.text_html or msg.text or "")[:300]
    await msg.reply_text(
        f"📋 <b>Preview:</b>\n\n{preview}\n\n"
        "Send <b>CONFIRM</b> to broadcast, or /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )
    return BROADCAST_CONFIRM


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    if msg is None: return BROADCAST_CONFIRM
    if (msg.text or "").strip().upper() != "CONFIRM":
        await msg.reply_text("❌ Not confirmed. Send CONFIRM or /cancel.")
        return BROADCAST_CONFIRM
    btext = (context.user_data or {}).get("broadcast_text", "")
    if not btext:
        await msg.reply_text("❌ No message stored. Start over with /broadcast.")
        return ConversationHandler.END
    users   = await storage.get_all_users()
    sent    = 0; failed = 0
    status  = await msg.reply_text(f"📤 Broadcasting to {len(users)} users…")
    for u in users:
        uid = u.get("user_id")
        if not uid: continue
        try:
            await context.bot.send_message(uid, btext, parse_mode=ParseMode.HTML)
            sent += 1
        except (Forbidden, BadRequest):
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)   # ~20 msg/s — stays inside Telegram limits
    try:
        await status.edit_text(
            f"✅ Broadcast complete!\n  Sent: <b>{sent}</b>\n  Failed: <b>{failed}</b>",
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("❌ Broadcast cancelled.")
    return ConversationHandler.END



# ── Central callback router for adm: and adm_user: ───────────

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None: return
    user = update.effective_user
    if user is None or not _is_admin(user.id):
        await q.answer("⛔ Admins only.", show_alert=True); return
    await q.answer()
    data = q.data

    # ── adm:main ──────────────────────────────────────────────
    if data == "adm:main":
        await cmd_admin(update, context); return

    # ── adm:close ─────────────────────────────────────────────
    if data == "adm:close":
        try: await q.message.delete()
        except BadRequest: pass
        return

    # ── adm:stats ─────────────────────────────────────────────
    if data == "adm:stats":
        text = await _fmt_stats_card()
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=_back_to_panel())
        except BadRequest: pass
        return

    # ── adm:users:<page> ──────────────────────────────────────
    if data.startswith("adm:users:"):
        page = int(data.split(":")[-1])
        await _show_users_page(update, context, page); return

    # ── adm:broadcast ─────────────────────────────────────────
    if data == "adm:broadcast":
        await cmd_broadcast_start(update, context); return

    # ── adm:find ──────────────────────────────────────────────
    if data == "adm:find":
        try:
            await q.edit_message_text(
                "🔍 Send me the user_id or @username to look up.\n"
                "Reply with the number, e.g. <code>123456789</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_panel(),
            )
        except BadRequest: pass
        if context.user_data is not None:
            context.user_data["admin_finding"] = True
        return

    # ── adm_user:view:<uid> ───────────────────────────────────
    if data.startswith("adm_user:view:"):
        uid = int(data.split(":")[-1])
        u   = await storage.get_user(uid)
        if not u:
            try: await q.edit_message_text(f"⚠️ No data for <code>{uid}</code>.",
                                           parse_mode=ParseMode.HTML,
                                           reply_markup=_back_to_panel())
            except BadRequest: pass
            return
        try:
            await q.edit_message_text(
                _fmt_user_card(u), parse_mode=ParseMode.HTML,
                reply_markup=_user_action_keyboard(uid, u.get("role","free"), bool(u.get("banned"))),
            )
        except BadRequest: pass
        return

    # ── adm_user:premium|free|admin:<uid> ────────────────────
    if data.startswith("adm_user:") and not data.startswith("adm_user:view"):
        parts = data.split(":")
        action, uid = parts[1], int(parts[2])
        if action in (config.ROLE_FREE, config.ROLE_PREMIUM, config.ROLE_ADMIN):
            u = await storage.set_user_role(uid, action)
            badge = _role_badge(action)
            try:
                await q.edit_message_text(
                    f"✅ <code>{uid}</code> is now <b>{badge}</b>.\n\n" + _fmt_user_card(u),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_user_action_keyboard(uid, action, bool(u.get("banned"))),
                )
            except BadRequest: pass
            try:
                await context.bot.send_message(
                    uid, f"🎉 Your role is now <b>{badge}</b>!", parse_mode=ParseMode.HTML)
            except (Forbidden, BadRequest): pass
        elif action == "ban":
            u = await storage.ban_user(uid)
            try:
                await q.edit_message_text(
                    f"🚫 <code>{uid}</code> has been banned.\n\n" + _fmt_user_card(u),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_user_action_keyboard(uid, u.get("role","free"), True),
                )
            except BadRequest: pass
            try: await context.bot.send_message(uid, "🚫 You have been <b>banned</b>.", parse_mode=ParseMode.HTML)
            except (Forbidden, BadRequest): pass
        elif action == "unban":
            u = await storage.unban_user(uid)
            try:
                await q.edit_message_text(
                    f"✅ <code>{uid}</code> has been unbanned.\n\n" + _fmt_user_card(u),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_user_action_keyboard(uid, u.get("role","free"), False),
                )
            except BadRequest: pass
            try: await context.bot.send_message(uid, "✅ You have been <b>unbanned</b>. Welcome back!", parse_mode=ParseMode.HTML)
            except (Forbidden, BadRequest): pass



# ── Registration ──────────────────────────────────────────────

def register(app: Application) -> None:
    # Broadcast conversation
    broadcast_conv = ConversationHandler(
        entry_points=[
            CommandHandler("broadcast", cmd_broadcast_start),
        ],
        states={
            BROADCAST_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_receive_text)],
            BROADCAST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_confirm)],
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CommandHandler("give",     cmd_give))
    app.add_handler(CommandHandler("ban",      cmd_ban))
    app.add_handler(CommandHandler("unban",    cmd_unban))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("users",    cmd_users))
    app.add_handler(broadcast_conv)
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^adm[_:]"))
