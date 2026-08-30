---
name: Telegram bot runtime lessons
description: Durable lessons for Telegram polling and inline menu behavior in this project.
---

Telegram inline keyboards must use the Bot API's nested row format, including for a single button. When migrating polling runtimes, stop any orphaned legacy poller before starting the replacement; concurrent getUpdates calls produce a 409 conflict and make the bot appear unresponsive.

**Why:** The bot displayed its menu but failed while handling button updates, and an orphaned legacy process caused competing polling sessions.

**How to apply:** Preserve nested `inline_keyboard` rows and verify that exactly one Telegram polling process owns the bot before testing commands.