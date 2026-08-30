import { createHash, randomBytes } from "node:crypto";
import { ReplitConnectors } from "@replit/connectors-sdk";
import { desc, eq, lt } from "drizzle-orm";
import {
  dashboardTokens,
  db,
  groupSettings,
  moderationEvents,
} from "@workspace/db";
import { logger } from "./lib/logger";

type TelegramObject = Record<string, any>;
type TelegramMessage = TelegramObject;

const POLL_TIMEOUT_SECONDS = 25;
const RETRY_DELAY_MS = 5_000;
const ADMIN_CACHE_MS = 60_000;
const FLOOD_WINDOW_MS = 12_000;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json";
const SIGHTENGINE_USER = "1290792300";
const DASHBOARD_TOKEN_TTL_MS = 24 * 60 * 60 * 1000;

const scamPattern = /\b(?:free\s+(?:crypto|bitcoin|money)|guaranteed\s+profit|double\s+your\s+(?:bitcoin|money)|claim\s+your\s+airdrop|investment\s+signal|verify\s+your\s+account|earn\s+\$?\d+)\b/i;
const urlPattern = /(?:https?:\/\/|www\.|t\.me\/)/i;
const invitePattern = /(?:t\.me\/(?:\+|joinchat)|telegram\.me\/joinchat)/i;

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

const arabicMenu = {
  inline_keyboard: [
    [
      { text: "قواعد الحماية", callback_data: "menu_rules" },
      { text: "حالة البوت", callback_data: "menu_status" },
    ],
    [
      { text: "المساعدة", callback_data: "menu_help" },
      { text: "طريقة الحظر", callback_data: "menu_ban" },
    ],
  ],
};

class TelegramApi {
  private readonly connectors = new ReplitConnectors();

  async call<T = any>(method: string, body?: TelegramObject): Promise<T> {
    const response = await this.connectors.proxy("telegram", `/${method}`, {
      method: body === undefined ? "GET" : "POST",
      ...(body === undefined
        ? {}
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }),
    });
    const payload = (await response.json()) as TelegramObject;
    if (!response.ok || !payload.ok) {
      throw new Error(
        `Telegram ${method} failed: ${payload.description ?? response.status}`,
      );
    }
    return payload.result as T;
  }
}

function isGroup(message: TelegramMessage): boolean {
  return ["group", "supergroup"].includes(message.chat?.type);
}

function isAdmin(member: TelegramObject): boolean {
  return ["administrator", "creator"].includes(member.status);
}

function canDeleteMessages(member: TelegramObject): boolean {
  return (
    member.status === "creator" ||
    (member.status === "administrator" && member.can_delete_messages === true)
  );
}

function canRestrictMembers(member: TelegramObject): boolean {
  return (
    member.status === "creator" ||
    (member.status === "administrator" && member.can_restrict_members === true)
  );
}

function commandName(text: string): string {
  return text.split(/\s+/, 1)[0].split("@", 1)[0].toLowerCase();
}

function commandArgs(text: string): string {
  return text.split(/\s+/, 2)[1]?.trim() ?? "";
}

function looksLikeSpam(text: string): boolean {
  const compact = text.replace(/\s+/g, " ").trim();
  if (scamPattern.test(compact) && urlPattern.test(compact)) return true;
  if (invitePattern.test(compact) && (scamPattern.test(compact) || compact.length > 160)) {
    return true;
  }
  const letters = [...compact].filter((character) => /\p{L}/u.test(character));
  const uppercaseRatio =
    letters.length > 0
      ? letters.filter((character) => character === character.toUpperCase()).length /
        letters.length
      : 0;
  return letters.length >= 30 && uppercaseRatio >= 0.9 && compact.split("!").length - 1 >= 2;
}

async function getSettings(chatId: number) {
  const rows = await db
    .select()
    .from(groupSettings)
    .where(eq(groupSettings.chatId, chatId))
    .limit(1);
  return rows[0] ?? null;
}

async function ensureSettings(chatId: number, title?: string | null) {
  const existing = await getSettings(chatId);
  if (existing) {
    if (title && existing.title !== title) {
      const updated = await db
        .update(groupSettings)
        .set({ title, updatedAt: new Date() })
        .where(eq(groupSettings.chatId, chatId))
        .returning();
      return updated[0] ?? existing;
    }
    return existing;
  }
  const inserted = await db
    .insert(groupSettings)
    .values({ chatId, title: title ?? null })
    .returning();
  return inserted[0];
}

