import { createHash, randomBytes } from "node:crypto";
import { Router, type IRouter, type NextFunction, type Request, type Response } from "express";
import { desc, eq, lt } from "drizzle-orm";
import {
  dashboardTokens,
  db,
  groupSettings,
  moderationEvents,
} from "@workspace/db";
import { logger } from "../lib/logger";

const router: IRouter = Router();
const DASHBOARD_TOKEN_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_TRANSLATION_LENGTH = 500;

function requireBotSecret(
  req: Request,
  res: Response,
  next: NextFunction,
): void {
  const configuredSecret = process.env["SESSION_SECRET"];
  const suppliedSecret = req.header("x-bot-secret");
  if (!configuredSecret || suppliedSecret !== configuredSecret) {
    res.status(401).json({ error: "Unauthorized bot request" });
    return;
  }
  next();
}

function parseChatId(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const raw = Array.isArray(value) ? value[0] : value;
  const parsed = typeof raw === "number" ? raw : Number(raw);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function parseBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function validatedWebhookUrl(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null || value === "") return null;
  if (typeof value !== "string" || value.length > 2000) {
    throw new Error("Webhook URL is invalid");
  }
  const url = new URL(value);
  if (url.protocol !== "https:") {
    throw new Error("Webhook URL must use HTTPS");
  }
  return url.toString();
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

async function fetchJson(url: string): Promise<Record<string, any>> {
  const response = await fetch(url, {
    signal: AbortSignal.timeout(10_000),
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`External service returned ${response.status}`);
  }
  return (await response.json()) as Record<string, any>;
}

async function sendExternalAlert(chatId: number, payload: Record<string, unknown>) {
  const settings = await getSettings(chatId);
  if (!settings?.alertsEnabled || !settings.alertWebhookUrl) return;

  try {
    const response = await fetch(settings.alertWebhookUrl, {
      method: "POST",
      signal: AbortSignal.timeout(5_000),
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        source: "telegram-group-bot",
        chat_id: chatId,
        ...payload,
      }),
    });
    if (!response.ok) {
      logger.warn(
        { chatId, status: response.status },
        "External alert webhook returned an error",
      );
    }
  } catch (error) {
    logger.warn({ chatId, error }, "External alert webhook failed");
  }
}

router.post("/bot/settings", requireBotSecret, async (req, res) => {
  const chatId = parseChatId(req.body?.chatId);
  if (chatId === null) {
    res.status(400).json({ error: "chatId must be a safe integer" });
    return;
  }

  try {
    const existing = await ensureSettings(chatId, req.body?.title);
    const webhookUrl = validatedWebhookUrl(req.body?.alertWebhookUrl);
    const updated = await db
      .update(groupSettings)
      .set({
        title: typeof req.body?.title === "string" ? req.body.title.slice(0, 255) : existing.title,
        translationEnabled: parseBoolean(
          req.body?.translationEnabled,
          existing.translationEnabled,
        ),
        alertsEnabled: parseBoolean(req.body?.alertsEnabled, existing.alertsEnabled),
        weatherLocation:
          typeof req.body?.weatherLocation === "string"
            ? req.body.weatherLocation.slice(0, 120)
            : existing.weatherLocation,
        ...(webhookUrl !== undefined ? { alertWebhookUrl: webhookUrl } : {}),
        updatedAt: new Date(),
      })
      .where(eq(groupSettings.chatId, chatId))
      .returning();
    res.json(updated[0]);
  } catch (error) {
    res.status(400).json({ error: error instanceof Error ? error.message : "Invalid settings" });
  }
});

router.get("/bot/settings", requireBotSecret, async (req, res) => {
  const chatId = parseChatId(req.query["chat_id"]);
  if (chatId === null) {
    res.status(400).json({ error: "chat_id must be a safe integer" });
    return;
  }
  res.json(await ensureSettings(chatId));
});

router.post("/bot/events", requireBotSecret, async (req, res) => {
  const chatId = parseChatId(req.body?.chatId);
  const eventType = typeof req.body?.eventType === "string"
    ? req.body.eventType.slice(0, 40)
    : "";
  if (chatId === null || !/^[a-z0-9_]+$/i.test(eventType)) {
    res.status(400).json({ error: "chatId and eventType are required" });
    return;
  }

  const event = await db
    .insert(moderationEvents)
    .values({
      chatId,
      messageId: parseChatId(req.body?.messageId),
      userId: parseChatId(req.body?.userId),
      username: typeof req.body?.username === "string" ? req.body.username.slice(0, 255) : null,
      eventType,
      details: req.body?.details && typeof req.body.details === "object" ? req.body.details : null,
    })
    .returning();

  void sendExternalAlert(chatId, {
    event_type: eventType,
    message_id: req.body?.messageId ?? null,
    user_id: req.body?.userId ?? null,
    username: req.body?.username ?? null,
    details: req.body?.details ?? null,
    created_at: event[0]?.createdAt?.toISOString() ?? new Date().toISOString(),
  });
  res.status(201).json(event[0]);
});

