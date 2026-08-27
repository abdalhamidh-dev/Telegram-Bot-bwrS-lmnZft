import { ReplitConnectors } from "@replit/connectors-sdk";
import { logger } from "./logger";

type TelegramUser = {
  id: number;
  is_bot: boolean;
  first_name: string;
  username?: string;
};

type TelegramChat = {
  id: number;
  type: string;
  title?: string;
  username?: string;
};

type TelegramMessage = {
  message_id: number;
  chat: TelegramChat;
  from?: TelegramUser;
  text?: string;
};

type TelegramUpdate = {
  update_id: number;
  message?: TelegramMessage;
};

type TelegramResponse<T> = {
  ok: boolean;
  result?: T;
  description?: string;
};

const connectors = new ReplitConnectors();
const pollTimeoutSeconds = 25;

let polling = false;
let lastUpdateId = 0;
let bot: TelegramUser | null = null;

async function telegramRequest<T>(
  method: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const response = await connectors.proxy("telegram", `/${method}`, {
    method: body ? "POST" : "GET",
    ...(body
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });

  const payload = (await response.json()) as TelegramResponse<T>;
  if (!response.ok || !payload.ok || payload.result === undefined) {
    throw new Error(
      `Telegram ${method} failed: ${payload.description ?? response.statusText}`,
    );
  }

  return payload.result;
}

async function sendMessage(chatId: number, text: string, replyTo?: number) {
  await telegramRequest("sendMessage", {
    chat_id: chatId,
    text,
    ...(replyTo
      ? { reply_parameters: { message_id: replyTo } }
      : {}),
  });
}

async function handleMessage(message: TelegramMessage) {
  const text = message.text?.trim();
  if (!text) return;

  const command = text.split(/\s+/, 1)[0]?.toLowerCase();
  const firstName = message.from?.first_name ?? "there";

  if (command === "/start") {
    await sendMessage(
      message.chat.id,
      `Hi ${firstName}! I’m your Telegram bot.\n\nSend me a message and I’ll reply. Use /help to see what I can do.`,
      message.message_id,
    );
    return;
  }

  if (command === "/help") {
    await sendMessage(
      message.chat.id,
      "Available commands:\n/start — welcome message\n/help — show this help\n/about — learn about this bot",
      message.message_id,
    );
    return;
  }

  if (command === "/about") {
    await sendMessage(
      message.chat.id,
      "I’m a starter Telegram bot built on your Replit project. My command flow is ready for your custom features.",
      message.message_id,
    );
    return;
  }

  await sendMessage(
    message.chat.id,
    `You said: ${text}\n\nTell me what you want this bot to do and I can add that workflow.`,
    message.message_id,
  );
}

async function pollOnce() {
  const updates = await telegramRequest<TelegramUpdate[]>("getUpdates", {
    timeout: pollTimeoutSeconds,
    offset: lastUpdateId + 1,
    allowed_updates: ["message"],
  });

  for (const update of updates) {
    lastUpdateId = Math.max(lastUpdateId, update.update_id);
    if (!update.message) continue;

    try {
      await handleMessage(update.message);
    } catch (error) {
      logger.error(
        { err: error, updateId: update.update_id },
        "Failed to handle Telegram message",
      );
    }
  }
}

async function pollForever() {
  if (polling) return;
  polling = true;

  while (polling) {
    try {
      await pollOnce();
    } catch (error) {
      logger.error({ err: error }, "Telegram polling error");
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
  }
}

export async function startTelegramBot() {
  if (polling) return bot;

  bot = await telegramRequest<TelegramUser>("getMe");
  await telegramRequest("deleteWebhook", { drop_pending_updates: false });
  await telegramRequest("setMyCommands", {
    commands: [
      { command: "start", description: "Start the bot" },
      { command: "help", description: "Show available commands" },
      { command: "about", description: "About this bot" },
    ],
  });

  logger.info(
    { username: bot.username, botId: bot.id },
    "Telegram bot connected",
  );
  void pollForever();
  return bot;
}

export function getTelegramStatus() {
  return {
    connected: bot !== null,
    polling,
    bot: bot
      ? {
          id: bot.id,
          username: bot.username ?? null,
          firstName: bot.first_name,
        }
      : null,
  };
}

export function stopTelegramBot() {
  polling = false;
}