async function updateSettings(
  chatId: number,
  values: {
    title?: string | null;
    translationEnabled?: boolean;
    alertsEnabled?: boolean;
    weatherLocation?: string | null;
  },
) {
  const existing = await ensureSettings(chatId, values.title);
  const updated = await db
    .update(groupSettings)
    .set({
      ...(values.title !== undefined ? { title: values.title } : {}),
      ...(values.translationEnabled !== undefined
        ? { translationEnabled: values.translationEnabled }
        : {}),
      ...(values.alertsEnabled !== undefined
        ? { alertsEnabled: values.alertsEnabled }
        : {}),
      ...(values.weatherLocation !== undefined
        ? { weatherLocation: values.weatherLocation }
        : {}),
      updatedAt: new Date(),
    })
    .where(eq(groupSettings.chatId, existing.chatId))
    .returning();
  return updated[0] ?? existing;
}

function weatherDescription(code: number, language: string): string {
  const descriptions: Record<number, [string, string]> = {
    0: ["سماء صافية", "Clear sky"],
    1: ["غائم جزئياً", "Mainly clear"],
    2: ["غائم جزئياً", "Partly cloudy"],
    3: ["غائم", "Overcast"],
    45: ["ضباب", "Fog"],
    48: ["ضباب متجمد", "Depositing rime fog"],
    51: ["رذاذ خفيف", "Light drizzle"],
    53: ["رذاذ متوسط", "Moderate drizzle"],
    55: ["رذاذ كثيف", "Dense drizzle"],
    61: ["أمطار خفيفة", "Slight rain"],
    63: ["أمطار متوسطة", "Moderate rain"],
    65: ["أمطار غزيرة", "Heavy rain"],
    71: ["ثلوج خفيفة", "Slight snow"],
    73: ["ثلوج متوسطة", "Moderate snow"],
    75: ["ثلوج غزيرة", "Heavy snow"],
    80: ["زخات مطر خفيفة", "Slight rain showers"],
    81: ["زخات مطر متوسطة", "Moderate rain showers"],
    82: ["زخات مطر غزيرة", "Violent rain showers"],
    95: ["عاصفة رعدية", "Thunderstorm"],
    96: ["عاصفة رعدية مع برد", "Thunderstorm with hail"],
    99: ["عاصفة رعدية مع برد", "Thunderstorm with heavy hail"],
  };
  const [arabic, english] = descriptions[code] ?? ["حالة جوية غير معروفة", "Unknown"];
  return language === "en" ? english : arabic;
}

async function fetchJson(url: string): Promise<TelegramObject> {
  const response = await fetch(url, {
    signal: AbortSignal.timeout(10_000),
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`External service returned ${response.status}`);
  return (await response.json()) as TelegramObject;
}

async function lookupWeather(location: string, language = "ar") {
  const geoUrl = new URL("https://geocoding-api.open-meteo.com/v1/search");
  geoUrl.search = new URLSearchParams({
    name: location,
    count: "1",
    language,
    format: "json",
  }).toString();
  const geocoding = await fetchJson(geoUrl.toString());
  const place = geocoding.results?.[0];
  if (!place) throw new Error("Location not found");

  const forecastUrl = new URL("https://api.open-meteo.com/v1/forecast");
  forecastUrl.search = new URLSearchParams({
    latitude: String(place.latitude),
    longitude: String(place.longitude),
    current:
      "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
    timezone: "auto",
  }).toString();
  const forecast = await fetchJson(forecastUrl.toString());
  const current = forecast.current ?? {};
  return {
    location: place.name,
    temperature: current.temperature_2m,
    unit: forecast.current_units?.temperature_2m ?? "°C",
    description: weatherDescription(Number(current.weather_code), language),
    humidity: current.relative_humidity_2m,
    windSpeed: current.wind_speed_10m,
  };
}

async function translateText(text: string, target: string): Promise<string> {
  const url = new URL("https://api.mymemory.translated.net/get");
  url.search = new URLSearchParams({
    q: text,
    langpair: `auto|${target}`,
  }).toString();
  const result = await fetchJson(url.toString());
  const translated = result.responseData?.translatedText;
  if (typeof translated !== "string" || !translated.trim()) {
    throw new Error("Translation service returned no translation");
  }
  return translated;
}

async function downloadTelegramFile(filePath: string): Promise<ArrayBuffer> {
  const token = process.env["TELEGRAM_BOT_TOKEN"];
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is not configured");
  const response = await fetch(
    `https://api.telegram.org/file/bot${token}/${filePath.replace(/^\/+/, "")}`,
    { signal: AbortSignal.timeout(30_000) },
  );
  if (!response.ok) throw new Error(`Could not download Telegram image (${response.status})`);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > MAX_IMAGE_BYTES) throw new Error("Image is larger than 20 MB");
  return bytes;
}

