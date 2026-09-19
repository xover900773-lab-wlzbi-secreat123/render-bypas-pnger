import os
import json
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import ipaddress

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User as TelegramUser
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telegram.error import TelegramError


# ============================================================================
# Configuration
# ============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "7282835498")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "10"))
MAX_CONCURRENT_PINGS = int(os.getenv("MAX_CONCURRENT_PINGS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
DATA_FILE = os.getenv("DATA_FILE", "urls.json")
LOG_FILE = os.getenv("LOG_FILE", "activity.log")
MAX_LOG_ENTRIES = int(os.getenv("MAX_LOG_ENTRIES", "1000"))
SLOW_RESPONSE_THRESHOLD_MS = int(os.getenv("SLOW_RESPONSE_THRESHOLD_MS", "2000"))
CREDIT = "— @rejerks | WLZBI"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set")

ADMIN_IDS = set()
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = {int(uid.strip()) for uid in ADMIN_IDS_STR.split(",")}
    except ValueError:
        raise ValueError("ADMIN_IDS must be comma-separated numeric user IDs")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================

class PriorityLevel(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"


class URLStatus(str, Enum):
    HEALTHY = "healthy"
    SLOW = "slow"
    DOWN = "down"
    DISABLED = "disabled"


@dataclass
class URLRecord:
    id: str
    url: str
    owner_id: Optional[int] = None
    owner_username: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_checked: Optional[str] = None
    last_successful_ping: Optional[str] = None
    last_http_status: Optional[int] = None
    response_time_ms: Optional[int] = None
    consecutive_failures: int = 0
    enabled: bool = True
    priority: str = PriorityLevel.NORMAL.value
    is_admin: bool = False
    owner_type: str = "user"  # "user", "admin", or "legacy"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "URLRecord":
        return cls(**data)

    def get_status(self) -> URLStatus:
        if not self.enabled:
            return URLStatus.DISABLED
        if self.last_http_status is None:
            return URLStatus.DOWN
        if 200 <= self.last_http_status < 400:
            if self.response_time_ms and self.response_time_ms > SLOW_RESPONSE_THRESHOLD_MS:
                return URLStatus.SLOW
            return URLStatus.HEALTHY
        return URLStatus.DOWN

    def get_status_emoji(self) -> str:
        status = self.get_status()
        if status == URLStatus.HEALTHY:
            return "🟢"
        elif status == URLStatus.SLOW:
            return "🟡"
        elif status == URLStatus.DOWN:
            return "🔴"
        else:
            return "⏸"

    def get_priority_emoji(self) -> str:
        if not self.is_admin:
            return ""
        if self.priority == PriorityLevel.CRITICAL.value:
            return "🔥"
        elif self.priority == PriorityLevel.HIGH.value:
            return "⭐"
        else:
            return "👑"


@dataclass
class ActivityLogEntry:
    timestamp: str
    event_type: str  # "url_added", "url_deleted", "url_enabled", "url_disabled", "ping_manual", "priority_changed", etc.
    user_id: Optional[int] = None
    username: Optional[str] = None
    url: Optional[str] = None
    details: Optional[str] = None


# ============================================================================
# Data Persistence
# ============================================================================

class DataManager:
    def __init__(self, data_file: str, log_file: str):
        self.data_file = data_file
        self.log_file = log_file

    def load_urls(self) -> List[URLRecord]:
        try:
            with open(self.data_file, "r") as f:
                data = json.load(f)

            # Handle legacy format (plain strings)
            if data and isinstance(data[0], str):
                logger.info("Migrating legacy URL format to structured records")
                records = []
                for url in data:
                    record = URLRecord(
                        id=str(uuid.uuid4()),
                        url=url,
                        owner_id=None,
                        owner_type="legacy",
                    )
                    records.append(record)
                self.save_urls(records)
                return records

            return [URLRecord.from_dict(item) for item in data]
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save_urls(self, records: List[URLRecord]):
        with open(self.data_file, "w") as f:
            json.dump([record.to_dict() for record in records], f, indent=2)

    def add_log_entry(self, entry: ActivityLogEntry):
        try:
            entries = self.load_log()
        except (FileNotFoundError, json.JSONDecodeError):
            entries = []

        entries.append(asdict(entry))

        # Keep bounded log
        if len(entries) > MAX_LOG_ENTRIES:
            entries = entries[-MAX_LOG_ENTRIES:]

        with open(self.log_file, "w") as f:
            json.dump(entries, f, indent=2)

    def load_log(self) -> List[ActivityLogEntry]:
        try:
            with open(self.log_file, "r") as f:
                data = json.load(f)
            return [ActivityLogEntry(**item) for item in data]
        except (FileNotFoundError, json.JSONDecodeError):
            return []


# ============================================================================
# SSRF Protection
# ============================================================================

PRIVATE_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),  # localhost
    ipaddress.ip_network("0.0.0.0/8"),  # this network
    ipaddress.ip_network("10.0.0.0/8"),  # private
    ipaddress.ip_network("172.16.0.0/12"),  # private
    ipaddress.ip_network("192.168.0.0/16"),  # private
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),  # ipv6 localhost
    ipaddress.ip_network("fc00::/7"),  # ipv6 private
]


