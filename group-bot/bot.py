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


def command_name(text: str) -> str:
    return text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()


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
    def __init__(self, api: TelegramBridge, bot_id: int) -> None:
        self.api = api
        self.bot_id = bot_id
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
            return reason
        except TelegramError as error:
            logger.warning("Could not remove message: %s", error)
            return None


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

    if command == "/start":
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
            "الأوامر المتاحة:\n/start — بدء البوت\n/help — عرض المساعدة\n"
            "/rules — عرض قواعد الحماية\n/modstatus — حالة الحماية\n"
            "/ban — حظر مستخدم بالرد على رسالته\n\n"
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
            "Moderation removes obvious scam links, repeated flood messages, "
            "and aggressive all-caps spam. Group admins are always ignored.",
            reply_to,
        )
        return

    if command == "/modstatus":
        status = (
            "active — the bot has permission to delete messages"
            if moderator.bot_can_delete(chat_id)
            else "waiting — make the bot a group administrator with permission to delete messages"
        )
        send_message(api, chat_id, f"Moderation is {status}.", reply_to)
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
        f"You said: {text}\n\nAdd me to a group and I’ll help moderate obvious spam.",
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
                ]
            },
        )
        logger.info(
            "Connected as @%s (%s)",
            bot.get("username", "unknown"),
            bot.get("id"),
        )

        moderator = GroupModerator(api, bot["id"])
        offset = 0
        while should_run:
            try:
                updates = api.call(
                    "getUpdates",
                    {
                        "timeout": POLL_TIMEOUT_SECONDS,
                        "offset": offset + 1,
                        "allowed_updates": ["message"],
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