async function imageHasNudity(bytes: ArrayBuffer): Promise<boolean> {
  const secret = process.env["SIGHTENGINE_SECRET"];
  if (!secret) throw new Error("SIGHTENGINE_SECRET is not configured");
  const form = new FormData();
  form.append("models", "nudity-2.0");
  form.append("api_user", SIGHTENGINE_USER);
  form.append("api_secret", secret);
  form.append("media", new Blob([bytes], { type: "image/jpeg" }), "telegram-photo.jpg");
  const response = await fetch(SIGHTENGINE_URL, {
    method: "POST",
    body: form,
    signal: AbortSignal.timeout(45_000),
  });
  const result = (await response.json()) as TelegramObject;
  if (!response.ok || result.status !== "success") {
    throw new Error(`Sightengine rejected the image: ${JSON.stringify(result.error ?? result)}`);
  }
  const nudity = result.nudity ?? {};
  return Number(nudity.sexual_display ?? 0) > 0.5 || Number(nudity.erotica ?? 0) > 0.5;
}

async function sendExternalAlert(chatId: number, payload: TelegramObject) {
  const settings = await getSettings(chatId);
  if (!settings?.alertsEnabled || !settings.alertWebhookUrl) return;
  try {
    const response = await fetch(settings.alertWebhookUrl, {
      method: "POST",
      signal: AbortSignal.timeout(5_000),
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ source: "telegram-group-bot", chat_id: chatId, ...payload }),
    });
    if (!response.ok) {
      logger.warn({ chatId, status: response.status }, "External alert webhook returned an error");
    }
  } catch (error) {
    logger.warn({ chatId, error }, "External alert webhook failed");
  }
}

async function recordEvent(
  chatId: number,
  eventType: string,
  message: TelegramMessage,
  details: TelegramObject = {},
) {
  const sender = message.from ?? {};
  const event = await db
    .insert(moderationEvents)
    .values({
      chatId,
      messageId: message.message_id ?? null,
      userId: sender.id ?? null,
      username: sender.username ?? null,
      eventType,
      details,
    })
    .returning();
  void sendExternalAlert(chatId, {
    event_type: eventType,
    message_id: message.message_id ?? null,
    user_id: sender.id ?? null,
    username: sender.username ?? null,
    details,
    created_at: event[0]?.createdAt?.toISOString() ?? new Date().toISOString(),
  });
}

class TelegramBot {
  private readonly api = new TelegramApi();
  private readonly adminCache = new Map<string, { expiresAt: number; value: boolean }>();
  private readonly permissionCache = new Map<
    number,
    { expiresAt: number; canDelete: boolean; canBan: boolean }
  >();
  private readonly recentMessages = new Map<
    string,
    Array<{ createdAt: number; text: string }>
  >();
  private running = true;
  private botId = 0;

  private async memberIsAdmin(chatId: number, userId: number): Promise<boolean> {
    const key = `${chatId}:${userId}`;
    const cached = this.adminCache.get(key);
    if (cached && cached.expiresAt > Date.now()) return cached.value;
    let value = false;
    try {
      value = isAdmin(
        await this.api.call("getChatMember", { chat_id: chatId, user_id: userId }),
      );
    } catch (error) {
      logger.warn({ error, chatId, userId }, "Could not inspect group member");
    }
    this.adminCache.set(key, { expiresAt: Date.now() + ADMIN_CACHE_MS, value });
    return value;
  }