router.post("/bot/dashboard-token", requireBotSecret, async (req, res) => {
  const chatId = parseChatId(req.body?.chatId);
  if (chatId === null) {
    res.status(400).json({ error: "chatId must be a safe integer" });
    return;
  }

  const rawToken = randomBytes(32).toString("base64url");
  const tokenHash = createHash("sha256").update(rawToken).digest("hex");
  await db.delete(dashboardTokens).where(lt(dashboardTokens.expiresAt, new Date()));
  await db.insert(dashboardTokens).values({
    tokenHash,
    chatId,
    expiresAt: new Date(Date.now() + DASHBOARD_TOKEN_TTL_MS),
  });

  const configuredBase = process.env["DASHBOARD_PUBLIC_URL"]?.replace(/\/$/, "");
  const domain = process.env["REPLIT_DEV_DOMAIN"];
  const baseUrl = configuredBase ?? (domain ? `https://${domain}` : `http://${req.get("host")}`);
  res.json({
    dashboardUrl: `${baseUrl}/api/dashboard?token=${encodeURIComponent(rawToken)}`,
    expiresInHours: 24,
  });
});

router.get("/bot/weather", requireBotSecret, async (req, res) => {
  const location = String(req.query["location"] ?? "Cairo").trim().slice(0, 120);
  const language = req.query["language"] === "en" ? "en" : "ar";
  if (!location) {
    res.status(400).json({ error: "location is required" });
    return;
  }

  try {
    const geoUrl = new URL("https://geocoding-api.open-meteo.com/v1/search");
    geoUrl.search = new URLSearchParams({
      name: location,
      count: "1",
      language,
      format: "json",
    }).toString();
    const geocoding = await fetchJson(geoUrl.toString());
    const place = geocoding.results?.[0];
    if (!place) {
      res.status(404).json({ error: "Location not found" });
      return;
    }

    const forecastUrl = new URL("https://api.open-meteo.com/v1/forecast");
    forecastUrl.search = new URLSearchParams({
      latitude: String(place.latitude),
      longitude: String(place.longitude),
      current: "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
      timezone: "auto",
    }).toString();
    const forecast = await fetchJson(forecastUrl.toString());
    const current = forecast.current ?? {};
    res.json({
      location: place.name,
      country: place.country,
      temperature: current.temperature_2m,
      apparentTemperature: current.apparent_temperature,
      humidity: current.relative_humidity_2m,
      windSpeed: current.wind_speed_10m,
      unit: forecast.current_units?.temperature_2m ?? "°C",
      description: weatherDescription(Number(current.weather_code), language),
      timezone: forecast.timezone,
    });
  } catch (error) {
    logger.warn({ error }, "Weather lookup failed");
    res.status(502).json({ error: "Weather service is temporarily unavailable" });
  }
});

router.post("/bot/translate", requireBotSecret, async (req, res) => {
  const text = typeof req.body?.text === "string" ? req.body.text.trim() : "";
  const target = typeof req.body?.target === "string" ? req.body.target.toLowerCase() : "ar";
  if (!text || text.length > MAX_TRANSLATION_LENGTH || !/^[a-z]{2}$/.test(target)) {
    res.status(400).json({ error: "text and a two-letter target language are required" });
    return;
  }

  try {
    const url = new URL("https://api.mymemory.translated.net/get");
    url.search = new URLSearchParams({
      q: text,
      langpair: `auto|${target}`,
    }).toString();
    const result = await fetchJson(url.toString());
    const translatedText = result.responseData?.translatedText;
    if (typeof translatedText !== "string" || !translatedText.trim()) {
      res.status(502).json({ error: "Translation service returned no translation" });
      return;
    }
    res.json({ translatedText, target });
  } catch (error) {
    logger.warn({ error }, "Translation lookup failed");
    res.status(502).json({ error: "Translation service is temporarily unavailable" });
  }
});

export default router;