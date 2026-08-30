from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from typing import Any
from urllib import error as urllib_error
from urllib.parse import quote
from urllib import request as urllib_request


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("telegram-group-bot")

POLL_TIMEOUT_SECONDS = 25
RETRY_DELAY_SECONDS = 5
ADMIN_CACHE_SECONDS = 60
FLOOD_WINDOW_SECONDS = 12
BOT_API_URL = os.getenv("BOT_API_URL", "http://127.0.0.1:8080/api").rstrip("/")

SCAM_PATTERN = re.compile(
    r"\b(?:free\s+(?:crypto|bitcoin|money)|guaranteed\s+profit|"
    r"double\s+your\s+(?:bitcoin|money)|claim\s+your\s+airdrop|"
    r"investment\s+signal|verify\s+your\s+account|earn\s+\$?\d+)\b",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"(?:https?://|www\.|t\.me/)", re.IGNORECASE)
INVITE_PATTERN = re.compile(r"(?:t\.me/(?:\+|joinchat)|telegram\.me/joinchat)", re.IGNORECASE)
SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"
SIGHTENGINE_USER = "1290792300"
MAX_IMAGE_BYTES = 20 * 1024 * 1024

ARABIC_MENU = {
    "inline_keyboard": [
        [
            {"text": "قواعد الحماية", "callback_data": "menu_rules"},
            {"text": "حالة البوت", "callback_data": "menu_status"},
        ],
        [
            {"text": "المساعدة", "callback_data": "menu_help"},
            {"text": "طريقة الحظر", "callback_data": "menu_ban"},
        ],
    ]
}


class TelegramError(RuntimeError):
    pass


class BotServiceError(RuntimeError):
    pass


class ImageModerationError(RuntimeError):
    pass