  private async botPermissions(chatId: number) {
    const cached = this.permissionCache.get(chatId);
    if (cached && cached.expiresAt > Date.now()) return cached;
    let canDelete = false;
    let canBan = false;
    try {
      const member = await this.api.call("getChatMember", {
        chat_id: chatId,
        user_id: this.botId,
      });
      canDelete = canDeleteMessages(member);
      canBan = canRestrictMembers(member);
    } catch (error) {
      logger.warn({ error, chatId }, "Could not inspect bot permissions");
    }
    const value = { expiresAt: Date.now() + ADMIN_CACHE_MS, canDelete, canBan };
    this.permissionCache.set(chatId, value);
    return value;
  }

  private async botCanDelete(chatId: number): Promise<boolean> {
    return (await this.botPermissions(chatId)).canDelete;
  }

  private async botCanBan(chatId: number): Promise<boolean> {
    return (await this.botPermissions(chatId)).canBan;
  }

  private isFlood(chatId: number, userId: number, text: string): boolean {
    const key = `${chatId}:${userId}`;
    const now = Date.now();
    const messages = (this.recentMessages.get(key) ?? []).filter(
      (message) => message.createdAt >= now - FLOOD_WINDOW_MS,
    );
    const normalized = text.replace(/\s+/g, " ").trim().toLowerCase();
    const sameText = messages.some((message) => message.text === normalized);
    messages.push({ createdAt: now, text: normalized });
    this.recentMessages.set(key, messages);
    return sameText || messages.length >= 5;
  }

  private async removalReason(message: TelegramMessage): Promise<string | null> {
    const text = String(message.text ?? "").trim();
    const sender = message.from ?? {};
    if (!text || sender.is_bot || sender.id === undefined) return null;
    if (await this.memberIsAdmin(message.chat.id, sender.id)) return null;
    if (urlPattern.test(text)) return "link";
    if (looksLikeSpam(text) || this.isFlood(message.chat.id, sender.id, text)) return "spam";
    return null;
  }

  private async moderate(message: TelegramMessage): Promise<string | null> {
    if (!(await this.botCanDelete(message.chat.id))) return null;
    const reason = await this.removalReason(message);
    if (!reason) return null;
    try {
      await this.api.call("deleteMessage", {
        chat_id: message.chat.id,
        message_id: message.message_id,
      });
      await recordEvent(message.chat.id, reason, message);
      logger.info(
        { chatId: message.chat.id, messageId: message.message_id, reason },
        "Removed a likely spam message",
      );
      return reason;
    } catch (error) {
      logger.warn({ error, chatId: message.chat.id }, "Could not remove message");
      return null;
    }
  }

  private async sendMessage(
    chatId: number,
    text: string,
    replyTo?: number,
    replyMarkup?: TelegramObject,
  ) {
    const body: TelegramObject = { chat_id: chatId, text };
    if (replyTo !== undefined) body.reply_parameters = { message_id: replyTo };
    if (replyMarkup) body.reply_markup = replyMarkup;
    await this.api.call("sendMessage", body);
  }

  private async checkPhoto(message: TelegramMessage) {
    if (!isGroup(message) || !message.photo?.length) return;
    const sender = message.from ?? {};
    if (sender.is_bot || sender.id === undefined) return;
    if (await this.memberIsAdmin(message.chat.id, sender.id)) return;
    if (!(await this.botCanDelete(message.chat.id))) return;
    try {
      const largestPhoto = message.photo[message.photo.length - 1];
      const file = await this.api.call("getFile", { file_id: largestPhoto.file_id });
      const imageBytes = await downloadTelegramFile(file.file_path);
      if (!(await imageHasNudity(imageBytes))) return;
      await this.api.call("deleteMessage", {
        chat_id: message.chat.id,
        message_id: message.message_id,
      });
      const mention = sender.username ? `@${sender.username}` : sender.first_name ?? "المستخدم";
      await this.sendMessage(message.chat.id, `⚠️ تم حذف صورة غير لائقة من ${mention}`);
      await recordEvent(message.chat.id, "image_nudity", message, {
        reason: "Sightengine nudity-2.0 threshold exceeded",
      });
    } catch (error) {
      logger.warn({ error, chatId: message.chat.id }, "Could not scan Telegram image");
    }
  }

  private menuText(firstName: string): string {
    return `أهلاً بك يا ${firstName}!\n\nأنا بوت حماية المجموعات. اختر من القائمة ما تريد معرفته:`;
  }

  private async editMenuMessage(callback: TelegramObject, text: string) {
    const message = callback.message ?? {};
    if (!message.chat?.id || !message.message_id) return;
    await this.api.call("editMessageText", {
      chat_id: message.chat.id,
      message_id: message.message_id,
      text,
      reply_markup: {
        inline_keyboard: [[{ text: "العودة للقائمة", callback_data: "menu_home" }]],
      },
    });
  }