def is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in network for network in PRIVATE_IP_RANGES)
    except ValueError:
        return False


# ============================================================================
# URL Validation
# ============================================================================

def validate_and_normalize_url(url: str) -> Tuple[bool, str, Optional[str]]:
    url = url.strip()

    # Add protocol if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Validate protocol
    if not url.startswith(("http://", "https://")):
        return False, "", "Only HTTP and HTTPS protocols are supported."

    # Basic URL format check
    if len(url) < 10 or " " in url:
        return False, "", "Invalid URL format."

    # Try to extract hostname
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False, "", "Invalid URL format."
        
        # Check for SSRF
        if is_private_ip(hostname):
            return False, "", "Private/local IP addresses are not allowed."

    except Exception as e:
        return False, "", f"Invalid URL: {str(e)}"

    return True, url, None


# ============================================================================
# Ping Engine
# ============================================================================

class PingEngine:
    def __init__(self, max_concurrent: int, timeout: int):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.session: Optional[ClientSession] = None

    async def initialize(self):
        connector = TCPConnector(limit=self.max_concurrent, limit_per_host=5)
        self.session = ClientSession(connector=connector)

    async def close(self):
        if self.session:
            await self.session.close()

    async def ping_url(self, record: URLRecord) -> Tuple[str, Optional[int], Optional[int]]:
        if not record.enabled:
            return record.id, None, None

        async with self.semaphore:
            try:
                timeout = ClientTimeout(total=self.timeout)
                headers = {"User-Agent": "Mozilla/5.0 (compatible; URLPingerBot/1.0)"}
                
                started_at = time.perf_counter()
                async with self.session.get(record.url, timeout=timeout, headers=headers, allow_redirects=True) as resp:
                    status = resp.status
                    response_time = int((time.perf_counter() - started_at) * 1000)
                    logger.info(f"Pinged {record.url} -> {status} ({response_time}ms)")
                    return record.id, status, response_time
            except asyncio.TimeoutError:
                logger.warning(f"Timeout pinging {record.url}")
                return record.id, None, None
            except Exception as e:
                logger.error(f"Error pinging {record.url}: {e}")
                return record.id, None, None

    async def ping_all(self, records: List[URLRecord]) -> List[Tuple[str, Optional[int], Optional[int]]]:
        if not records:
            return []

        # Sort by priority: admin first, then by priority level
        sorted_records = sorted(
            records,
            key=lambda r: (
                not r.is_admin,  # Admin URLs first
                (PriorityLevel.CRITICAL.value != r.priority),
                (PriorityLevel.HIGH.value != r.priority),
            ),
        )

        tasks = [self.ping_url(r) for r in sorted_records]
        return await asyncio.gather(*tasks)


