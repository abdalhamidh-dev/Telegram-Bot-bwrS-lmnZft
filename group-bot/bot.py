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


class TelegramError(RuntimeError):
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
        key = (chat_id, self.bot_id)
        cached = self.admin_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        try:
            member = self.api.call(
                "getChatMember",
                {"chat_id": chat_id, "user_id": self.bot_id},
            )
            result = can_delete_messages(member or {})
        except TelegramError as error:
            logger.warning("Could not inspect bot permissions: %s", error)
            result = False

        self.admin_cache[key] = (time.monotonic() + ADMIN_CACHE_SECONDS, result)
        return result

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

    def should_remove(self, message: dict[str, Any]) -> bool:
        text = (message.get("text") or "").strip()
        sender = message.get("from") or {}
        if not text or sender.get("is_bot"):
            return False

        chat_id = message["chat"]["id"]
        user_id = sender.get("id")
        if user_id is None or self.member_is_admin(chat_id, user_id):
            return False

        return looks_like_spam(text) or self.is_flood(chat_id, user_id, text)

    def moderate(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        if not self.bot_can_delete(chat_id):
            return
        if not self.should_remove(message):
            return

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
        except TelegramError as error:
            logger.warning("Could not remove message: %s", error)


def send_message(api: TelegramBridge, chat_id: int, text: str, reply_to: int | None = None) -> None:
    body: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to is not None:
        body["reply_parameters"] = {"message_id": reply_to}
    api.call("sendMessage", body)


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
            f"Hi {first_name}! I’m ready to help keep this group clear of obvious spam.\n\n"
            "Use /help to see what I can do.",
            reply_to,
        )
        return

    if command == "/help":
        send_message(
            api,
            chat_id,
            "Commands:\n/start — start the bot\n/help — show this help\n"
            "/rules — show moderation rules\n/modstatus — show moderation status",
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
        moderator.moderate(message)
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
                    {"command": "start", "description": "Start the bot"},
                    {"command": "help", "description": "Show available commands"},
                    {"command": "rules", "description": "Show moderation rules"},
                    {"command": "modstatus", "description": "Show moderation status"},
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
                    message = update.get("message")
                    if message:
                        try:
                            handle_message(api, moderator, message)
                        except TelegramError as error:
                            logger.warning("Message handling failed: %s", error)
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