  private async handleCallback(callback: TelegramObject) {
    if (callback.id) await this.api.call("answerCallbackQuery", { callback_query_id: callback.id });
    const chat = callback.message?.chat;
    if (!chat?.id) return;
    const data = callback.data;
    if (data === "menu_home") {
      await this.editMenuMessage(callback, this.menuText(callback.from?.first_name ?? "صديقي"));
    } else if (data === "menu_rules") {
      await this.editMenuMessage(
        callback,
        "قواعد الحماية:\n• حذف الروابط والرسائل المزعجة\n• فحص الصور غير اللائقة\n• منع تكرار الرسائل والإغراق\n• تجاهل رسائل مشرفي المجموعة",
      );
    } else if (data === "menu_help") {
      await this.editMenuMessage(
        callback,
        "الأوامر المتاحة:\n/start — فتح القائمة\n/help — عرض المساعدة\n/rules — عرض قواعد الحماية\n/modstatus — حالة الحماية\n/ban — حظر مستخدم بالرد على رسالته\n/weather — حالة الطقس\n/translate — ترجمة نص\n/dashboard — لوحة المشرف",
      );
    } else if (data === "menu_ban") {
      await this.editMenuMessage(
        callback,
        "لحظر مستخدم:\n1. تأكد أنك مشرف في المجموعة.\n2. اضغط مطولاً على رسالة المستخدم.\n3. اختر الرد، ثم اكتب /ban.\n\nيجب أن يكون لدي صلاحية حظر المستخدمين.",
      );
    } else if (data === "menu_status") {
      const status =
        chat.type === "group" || chat.type === "supergroup"
          ? (await this.botCanDelete(chat.id)
              ? "نشطة — لدي صلاحية حذف الرسائل."
              : "قيد الانتظار — أضفني كمشرف مع صلاحية حذف الرسائل.")
          : "أضفني إلى مجموعة كمشرف لتفعيل الحماية.";
      await this.editMenuMessage(callback, `حالة الحماية: ${status}`);
    }
  }

  private async dashboardUrl(chatId: number): Promise<string> {
    const token = randomBytes(32).toString("base64url");
    await db.delete(dashboardTokens).where(lt(dashboardTokens.expiresAt, new Date()));
    await db.insert(dashboardTokens).values({
      tokenHash: createHash("sha256").update(token).digest("hex"),
      chatId,
      expiresAt: new Date(Date.now() + DASHBOARD_TOKEN_TTL_MS),
    });
    const configuredBase = process.env["DASHBOARD_PUBLIC_URL"]?.replace(/\/$/, "");
    const domain = process.env["REPLIT_DEV_DOMAIN"];
    const baseUrl = configuredBase ?? (domain ? `https://${domain}` : "http://localhost:8080");
    return `${baseUrl}/api/dashboard?token=${encodeURIComponent(token)}`;
  }

