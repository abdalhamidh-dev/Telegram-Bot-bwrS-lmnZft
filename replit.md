# Telegram Bot

A TypeScript/Node.js Telegram group bot that connects securely to Telegram and removes obvious spam while keeping group administration in the hands of human admins.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server, dashboard, and Telegram bot together on port 8080
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `artifacts/api-server/src/index.ts` — single entry point that starts Express and Telegram polling together.
- `artifacts/api-server/src/telegram-bot.ts` — Node.js Telegram polling loop, moderation rules, and command handlers.
- `artifacts/api-server/src/routes/health.ts` — API health check.
- `artifacts/api-server/src/routes/bot.ts` — database-backed bot settings/events, weather, translation, alerts, and dashboard-token APIs.
- `artifacts/api-server/src/routes/dashboard.ts` — time-limited Arabic admin dashboard.
- `lib/db/src/schema/index.ts` — group settings, moderation events, and dashboard token tables.

## Architecture decisions

- Telegram calls use the Replit-managed Telegram connection; bot credentials never enter application code or chat.
- Moderation logic and Telegram polling run in TypeScript/Node.js in the same process as the dashboard API.
- The Node process uses the Replit-managed Telegram connector directly; no Python runtime or subprocess bridge is required.
- The bot uses long polling so it works immediately without requiring a public webhook URL.
- Update offsets and moderation caches are held in memory; group settings and moderation events are persisted in PostgreSQL.
- Internal bot endpoints use `SESSION_SECRET`; the secret is never logged or placed in Telegram messages.
- Weather uses Open-Meteo without an API key. Translation uses MyMemory without an API key. External alerts use an administrator-configured HTTPS webhook.

## Product

- Responds to `/start`, `/help`, `/rules`, `/modstatus`, and reply-based `/ban`.
- Removes links, obvious scam messages, repeated flood messages, and aggressive all-caps spam when it has Telegram delete permission.
- Scans group photos with Sightengine `nudity-2.0` and removes images when `sexual_display` or `erotica` exceeds 50%.
- Lets group administrators ban a user by replying to that user's message with `/ban`.
- Supports `/weather Cairo` or `/طقس القاهرة` using live weather data.
- Supports `/translate en مرحباً` or `/ترجم en مرحباً`, including translating a replied-to message.
- Lets group administrators request a 24-hour dashboard link with `/dashboard` or `/لوحة`.
- Stores moderation events and supports external webhook alerts controlled by `/alerts on|off` or the dashboard.
- Never moderates group admins.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Only one polling loop should run against a Telegram bot token at a time.
- Telegram long polling and webhooks are mutually exclusive; this starter intentionally uses polling.
- To moderate every group message, disable Group Privacy for the bot through BotFather and make the bot a group administrator with permission to delete messages.
- Image scanning needs the `SIGHTENGINE_SECRET` and `TELEGRAM_BOT_TOKEN` secrets; both are stored outside the source code.
- Bot-to-API features need `SESSION_SECRET`; it is already available as a secure environment secret.
- The dashboard link is intentionally time-limited. Configure `DASHBOARD_PUBLIC_URL` if the bot should send a custom public base URL; otherwise it uses the Replit development domain.
- Deployment is configured as an always-on VM because Telegram long polling must remain active while the dashboard is served.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