class TelegramBridge:
    """Runs the Python bot logic while keeping authenticated API calls in Node."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            ["node", "group-bot/telegram_bridge.mjs"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def call(self, method: str, body: dict[str, Any] | None = None) -> Any:
        if self.process.poll() is not None:
            raise TelegramError("Telegram connector bridge stopped")
        if self.process.stdin is None or self.process.stdout is None:
            raise TelegramError("Telegram connector bridge is unavailable")

        self.process.stdin.write(
            json.dumps({"method": method, "body": body}, separators=(",", ":"))
            + "\n"
        )
        self.process.stdin.flush()

        response_line = self.process.stdout.readline()
        if not response_line:
            raise TelegramError("Telegram connector bridge returned no response")

        response = json.loads(response_line)
        payload = response.get("payload", {})
        if response.get("status", 500) >= 400 or not payload.get("ok"):
            raise TelegramError(
                f"Telegram {method} failed: "
                f"{payload.get('description', 'unknown error')}"
            )
        return payload.get("result")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


class BotServiceClient:
    """Connects the Python runtime to the database-backed API service."""

    def __init__(self) -> None:
        self.base_url = BOT_API_URL
        self.secret = os.getenv("SESSION_SECRET")

    def call(
        self,
        path: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.secret:
            raise BotServiceError("SESSION_SECRET is not configured")

        payload = None
        headers = {
            "Accept": "application/json",
            "X-Bot-Secret": self.secret,
        }
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        api_request = urllib_request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib_request.urlopen(api_request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib_error.HTTPError, urllib_error.URLError, json.JSONDecodeError) as error:
            raise BotServiceError(f"Bot API request failed: {error}") from error

        if isinstance(result, dict) and result.get("error"):
            raise BotServiceError(str(result["error"]))
        return result

    def update_settings(self, chat_id: int, **settings: Any) -> dict[str, Any]:
        result = self.call(
            "/bot/settings",
            method="POST",
            body={"chatId": chat_id, **settings},
        )
        return result if isinstance(result, dict) else {}

    def get_settings(self, chat_id: int) -> dict[str, Any]:
        result = self.call(f"/bot/settings?chat_id={chat_id}")
        return result if isinstance(result, dict) else {}

    def record_event(self, chat_id: int, event_type: str, **details: Any) -> None:
        self.call(
            "/bot/events",
            method="POST",
            body={
                "chatId": chat_id,
                "eventType": event_type,
                "messageId": details.pop("message_id", None),
                "userId": details.pop("user_id", None),
                "username": details.pop("username", None),
                "details": details,
            },
        )

    def create_dashboard_token(self, chat_id: int) -> str:
        result = self.call(
            "/bot/dashboard-token",
            method="POST",
            body={"chatId": chat_id},
        )
        url = result.get("dashboardUrl") if isinstance(result, dict) else None
        if not isinstance(url, str):
            raise BotServiceError("Dashboard URL was not returned")
        return url

    def weather(self, location: str, language: str = "ar") -> dict[str, Any]:
        result = self.call(
            f"/bot/weather?location={quote(location)}&language={language}",
        )
        return result if isinstance(result, dict) else {}

    def translate(self, text: str, target: str = "ar") -> str:
        result = self.call(
            "/bot/translate",
            method="POST",
            body={"text": text, "target": target},
        )
        translated = result.get("translatedText") if isinstance(result, dict) else None
        if not isinstance(translated, str):
            raise BotServiceError("Translation was not returned")
        return translated


def command_name(text: str) -> str:
    return text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()


def command_args(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def is_group(message: dict[str, Any]) -> bool:
    return message.get("chat", {}).get("type") in {"group", "supergroup"}


def is_admin(member: dict[str, Any]) -> bool:
    return member.get("status") in {"administrator", "creator"}


def can_delete_messages(member: dict[str, Any]) -> bool:
    return member.get("status") == "creator" or (
        member.get("status") == "administrator"
        and member.get("can_delete_messages") is True
    )


def looks_like_spam(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    if SCAM_PATTERN.search(compact) and URL_PATTERN.search(compact):
        return True
    if INVITE_PATTERN.search(compact) and (
        SCAM_PATTERN.search(compact) or len(compact) > 160
    ):
        return True

    letters = [character for character in compact if character.isalpha()]
    uppercase_ratio = (
        sum(character.isupper() for character in letters) / len(letters)
        if letters
        else 0
    )
    return len(letters) >= 30 and uppercase_ratio >= 0.9 and compact.count("!") >= 2


class GroupModerator:
    def __init__(
        self,
        api: TelegramBridge,
        bot_id: int,
        service: BotServiceClient | None = None,
    ) -> None:
        self.api = api
        self.bot_id = bot_id
        self.service = service
        self.admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}
        self.bot_permission_cache: dict[
            int, tuple[float, bool, bool]
        ] = {}
        self.recent_messages: defaultdict[
            tuple[int, int], deque[tuple[float, str]]
        ] = defaultdict(deque)

    def member_is_admin(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        cached = self.admin_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        try:
            member = self.api.call(
                "getChatMember",
                {"chat_id": chat_id, "user_id": user_id},
            )
            result = is_admin(member or {})
        except TelegramError as error:
            logger.warning("Could not inspect group member: %s", error)
            result = False

        self.admin_cache[key] = (time.monotonic() + ADMIN_CACHE_SECONDS, result)
        return result

    def bot_can_delete(self, chat_id: int) -> bool:
        cached = self.bot_permission_cache.get(chat_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        try:
            member = self.api.call(
                "getChatMember",
                {"chat_id": chat_id, "user_id": self.bot_id},
            )
            result = can_delete_messages(member or {})
            can_ban = can_restrict_members(member or {})
        except TelegramError as error:
            logger.warning("Could not inspect bot permissions: %s", error)
            result = False
            can_ban = False

        self.bot_permission_cache[chat_id] = (
            time.monotonic() + ADMIN_CACHE_SECONDS,
            result,
            can_ban,
        )
        return result

    def bot_can_ban(self, chat_id: int) -> bool:
        cached = self.bot_permission_cache.get(chat_id)
        if cached and cached[0] > time.monotonic():
            return cached[2]
        self.bot_can_delete(chat_id)
        return self.bot_permission_cache.get(chat_id, (0, False, False))[2]

    def is_flood(self, chat_id: int, user_id: int, text: str) -> bool:
        key = (chat_id, user_id)
        now = time.monotonic()
        messages = self.recent_messages[key]
        while messages and messages[0][0] < now - FLOOD_WINDOW_SECONDS:
            messages.popleft()

        normalized = re.sub(r"\s+", " ", text).strip().lower()
        same_text_count = sum(
            1 for _, previous_text in messages if previous_text == normalized
        )
        messages.append((now, normalized))
        return same_text_count >= 1 or len(messages) >= 5

    def removal_reason(self, message: dict[str, Any]) -> str | None:
        text = (message.get("text") or "").strip()
        sender = message.get("from") or {}
        if not text or sender.get("is_bot"):
            return None

        chat_id = message["chat"]["id"]
        user_id = sender.get("id")
        if user_id is None or self.member_is_admin(chat_id, user_id):
            return None

        if URL_PATTERN.search(text):
            return "link"
        if looks_like_spam(text) or self.is_flood(chat_id, user_id, text):
            return "spam"
        return None

    def moderate(self, message: dict[str, Any]) -> str | None:
        chat_id = message["chat"]["id"]
        if not self.bot_can_delete(chat_id):
            return None
        reason = self.removal_reason(message)
        if reason is None:
            return None

        try:
            self.api.call(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message["message_id"]},
            )
            logger.info(
                "Removed a likely spam message in chat %s (message %s)",
                chat_id,
                message["message_id"],
            )
            sender = message.get("from") or {}
            self.record_event(
                chat_id,
                reason,
                message_id=message["message_id"],
                user_id=sender.get("id"),
                username=sender.get("username"),
            )
            return reason
        except TelegramError as error:
            logger.warning("Could not remove message: %s", error)
            return None

    def record_event(self, chat_id: int, event_type: str, **details: Any) -> None:
        if not self.service:
            return
        try:
            self.service.record_event(chat_id, event_type, **details)
        except BotServiceError as error:
            logger.warning("Could not record %s event: %s", event_type, error)


def can_restrict_members(member: dict[str, Any]) -> bool:
    return member.get("status") == "creator" or (
        member.get("status") == "administrator"
        and member.get("can_restrict_members") is True
    )


def download_telegram_file(file_path: str) -> bytes:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ImageModerationError("TELEGRAM_BOT_TOKEN is not configured")

    file_url = f"https://api.telegram.org/file/bot{token}/{file_path.lstrip('/')}"
    try:
        with urllib_request.urlopen(file_url, timeout=30) as response:
            chunks: list[bytes] = []
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ImageModerationError("Image is larger than 20 MB")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib_error.URLError as error:
        raise ImageModerationError(f"Could not download Telegram image: {error}") from error


def build_multipart(fields: dict[str, str], file_bytes: bytes) -> tuple[bytes, str]:
    boundary = f"----ReplitSightengine{int(time.time() * 1000)}"
    boundary_bytes = boundary.encode()
    body = bytearray()

    for name, value in fields.items():
        body.extend(b"--" + boundary_bytes + b"\r\n")
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value.encode())
        body.extend(b"\r\n")

    body.extend(b"--" + boundary_bytes + b"\r\n")
    body.extend(
        b'Content-Disposition: form-data; name="media"; filename="telegram-photo.jpg"\r\n'
    )
    body.extend(b"Content-Type: image/jpeg\r\n\r\n")
    body.extend(file_bytes)
    body.extend(b"\r\n--" + boundary_bytes + b"--\r\n")
    return bytes(body), boundary


def sightengine_detects_nudity(file_bytes: bytes) -> bool:
    secret = os.getenv("SIGHTENGINE_SECRET")
    if not secret:
        raise ImageModerationError("SIGHTENGINE_SECRET is not configured")

    body, boundary = build_multipart(
        {
            "models": "nudity-2.0",
            "api_user": SIGHTENGINE_USER,
            "api_secret": secret,
        },
        file_bytes,
    )
    request = urllib_request.Request(
        SIGHTENGINE_URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, json.JSONDecodeError) as error:
        raise ImageModerationError(f"Sightengine request failed: {error}") from error

    if result.get("status") != "success":
        raise ImageModerationError(
            f"Sightengine rejected the image: {result.get('error', result)}"
        )

    nudity = result.get("nudity") or {}
    sexual_display = float(nudity.get("sexual_display", 0))
    erotica = float(nudity.get("erotica", 0))
    return sexual_display > 0.5 or erotica > 0.5


def send_message(
    api: TelegramBridge,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    body: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to is not None:
        body["reply_parameters"] = {"message_id": reply_to}
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    api.call("sendMessage", body)


def menu_text(first_name: str) -> str:
    return (
        f"أهلاً بك يا {first_name}!\n\n"
        "أنا بوت حماية المجموعات. اختر من القائمة ما تريد معرفته:"
    )


def edit_menu_message(
    api: TelegramBridge,
    callback_query: dict[str, Any],
    text: str,
) -> None:
    callback_message = callback_query.get("message") or {}
    chat = callback_message.get("chat") or {}
    message_id = callback_message.get("message_id")
    if not chat.get("id") or not message_id:
        return

    api.call(
        "editMessageText",
        {
            "chat_id": chat["id"],
            "message_id": message_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "العودة للقائمة", "callback_data": "menu_home"}]
                ]
            },
        },
    )


def handle_callback_query(
    api: TelegramBridge,
    moderator: GroupModerator,
    callback_query: dict[str, Any],
) -> None:
    callback_id = callback_query.get("id")
    if callback_id:
        api.call("answerCallbackQuery", {"callback_query_id": callback_id})

    callback_message = callback_query.get("message") or {}
    chat = callback_message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    data = callback_query.get("data")
    if data == "menu_home":
        user = callback_query.get("from") or {}
        edit_menu_message(api, callback_query, menu_text(user.get("first_name", "صديقي")))
    elif data == "menu_rules":
        edit_menu_message(
            api,
            callback_query,
            "قواعد الحماية:\n"
            "• حذف الروابط والرسائل المزعجة\n"
            "• فحص الصور غير اللائقة\n"
            "• منع تكرار الرسائل والإغراق\n"
            "• تجاهل رسائل مشرفي المجموعة",
        )
    elif data == "menu_help":
        edit_menu_message(
            api,
            callback_query,
            "الأوامر المتاحة:\n"
            "/start — فتح القائمة\n"
            "/help — عرض المساعدة\n"
            "/rules — عرض قواعد الحماية\n"
            "/modstatus — حالة الحماية\n"
            "/ban — حظر مستخدم بالرد على رسالته",
        )
    elif data == "menu_ban":
        edit_menu_message(
            api,
            callback_query,
            "لحظر مستخدم:\n"
            "1. تأكد أنك مشرف في المجموعة.\n"
            "2. اضغط مطولاً على رسالة المستخدم.\n"
            "3. اختر الرد، ثم اكتب /ban.\n\n"
            "يجب أن يكون لدي صلاحية حظر المستخدمين.",
        )
    elif data == "menu_status":
        if chat.get("type") in {"group", "supergroup"}:
            status = (
                "نشطة — لدي صلاحية حذف الرسائل."
                if moderator.bot_can_delete(chat_id)
                else "قيد الانتظار — أضفني كمشرف مع صلاحية حذف الرسائل."
            )
        else:
            status = "أضفني إلى مجموعة كمشرف لتفعيل الحماية."
        edit_menu_message(api, callback_query, f"حالة الحماية: {status}")


def check_photo(
    api: TelegramBridge,
    moderator: GroupModerator,
    message: dict[str, Any],
) -> None:
    if not is_group(message) or not message.get("photo"):
        return

    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    sender_id = sender.get("id")
    if sender.get("is_bot") or sender_id is None:
        return
    if moderator.member_is_admin(chat_id, sender_id):
        return
    if not moderator.bot_can_delete(chat_id):
        return

    largest_photo = message["photo"][-1]
    try:
        file_info = api.call("getFile", {"file_id": largest_photo["file_id"]})
        file_path = (file_info or {}).get("file_path")
        if not file_path:
            raise ImageModerationError("Telegram did not return an image path")

        image_bytes = download_telegram_file(file_path)
        if not sightengine_detects_nudity(image_bytes):
            return

        api.call(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message["message_id"]},
        )
        username = sender.get("username")
        mention = f"@{username}" if username else sender.get("first_name", "المستخدم")
        send_message(api, chat_id, f"⚠️ تم حذف صورة غير لائقة من {mention}")
        logger.info(
            "Removed an image flagged by Sightengine in chat %s (message %s)",
            chat_id,
            message["message_id"],
        )
        moderator.record_event(
            chat_id,
            "image_nudity",
            message_id=message["message_id"],
            user_id=sender_id,
            username=sender.get("username"),
            reason="Sightengine nudity-2.0 threshold exceeded",
        )
    except (TelegramError, ImageModerationError) as error:
        logger.warning("Could not scan Telegram image: %s", error)


def handle_message(
    api: TelegramBridge,
    moderator: GroupModerator,
    message: dict[str, Any],
) -> None:
    text = (message.get("text") or "").strip()
    if not text:
        return

    chat_id = message["chat"]["id"]
    command = command_name(text)
    first_name = (message.get("from") or {}).get("first_name", "there")
    reply_to = message.get("message_id")
    args = command_args(text)

    def save_group_settings(**settings: Any) -> bool:
        if not moderator.service or not is_group(message):
            return False
        try:
            moderator.service.update_settings(
                chat_id,
                title=(message.get("chat") or {}).get("title"),
                **settings,
            )
            return True
        except BotServiceError as error:
            logger.warning("Could not save group settings: %s", error)
            return False

    if command == "/start":
        save_group_settings()
        send_message(
            api,
            chat_id,
            menu_text(first_name),
            reply_to,
            ARABIC_MENU,
        )
        return

    if command == "/help":
        send_message(
            api,
            chat_id,
            "الأوامر المتاحة:\n/start — فتح القائمة\n/help — عرض المساعدة\n"
            "/rules — عرض قواعد الحماية\n/modstatus — حالة الحماية\n"
            "/ban — حظر مستخدم بالرد على رسالته\n"
            "/weather — حالة الطقس، مثال: /weather Cairo\n"
            "/translate — ترجمة نص، مثال: /translate en مرحباً\n"
            "/dashboard — رابط لوحة المشرف\n"
            "/alerts — تشغيل أو إيقاف التنبيهات الخارجية\n\n"
            "استخدم /start لفتح القائمة التفاعلية.",
            reply_to,
        )
        return

    if command == "/ban":
        if not is_group(message):
            send_message(api, chat_id, "هذا الأمر متاح داخل المجموعات فقط.", reply_to)
            return

        caller_id = (message.get("from") or {}).get("id")
        replied_message = message.get("reply_to_message")
        target_user = (replied_message or {}).get("from") or {}
        if caller_id is None or not moderator.member_is_admin(chat_id, caller_id):
            send_message(api, chat_id, "هذا الأمر متاح لمشرفي المجموعة فقط.", reply_to)
            return
        if not replied_message or not target_user.get("id"):
            send_message(api, chat_id, "رد على رسالة الشخص الذي تريد حظره.", reply_to)
            return
        if target_user.get("is_bot") or moderator.member_is_admin(
            chat_id, target_user["id"]
        ):
            send_message(api, chat_id, "لا يمكنني حظر مشرف أو بوت.", reply_to)
            return
        if not moderator.bot_can_ban(chat_id):
            send_message(
                api,
                chat_id,
                "خطأ: تأكد أنني مشرف وأملك صلاحيات الحظر.",
                reply_to,
            )
            return

        try:
            api.call(
                "banChatMember",
                {"chat_id": chat_id, "user_id": target_user["id"]},
            )
            send_message(api, chat_id, "تم حظر المستخدم بنجاح.", reply_to)
            moderator.record_event(
                chat_id,
                "ban",
                message_id=reply_to,
                user_id=target_user["id"],
                username=target_user.get("username"),
            )
        except TelegramError as error:
            logger.warning("Could not ban user in chat %s: %s", chat_id, error)
            send_message(
                api,
                chat_id,
                "خطأ: تأكد أنني مشرف وأملك صلاحيات الحظر.",
                reply_to,
            )
        return

    if command == "/rules":
        send_message(
            api,
            chat_id,
            "قواعد الحماية:\n"
            "• حذف الروابط والرسائل المزعجة\n"
            "• فحص الصور غير اللائقة\n"
            "• منع تكرار الرسائل والإغراق\n"
            "• تجاهل رسائل المشرفين والبوتات",
            reply_to,
        )
        return

    if command == "/modstatus":
        save_group_settings()
        status = (
            "نشطة — لدي صلاحية حذف الرسائل"
            if moderator.bot_can_delete(chat_id)
            else "قيد الانتظار — أضفني كمشرف مع صلاحية حذف الرسائل"
        )
        send_message(api, chat_id, f"حالة الحماية: {status}.", reply_to)
        return

    if command in {"/weather", "/طقس"}:
        location = args
        if not location and moderator.service:
            try:
                location = moderator.service.get_settings(chat_id).get("weatherLocation") or ""
            except BotServiceError:
                location = ""
        location = location or "Cairo"
        try:
            if not moderator.service:
                raise BotServiceError("Bot API is not available")
            weather = moderator.service.weather(location)
            send_message(
                api,
                chat_id,
                f"الطقس الآن في {weather.get('location', location)}:\n"
                f"🌡️ الحرارة: {weather.get('temperature', '—')}{weather.get('unit', '°C')}\n"
                f"🤍 الحالة: {weather.get('description', 'غير معروفة')}\n"
                f"💧 الرطوبة: {weather.get('humidity', '—')}%\n"
                f"💨 سرعة الرياح: {weather.get('windSpeed', '—')} كم/س",
                reply_to,
            )
            save_group_settings(weatherLocation=location)
        except BotServiceError as error:
            logger.warning("Weather command failed: %s", error)
            send_message(api, chat_id, "تعذر الحصول على حالة الطقس حالياً.", reply_to)
        return

    if command in {"/translate", "/ترجم"}:
        source_text = args
        if message.get("reply_to_message"):
            source_text = (
                (message["reply_to_message"].get("text") or "")
                if not source_text
                else source_text
            )
        target = "ar"
        if source_text:
            parts = source_text.split(maxsplit=1)
            if len(parts) == 2 and re.fullmatch(r"[a-zA-Z]{2}", parts[0]):
                target, source_text = parts[0].lower(), parts[1]
        if not source_text:
            send_message(
                api,
                chat_id,
                "اكتب النص بعد الأمر أو استخدم الأمر بالرد على رسالة.\n"
                "مثال: /translate en مرحباً",
                reply_to,
            )
            return
        try:
            if not moderator.service:
                raise BotServiceError("Bot API is not available")
            translated = moderator.service.translate(source_text[:500], target)
            send_message(api, chat_id, f"الترجمة ({target}):\n{translated}", reply_to)
        except BotServiceError as error:
            logger.warning("Translation command failed: %s", error)
            send_message(api, chat_id, "تعذرت الترجمة حالياً.", reply_to)
        return

    if command in {"/dashboard", "/لوحة"}:
        if not is_group(message):
            send_message(api, chat_id, "هذا الأمر متاح داخل المجموعات فقط.", reply_to)
            return
        caller_id = (message.get("from") or {}).get("id")
        if caller_id is None or not moderator.member_is_admin(chat_id, caller_id):
            send_message(api, chat_id, "هذا الأمر متاح لمشرفي المجموعة فقط.", reply_to)
            return
        try:
            if not moderator.service:
                raise BotServiceError("Bot API is not available")
            dashboard_url = moderator.service.create_dashboard_token(chat_id)
            send_message(
                api,
                chat_id,
                "لوحة المشرف متاحة لمدة 24 ساعة:\n"
                f"{dashboard_url}\n\n"
                "لا تشارك هذا الرابط مع الآخرين.",
                reply_to,
            )
        except BotServiceError as error:
            logger.warning("Dashboard command failed: %s", error)
            send_message(api, chat_id, "تعذر إنشاء رابط لوحة المشرف حالياً.", reply_to)
        return

    if command in {"/alerts", "/تنبيهات"}:
        if not is_group(message):
            send_message(api, chat_id, "هذا الأمر متاح داخل المجموعات فقط.", reply_to)
            return
        caller_id = (message.get("from") or {}).get("id")
        if caller_id is None or not moderator.member_is_admin(chat_id, caller_id):
            send_message(api, chat_id, "هذا الأمر متاح لمشرفي المجموعة فقط.", reply_to)
            return
        enabled = args.lower() in {"on", "تشغيل", "نعم", "1"}
        disabled = args.lower() in {"off", "إيقاف", "لا", "0"}
        if not enabled and not disabled:
            send_message(
                api,
                chat_id,
                "استخدم /alerts on أو /alerts off.\n"
                "لإضافة رابط تنبيهات خارجي استخدم لوحة المشرف.",
                reply_to,
            )
            return
        if save_group_settings(alertsEnabled=enabled):
            send_message(
                api,
                chat_id,
                "تم تشغيل التنبيهات الخارجية." if enabled else "تم إيقاف التنبيهات الخارجية.",
                reply_to,
            )
        else:
            send_message(api, chat_id, "تعذر حفظ إعدادات التنبيهات.", reply_to)
        return

    if is_group(message):
        reason = moderator.moderate(message)
        if reason == "link":
            username = (message.get("from") or {}).get("username")
            mention = f"@{username}" if username else first_name
            send_message(
                api,
                chat_id,
                f"{mention} ممنوع إرسال الروابط!",
            )
        return

    send_message(
        api,
        chat_id,
        f"قلت: {text}\n\nأضفني إلى مجموعة وسأساعد في الحماية من الرسائل المزعجة.",
        reply_to,
    )


def run() -> None:
    api = TelegramBridge()
    should_run = True

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal should_run
        should_run = False

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    try:
        bot = api.call("getMe")
        api.call("deleteWebhook", {"drop_pending_updates": False})
        api.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "فتح القائمة الرئيسية"},
                    {"command": "help", "description": "عرض المساعدة"},
                    {"command": "rules", "description": "عرض قواعد الحماية"},
                    {"command": "modstatus", "description": "حالة الحماية"},
                    {"command": "ban", "description": "حظر مستخدم بالرد على رسالته"},
                    {"command": "weather", "description": "عرض حالة الطقس"},
                    {"command": "translate", "description": "ترجمة نص"},
                    {"command": "dashboard", "description": "لوحة المشرف"},
                    {"command": "alerts", "description": "إعداد التنبيهات"},
                ]
            },
        )
        logger.info(
            "Connected as @%s (%s)",
            bot.get("username", "unknown"),
            bot.get("id"),
        )

        service = BotServiceClient()
        moderator = GroupModerator(api, bot["id"], service)
        offset = 0
        while should_run:
            try:
                updates = api.call(
                    "getUpdates",
                    {
                        "timeout": POLL_TIMEOUT_SECONDS,
                        "offset": offset + 1,
                        "allowed_updates": ["message", "callback_query"],
                    },
                ) or []
                for update in updates:
                    offset = max(offset, update["update_id"])
                    try:
                        if update.get("callback_query"):
                            handle_callback_query(
                                api,
                                moderator,
                                update["callback_query"],
                            )
                            continue
                        message = update.get("message")
                        if message:
                            if message.get("photo"):
                                check_photo(api, moderator, message)
                            else:
                                handle_message(api, moderator, message)
                    except TelegramError as error:
                        logger.warning("Update handling failed: %s", error)
            except TelegramError as error:
                logger.error("Telegram polling error: %s", error)
                if should_run:
                    time.sleep(RETRY_DELAY_SECONDS)
    finally:
        api.close()


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logger.exception("Group bot stopped unexpectedly")
        sys.exit(1)