  private async handleMessage(message: TelegramMessage) {
    const text = String(message.text ?? "").trim();
    if (!text) return;
    const chatId = message.chat.id as number;
    const replyTo = message.message_id as number | undefined;
    const command = commandName(text);
    const args = commandArgs(text);
    const firstName = message.from?.first_name ?? "there";
    const saveSettings = async (values: TelegramObject = {}) => {
      if (isGroup(message)) {
        await updateSettings(chatId, {
          title: message.chat.title,
          ...values,
        });
      }
    };

    if (command === "/start") {
      await saveSettings();
      await this.sendMessage(chatId, this.menuText(firstName), replyTo, arabicMenu);
      return;
    }
    if (command === "/help") {
      await this.sendMessage(
        chatId,
        "الأوامر المتاحة:\n/start — فتح القائمة\n/help — عرض المساعدة\n/rules — عرض قواعد الحماية\n/modstatus — حالة الحماية\n/ban — حظر مستخدم بالرد على رسالته\n/weather Cairo — حالة الطقس\n/translate en مرحباً — ترجمة نص\n/dashboard — لوحة المشرف\n/alerts on|off — إعداد التنبيهات الخارجية",
        replyTo,
      );
      return;
    }
    if (command === "/ban") {
      if (!isGroup(message)) {
        await this.sendMessage(chatId, "هذا الأمر متاح داخل المجموعات فقط.", replyTo);
        return;
      }
      const callerId = message.from?.id;
      const target = message.reply_to_message?.from;
      if (
        callerId === undefined ||
        !(await this.memberIsAdmin(chatId, callerId))
      ) {
        await this.sendMessage(chatId, "هذا الأمر متاح لمشرفي المجموعة فقط.", replyTo);
        return;
      }
      if (!target?.id) {
        await this.sendMessage(chatId, "رد على رسالة الشخص الذي تريد حظره.", replyTo);
        return;
      }
      if (target.is_bot || (await this.memberIsAdmin(chatId, target.id))) {
        await this.sendMessage(chatId, "لا يمكنني حظر مشرف أو بوت.", replyTo);
        return;
      }
      if (!(await this.botCanBan(chatId))) {
        await this.sendMessage(chatId, "خطأ: تأكد أنني مشرف وأملك صلاحيات الحظر.", replyTo);
        return;
      }
      try {
        await this.api.call("banChatMember", { chat_id: chatId, user_id: target.id });
        await this.sendMessage(chatId, "تم حظر المستخدم بنجاح.", replyTo);
        await recordEvent(chatId, "ban", message, {
          targetUserId: target.id,
          targetUsername: target.username ?? null,
        });
      } catch (error) {
        logger.warn({ error, chatId }, "Could not ban user");
        await this.sendMessage(chatId, "خطأ: تأكد أنني مشرف وأملك صلاحيات الحظر.", replyTo);
      }
      return;
    }
    if (command === "/rules") {
      await this.sendMessage(
        chatId,
        "قواعد الحماية:\n• حذف الروابط والرسائل المزعجة\n• فحص الصور غير اللائقة\n• منع تكرار الرسائل والإغراق\n• تجاهل رسائل المشرفين والبوتات",
        replyTo,
      );
      return;
    }
    if (command === "/modstatus") {
      await saveSettings();
      const status = await this.botCanDelete(chatId)
        ? "نشطة — لدي صلاحية حذف الرسائل"
        : "قيد الانتظار — أضفني كمشرف مع صلاحية حذف الرسائل";
      await this.sendMessage(chatId, `حالة الحماية: ${status}.`, replyTo);
      return;
    }
    if (command === "/weather" || command === "/طقس") {
      let location = args;
      if (!location) location = (await getSettings(chatId))?.weatherLocation ?? "Cairo";
      try {
        const weather = await lookupWeather(location);
        await this.sendMessage(
          chatId,
          `الطقس الآن في ${weather.location}:\n🌡️ الحرارة: ${weather.temperature}${weather.unit}\n🤍 الحالة: ${weather.description}\n💧 الرطوبة: ${weather.humidity}%\n💨 سرعة الرياح: ${weather.windSpeed} كم/س`,
          replyTo,
        );
        await saveSettings({ weatherLocation: location });
      } catch (error) {
        logger.warn({ error, chatId }, "Weather command failed");
        await this.sendMessage(chatId, "تعذر الحصول على حالة الطقس حالياً.", replyTo);
      }
      return;
    }
    if (command === "/translate" || command === "/ترجم") {
      let sourceText = args || message.reply_to_message?.text || "";
      let target = "ar";
      const targetMatch = sourceText.match(/^([a-zA-Z]{2})\s+(.+)$/s);
      if (targetMatch) {
        target = targetMatch[1].toLowerCase();
        sourceText = targetMatch[2];
      }
      if (!sourceText) {
        await this.sendMessage(
          chatId,
          "اكتب النص بعد الأمر أو استخدم الأمر بالرد على رسالة.\nمثال: /translate en مرحباً",
          replyTo,
        );
        return;
      }
      try {
        const translated = await translateText(sourceText.slice(0, 500), target);
        await this.sendMessage(chatId, `الترجمة (${target}):\n${translated}`, replyTo);
      } catch (error) {
        logger.warn({ error, chatId }, "Translation command failed");
        await this.sendMessage(chatId, "تعذرت الترجمة حالياً.", replyTo);
      }
      return;
    }
    if (command === "/dashboard" || command === "/لوحة") {
      if (!isGroup(message)) {
        await this.sendMessage(chatId, "هذا الأمر متاح داخل المجموعات فقط.", replyTo);
        return;
      }
      if (message.from?.id === undefined || !(await this.memberIsAdmin(chatId, message.from.id))) {
        await this.sendMessage(chatId, "هذا الأمر متاح لمشرفي المجموعة فقط.", replyTo);
        return;
      }
      try {
        const url = await this.dashboardUrl(chatId);
        await this.sendMessage(chatId, `لوحة المشرف متاحة لمدة 24 ساعة:\n${url}\n\nلا تشارك هذا الرابط مع الآخرين.`, replyTo);
      } catch (error) {
        logger.warn({ error, chatId }, "Dashboard command failed");
        await this.sendMessage(chatId, "تعذر إنشاء رابط لوحة المشرف حالياً.", replyTo);
      }
      return;
    }
    if (command === "/alerts" || command === "/تنبيهات") {
      if (!isGroup(message)) {
        await this.sendMessage(chatId, "هذا الأمر متاح داخل المجموعات فقط.", replyTo);
        return;
      }
      if (message.from?.id === undefined || !(await this.memberIsAdmin(chatId, message.from.id))) {
        await this.sendMessage(chatId, "هذا الأمر متاح لمشرفي المجموعة فقط.", replyTo);
        return;
      }
      const enabled = ["on", "تشغيل", "نعم", "1"].includes(args.toLowerCase());
      const disabled = ["off", "إيقاف", "لا", "0"].includes(args.toLowerCase());
      if (!enabled && !disabled) {
        await this.sendMessage(chatId, "استخدم /alerts on أو /alerts off.\nلإضافة رابط خارجي استخدم لوحة المشرف.", replyTo);
        return;
      }
      await saveSettings({ alertsEnabled: enabled });
      await this.sendMessage(chatId, enabled ? "تم تشغيل التنبيهات الخارجية." : "تم إيقاف التنبيهات الخارجية.", replyTo);
      return;
    }
    if (isGroup(message)) {
      const reason = await this.moderate(message);
      if (reason === "link") {
        const mention = message.from?.username ? `@${message.from.username}` : firstName;
        await this.sendMessage(chatId, `${mention} ممنوع إرسال الروابط!`);
      }
      return;
    }
    await this.sendMessage(chatId, `قلت: ${text}\n\nأضفني إلى مجموعة وسأساعد في الحماية من الرسائل المزعجة.`, replyTo);
  }

