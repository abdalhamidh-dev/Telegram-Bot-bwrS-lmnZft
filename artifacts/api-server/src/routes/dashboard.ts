import { createHash } from "node:crypto";
import { Router, type IRouter, type Request, type Response } from "express";
import { desc, eq } from "drizzle-orm";
import { dashboardTokens, db, groupSettings, moderationEvents } from "@workspace/db";

const router: IRouter = Router();

function tokenFromRequest(req: Request): string | null {
  const queryToken = req.query["token"];
  const headerToken = req.header("x-dashboard-token");
  const token = Array.isArray(queryToken) ? queryToken[0] : queryToken;
  return typeof token === "string" ? token : headerToken ?? null;
}

async function chatIdForToken(req: Request): Promise<number | null> {
  const token = tokenFromRequest(req);
  if (!token) return null;
  const tokenHash = createHash("sha256").update(token).digest("hex");
  const rows = await db
    .select()
    .from(dashboardTokens)
    .where(eq(dashboardTokens.tokenHash, tokenHash))
    .limit(1);
  const row = rows[0];
  if (!row || row.expiresAt.getTime() <= Date.now()) return null;
  return row.chatId;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function dashboardUrl(req: Request): string {
  const token = tokenFromRequest(req) ?? "";
  return `/api/dashboard?token=${encodeURIComponent(token)}`;
}

function dashboardSettingsUrl(req: Request): string {
  const token = tokenFromRequest(req) ?? "";
  return `/api/dashboard/settings?token=${encodeURIComponent(token)}`;
}

router.get("/dashboard", async (req, res) => {
  const chatId = await chatIdForToken(req);
  if (chatId === null) {
    res.status(401).send("<h1>رابط لوحة التحكم غير صالح أو منتهي الصلاحية</h1>");
    return;
  }

  const [settingsRows, events] = await Promise.all([
    db.select().from(groupSettings).where(eq(groupSettings.chatId, chatId)).limit(1),
    db
      .select()
      .from(moderationEvents)
      .where(eq(moderationEvents.chatId, chatId))
      .orderBy(desc(moderationEvents.createdAt))
      .limit(50),
  ]);
  const settings = settingsRows[0];
  const counts = events.reduce<Record<string, number>>((result, event) => {
    result[event.eventType] = (result[event.eventType] ?? 0) + 1;
    return result;
  }, {});
  const saved = req.query["saved"] === "1";
  const eventRows = events
    .map(
      (event) => `
        <tr>
          <td>${escapeHtml(event.eventType)}</td>
          <td>${escapeHtml(event.username ? `@${event.username}` : event.userId ?? "—")}</td>
          <td>${escapeHtml(event.createdAt.toLocaleString("ar-EG"))}</td>
          <td>${escapeHtml(JSON.stringify(event.details ?? {}))}</td>
        </tr>`,
    )
    .join("");
  const cards = Object.entries(counts)
    .map(([name, count]) => `<div class="metric"><strong>${count}</strong><span>${escapeHtml(name)}</span></div>`)
    .join("");

  res.type("html").send(`<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>لوحة حماية المجموعة</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, system-ui, sans-serif; background: #0b1220; color: #e5edf8; }
    body { margin: 0; background: linear-gradient(135deg,#0b1220,#111c33); min-height: 100vh; }
    main { max-width: 1100px; margin: auto; padding: 32px 20px 60px; }
    h1 { margin-bottom: 4px; } .muted { color: #9aacbf; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 16px; margin: 22px 0; }
    .panel, .metric { background: #14213a; border: 1px solid #263a5b; border-radius: 16px; padding: 18px; box-shadow: 0 10px 30px #0002; }
    .metric strong { display:block; font-size: 30px; color:#61dafb; } .metric span { color:#a9bad0; }
    form { display:grid; gap: 14px; } label { display:flex; gap:10px; align-items:center; }
    input[type=text] { width:100%; box-sizing:border-box; padding:11px; border-radius:9px; border:1px solid #385173; background:#0c172a; color:#fff; }
    button { cursor:pointer; border:0; border-radius:9px; padding:11px 16px; background:#36a3f7; color:#06111f; font-weight:700; }
    .notice { padding: 10px 12px; border-radius: 9px; background:#174c3b; color:#b7f4d6; margin: 16px 0; }
    .table-wrap { overflow:auto; } table { width:100%; border-collapse:collapse; min-width:650px; } th,td { text-align:right; padding:11px 8px; border-bottom:1px solid #263a5b; vertical-align:top; } td:last-child { direction:ltr; text-align:left; font-family:monospace; font-size:12px; }
  </style>
</head>
<body><main>
  <h1>لوحة حماية المجموعة</h1>
  <div class="muted">المجموعة: ${escapeHtml(settings?.title ?? chatId)} · المعرّف: ${chatId}</div>
  ${saved ? '<div class="notice">تم حفظ الإعدادات بنجاح.</div>' : ""}
  <section class="grid">
    <div class="metric"><strong>${events.length}</strong><span>آخر الأحداث المسجلة</span></div>
    ${cards || '<div class="metric"><strong>0</strong><span>لا توجد أحداث بعد</span></div>'}
  </section>
  <section class="panel">
    <h2>إعدادات المجموعة</h2>
    <form method="post" action="${dashboardSettingsUrl(req)}">
      <label><input type="checkbox" name="translationEnabled" ${settings?.translationEnabled ? "checked" : ""}> تفعيل الترجمة</label>
      <label><input type="checkbox" name="alertsEnabled" ${settings?.alertsEnabled ? "checked" : ""}> تفعيل التنبيهات الخارجية</label>
      <label>موقع الطقس<input type="text" name="weatherLocation" value="${escapeHtml(settings?.weatherLocation ?? "Cairo")}" placeholder="Cairo"></label>
      <label>رابط Webhook للتنبيهات<input type="text" name="alertWebhookUrl" value="${escapeHtml(settings?.alertWebhookUrl ?? "")}" placeholder="https://example.com/webhook"></label>
      <button type="submit">حفظ الإعدادات</button>
    </form>
  </section>
  <section class="panel" style="margin-top:18px">
    <h2>آخر الأحداث</h2>
    <div class="table-wrap"><table><thead><tr><th>النوع</th><th>المستخدم</th><th>الوقت</th><th>التفاصيل</th></tr></thead><tbody>${eventRows || '<tr><td colspan="4">لا توجد أحداث مسجلة بعد.</td></tr>'}</tbody></table></div>
  </section>
</main></body></html>`);
});

router.post("/dashboard/settings", async (req, res) => {
  const chatId = await chatIdForToken(req);
  if (chatId === null) {
    res.status(401).send("Unauthorized");
    return;
  }
  const alertWebhookUrl =
    typeof req.body?.alertWebhookUrl === "string" && req.body.alertWebhookUrl.trim()
      ? req.body.alertWebhookUrl.trim()
      : null;
  if (alertWebhookUrl) {
    try {
      const parsed = new URL(alertWebhookUrl);
      if (parsed.protocol !== "https:") throw new Error();
    } catch {
      res.status(400).send("Webhook URL must use HTTPS.");
      return;
    }
  }
  await db
    .update(groupSettings)
    .set({
      translationEnabled: req.body?.translationEnabled === "on",
      alertsEnabled: req.body?.alertsEnabled === "on",
      weatherLocation:
        typeof req.body?.weatherLocation === "string"
          ? req.body.weatherLocation.trim().slice(0, 120)
          : null,
      alertWebhookUrl,
      updatedAt: new Date(),
    })
    .where(eq(groupSettings.chatId, chatId));
  res.redirect(`${dashboardUrl(req)}&saved=1`);
});

export default router;