# ============================================================================
# Global State
# ============================================================================

dm = DataManager(DATA_FILE, LOG_FILE)
ping_engine: Optional[PingEngine] = None

CONVERSATION_STATES = {
    "WAITING_FOR_URL": 1,
    "WAITING_FOR_ADMIN_URL": 2,
    "WAITING_FOR_PRIORITY": 3,
    "WAITING_FOR_SEARCH": 4,
}


# ============================================================================
# Helper Functions
# ============================================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_url_display(url: str, max_len: int = 45) -> str:
    if len(url) <= max_len:
        return url
    return url[: max_len - 3] + "..."


def get_url_by_id(url_id: str) -> Optional[URLRecord]:
    records = dm.load_urls()
    for record in records:
        if record.id == url_id:
            return record
    return None


def user_owns_url(user_id: int, url_id: str) -> bool:
    record = get_url_by_id(url_id)
    if not record:
        return False
    return record.owner_id == user_id


async def log_activity(
    event_type: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    url: Optional[str] = None,
    details: Optional[str] = None,
):
    entry = ActivityLogEntry(
        timestamp=datetime.utcnow().isoformat(),
        event_type=event_type,
        user_id=user_id,
        username=username,
        url=url,
        details=details,
    )
    dm.add_log_entry(entry)


def format_timestamp(ts_str: Optional[str]) -> str:
    if not ts_str:
        return "Never"
    try:
        dt = datetime.fromisoformat(ts_str)
        now = datetime.utcnow()
        diff = now - dt
        if diff.total_seconds() < 60:
            return "Just now"
        elif diff.total_seconds() < 3600:
            return f"{int(diff.total_seconds() // 60)}m ago"
        elif diff.total_seconds() < 86400:
            return f"{int(diff.total_seconds() // 3600)}h ago"
        else:
            return f"{int(diff.total_seconds() // 86400)}d ago"
    except (TypeError, ValueError, OverflowError):
        return "Unknown"