  async start() {
    const bot = await this.api.call<TelegramObject>("getMe");
    this.botId = bot.id;
    await this.api.call("deleteWebhook", { drop_pending_updates: false });
    await this.api.call("setMyCommands", {
      commands: [
        { command: "start", description: "فتح القائمة الرئيسية" },
        { command: "help", description: "عرض المساعدة" },
        { command: "rules", description: "عرض قواعد الحماية" },
        { command: "modstatus", description: "حالة الحماية" },
        { command: "ban", description: "حظر مستخدم بالرد على رسالته" },
        { command: "weather", description: "عرض حالة الطقس" },
        { command: "translate", description: "ترجمة نص" },
        { command: "dashboard", description: "لوحة المشرف" },
        { command: "alerts", description: "إعداد التنبيهات" },
      ],
    });
    logger.info({ username: bot.username, id: bot.id }, "Telegram bot connected");

    let offset = 0;
    while (this.running) {
      try {
        const updates = await this.api.call<TelegramObject[]>("getUpdates", {
          timeout: POLL_TIMEOUT_SECONDS,
          offset: offset + 1,
          allowed_updates: ["message", "callback_query"],
        });
        for (const update of updates ?? []) {
          offset = Math.max(offset, update.update_id);
          try {
            if (update.callback_query) await this.handleCallback(update.callback_query);
            else if (update.message?.photo) await this.checkPhoto(update.message);
            else if (update.message) await this.handleMessage(update.message);
          } catch (error) {
            logger.warn(
              { err: describeError(error), updateId: update.update_id },
              "Telegram update handling failed",
            );
          }
        }
      } catch (error) {
        logger.error({ err: describeError(error) }, "Telegram polling error");
        if (this.running) await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
      }
    }
  }

  stop() {
    this.running = false;
  }
}

export function startTelegramBot(): { stop: () => void } {
  const bot = new TelegramBot();
  void bot.start().catch((error) => {
    logger.error({ error }, "Telegram bot stopped unexpectedly");
  });
  return bot;
}