# ============================================================================
# Keyboard Builders
# ============================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("➕ Add URL ", callback_data="add"),
            InlineKeyboardButton("🔗 My URLs", callback_data="my_urls"),
        ],
        [
            InlineKeyboardButton("📡 Ping URL ", callback_data="ping_url"),
            InlineKeyboardButton("📊 My Stats ", callback_data="my_stats"),
        ],
        [InlineKeyboardButton("ℹ️ Help ", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔗 All URLs ", callback_data="admin_all_urls")],
        [InlineKeyboardButton("⭐ Admin URLs ", callback_data="admin_admin_urls")],
        [InlineKeyboardButton("👥 User URLs ", callback_data="admin_user_urls")],
        [
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
            InlineKeyboardButton("❤️ Health", callback_data="admin_health"),
        ],
        [
            InlineKeyboardButton("📜 Activity Log ", callback_data="admin_log"),
            InlineKeyboardButton("👥 Users ", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings ", callback_data="admin_settings"),
            InlineKeyboardButton("📢 Broadcast ", callback_data="admin_broadcast"),
        ],
        [InlineKeyboardButton("🏠 User Menu ", callback_data="user_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_to_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Admin ", callback_data="admin_menu")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back ", callback_data="back")],
    ])


def pagination_keyboard(current_page: int, total_pages: int, prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    if current_page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Previous ", callback_data=f"{prefix}_page_{current_page - 1}"))
    if current_page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Next ➡️ ", callback_data=f"{prefix}_page_{current_page + 1}"))
    buttons.append(InlineKeyboardButton("🔙 Back ", callback_data=f"{prefix}_back"))
    return InlineKeyboardMarkup([buttons])


# ============================================================================
# User Commands
# ============================================================================

async def send_message_for_update(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """Reply to either a normal message update or a callback query message."""
    message = update.effective_message
    if message is None:
        logger.warning("Ignoring update without a replyable message: %s", update.update_id)
        return None
    return await message.reply_text(text, reply_markup=reply_markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        logger.warning("Ignoring /start without an effective user: %s", update.update_id)
        return

    text = (
        "🤖 render free tier bypasser\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Make your bots or websites hosted on render 24*7.\n"
        f"share me the link of your render service\n"
        f"never let your app take a nap\n"
        "━━━━━━━━━━━━━━━━━━━\n"
    )

    if is_admin(user.id):
        text += "Admin Mode Enabled\n"
        await send_message_for_update(update, text + CREDIT, reply_markup=admin_menu_keyboard())
    else:
        await send_message_for_update(update, text + CREDIT, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 URL PINGER HELP\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Add URLs you want to keep alive. The bot will automatically ping them every " + str(PING_INTERVAL) + " seconds.\n"
        "Your URLs:\n"
        "• Only you can see and manage your URLs\n"
        "• View status: Online 🟢, Offline 🔴, Slow 🟡\n"
        "• Manually ping any URL\n"
        "• Delete URLs you no longer need\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{CREDIT}"
    )
    await send_message_for_update(update, text, reply_markup=back_keyboard())


# ============================================================================
# User Callbacks
# ============================================================================

async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        logger.warning("Ignoring user callback without callback query: %s", update.update_id)
        return

    await query.answer()
    user = update.effective_user
    if user is None:
        logger.warning("Ignoring user callback without effective user: %s", update.update_id)
        return

    data = query.data

    if data == "add":
        await query.edit_message_text(
            "📝 Please send the URL you want to add.\n"
            "Include http:// or https:// – I'll add https:// if missing.\n"
            "Type /cancel to abort."
        )
        return CONVERSATION_STATES["WAITING_FOR_URL"]

    elif data == "my_urls":
        records = dm.load_urls()
        user_records = [r for r in records if r.owner_id == user.id]

        if not user_records:
            await query.edit_message_text(
                "📭 You have no URLs stored yet.\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "Add one using the ➕ Add URL button.",
                reply_markup=back_keyboard()
            )
            return

        text = "🔗 YOUR URLS\n"
        text += "━━━━━━━━━━━━━━━━━━━\n"
        for record in user_records:
            status_emoji = record.get_status_emoji()
            http_status = f"HTTP {record.last_http_status}" if record.last_http_status else "Unknown"
            text += f"{status_emoji} {format_url_display(record.url)}\n"
            text += f"   {http_status}\n"

        text += "━━━━━━━━━━━━━━━━━━━"
        await query.edit_message_text(text, reply_markup=back_keyboard())

    elif data == "my_stats":
        records = dm.load_urls()
        user_records = [r for r in records if r.owner_id == user.id]

        if not user_records:
            await query.edit_message_text(
                "📊 MY STATISTICS\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "No statistics available yet.",
                reply_markup=back_keyboard()
            )
            return

        total_urls = len(user_records)
        healthy = sum(1 for r in user_records if r.get_status() == URLStatus.HEALTHY)
        down = sum(1 for r in user_records if r.get_status() == URLStatus.DOWN)
        slow = sum(1 for r in user_records if r.get_status() == URLStatus.SLOW)

        text = (
            "📊 MY STATISTICS\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 Total URLs: {total_urls}\n"
            f"🟢 Healthy: {healthy}\n"
            f"🟡 Slow: {slow}\n"
            f"🔴 Down: {down}\n"
            "━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(text, reply_markup=back_keyboard())

    elif data == "help":
        await help_command(update, context)

    elif data == "back" or data == "user_menu":
        await query.edit_message_text(
            f"🤖 URL PINGER\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Welcome to URL Pinger.\n"
            f"{CREDIT}",
            reply_markup=main_menu_keyboard()
        )


async def add_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning("Ignoring URL input without message or user: %s", update.update_id)
        return ConversationHandler.END

    url_input = message.text.strip()

    valid, normalized_url, error = validate_and_normalize_url(url_input)
    if not valid:
        await update.message.reply_text(
            f"❌ {error}\n"
            "Type /start to return to the menu."
        )
        return ConversationHandler.END

    records = dm.load_urls()
    user_records = [r for r in records if r.owner_id == user.id]

    # Check for duplicate
    if any(r.url == normalized_url for r in user_records):
        await update.message.reply_text(
            f"⚠️ You already have this URL.\n"
            "Type /start to return to the menu."
        )
        return ConversationHandler.END

    # Create new record
    new_record = URLRecord(
        id=str(uuid.uuid4()),
        url=normalized_url,
        owner_id=user.id,
        owner_username=user.username,
        is_admin=False,
        owner_type="user",
    )
    records.append(new_record)
    dm.save_urls(records)

    await log_activity("url_added", user.id, user.username, normalized_url)

    await update.message.reply_text(
        f"✅ Added: {format_url_display(normalized_url)}\n"
        "Type /start to return to the menu."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None:
        logger.warning("Ignoring cancel without message: %s", update.update_id)
        return ConversationHandler.END

    await message.reply_text(
        "❌ Operation cancelled.\n"
        "Type /start to return to the menu."
    )
    return ConversationHandler.END


# ============================================================================
# Admin Callbacks
# ============================================================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        logger.warning("Ignoring admin callback without callback query: %s", update.update_id)
        return

    await query.answer()
    user = update.effective_user
    if user is None:
        logger.warning("Ignoring admin callback without effective user: %s", update.update_id)
        return

    if not is_admin(user.id):
        await query.edit_message_text("❌ Access denied.")
        return

    data = query.data

    if data == "admin_menu":
        await query.edit_message_text(
            "🤖 ADMIN PANEL\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Select an option below:\n"
            "━━━━━━━━━━━━━━━━━━━",
            reply_markup=admin_menu_keyboard()
        )

    elif data == "admin_all_urls":
        records = dm.load_urls()
        if not records:
            await query.edit_message_text(
                "📭 No URLs stored.\n"
                "━━━━━━━━━━━━━━━━━━━",
                reply_markup=back_to_admin_keyboard()
            )
            return

        text = f"🔗 ALL URLS ({len(records)} total)\n"
        text += "━━━━━━━━━━━━━━━━━━━\n"
        for i, record in enumerate(records[:10], 1):
            status_emoji = record.get_status_emoji()
            priority_emoji = record.get_priority_emoji()
            owner_type = "👑 Admin" if record.is_admin else "👤 User"
            text += f"{priority_emoji}{status_emoji} {i}. {format_url_display(record.url)}\n"
            text += f"   {owner_type} • ID: {record.owner_id}\n"

        text += "━━━━━━━━━━━━━━━━━━━"
        await query.edit_message_text(text, reply_markup=back_to_admin_keyboard())

    elif data == "admin_stats":
        records = dm.load_urls()
        total_urls = len(records)
        admin_urls = sum(1 for r in records if r.is_admin)
        user_urls = total_urls - admin_urls
        active_urls = sum(1 for r in records if r.enabled)
        disabled_urls = total_urls - active_urls

        healthy = sum(1 for r in records if r.get_status() == URLStatus.HEALTHY)
        unhealthy = sum(1 for r in records if r.get_status() in [URLStatus.DOWN, URLStatus.SLOW])

        unique_users = len(set(r.owner_id for r in records if r.owner_id))

        text = (
            "📊 STATISTICS\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 Total URLs: {total_urls}\n"
            f"⭐ Admin URLs: {admin_urls}\n"
            f"👥 User URLs: {user_urls}\n"
            f"▶️ Active URLs: {active_urls}\n"
            f"⏸ Disabled URLs: {disabled_urls}\n"
            f"🟢 Healthy: {healthy}\n"
            f"🔴 Unhealthy: {unhealthy}\n"
            f"👥 Total Users: {unique_users}\n"
            "━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(text, reply_markup=back_to_admin_keyboard())

    elif data == "admin_health":
        records = dm.load_urls()
        healthy = sum(1 for r in records if r.get_status() == URLStatus.HEALTHY)
        slow = sum(1 for r in records if r.get_status() == URLStatus.SLOW)
        down = sum(1 for r in records if r.get_status() == URLStatus.DOWN)
        disabled = sum(1 for r in records if r.get_status() == URLStatus.DISABLED)

        text = (
            "❤️ HEALTH MONITOR\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Healthy: {healthy}\n"
            f"🟡 Slow: {slow}\n"
            f"🔴 Down: {down}\n"
            f"⏸ Disabled: {disabled}\n"
            "━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(text, reply_markup=back_to_admin_keyboard())

    elif data == "admin_log":
        log_entries = dm.load_log()
        if not log_entries:
            await query.edit_message_text(
                "📜 ACTIVITY LOG\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "No activity recorded yet.\n"
                "━━━━━━━━━━━━━━━━━━━",
                reply_markup=back_to_admin_keyboard()
            )
            return

        text = "📜 ACTIVITY LOG\n"
        text += "━━━━━━━━━━━━━━━━━━━\n"
        for entry in log_entries[-10:]:
            timestamp = entry.timestamp[:16]  # YYYY-MM-DDTHH:MM
            user_info = f"@{entry.username}" if entry.username else f"ID {entry.user_id}"
            text += f"{timestamp} — {entry.event_type}\n"
            if entry.url:
                text += f"   {format_url_display(entry.url)}\n"
            if entry.user_id:
                text += f"   By: {user_info}\n"
            text += "\n"

        text += "━━━━━━━━━━━━━━━━━━━"
        await query.edit_message_text(text, reply_markup=back_to_admin_keyboard())


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None:
        logger.warning("Ignoring update without a replyable message: %s", update.update_id)
        return

    await message.reply_text(
        "❓ I don't understand that.\n"
        "Please use /start or the buttons."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error is None:
        logger.error("Unhandled Telegram update error without exception details")
    else:
        logger.error(
            "Unhandled Telegram update error",
            exc_info=(type(error), error, error.__traceback__),
        )

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Something went wrong while processing that request.\n"
                "Please try again."
            )
        except TelegramError:
            logger.exception("Could not send the error response")


# ============================================================================
# Scheduler
# ============================================================================

async def ping_scheduler(app: Application):
    global ping_engine
    ping_engine = PingEngine(MAX_CONCURRENT_PINGS, REQUEST_TIMEOUT)
    await ping_engine.initialize()

    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            records = dm.load_urls()
            if records:
                results = await ping_engine.ping_all(records)

                # Update records with ping results
                for url_id, status, response_time in results:
                    for record in records:
                        if record.id == url_id:
                            record.last_checked = datetime.utcnow().isoformat()
                            if status:
                                record.last_http_status = status
                                record.response_time_ms = response_time
                                record.consecutive_failures = 0
                                record.last_successful_ping = datetime.utcnow().isoformat()
                            else:
                                record.consecutive_failures += 1
                            break

                dm.save_urls(records)
    except Exception as e:
        logger.error(f"Error in ping scheduler: {e}")
    finally:
        if ping_engine:
            await ping_engine.close()


# ============================================================================
# Main Application
# ============================================================================

async def start_scheduler(app: Application):
    app.create_task(ping_scheduler(app), name="url-ping-scheduler")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(start_scheduler).build()

    # Conversation handlers
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(user_callback, pattern="^add$"),
        ],
        states={
            CONVERSATION_STATES["WAITING_FOR_URL"]: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_url_message),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # User callbacks
    app.add_handler(
        CallbackQueryHandler(
            user_callback,
            pattern="^(my_urls|ping_url|my_stats|help|back|user_menu)$",
        )
    )

    # Admin callbacks
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern="^admin_",
        )
    )

    # Fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))
    app.add_error_handler(error_handler)

    logger.info("Starting URL Pinger Bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
