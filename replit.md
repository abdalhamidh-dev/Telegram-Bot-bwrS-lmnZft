# Telegram Bot

A Python Telegram group bot that connects securely to Telegram and removes obvious spam while keeping group administration in the hands of human admins.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
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

- `group-bot/bot.py` — Python polling loop, group moderation rules, and command handlers.
- `group-bot/telegram_bridge.mjs` — small authenticated connector bridge used by the Python runtime.
- `artifacts/api-server/src/routes/health.ts` — API health check.

## Architecture decisions

- Telegram calls use the Replit-managed Telegram connection; bot credentials never enter application code or chat.
- Moderation logic runs in Python, while the bridge keeps the authenticated Replit connector in a local Node process because the Python connector package is unavailable in this runtime.
- The bot uses long polling so it works immediately without requiring a public webhook URL.
- Update offsets and moderation caches are held in memory; persistent state can be added for larger groups.

## Product

- Responds to `/start`, `/help`, `/rules`, `/modstatus`, and reply-based `/ban`.
- Removes links, obvious scam messages, repeated flood messages, and aggressive all-caps spam when it has Telegram delete permission.
- Scans group photos with Sightengine `nudity-2.0` and removes images when `sexual_display` or `erotica` exceeds 50%.
- Lets group administrators ban a user by replying to that user's message with `/ban`.
- Never moderates group admins.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Only one polling loop should run against a Telegram bot token at a time.
- Telegram long polling and webhooks are mutually exclusive; this starter intentionally uses polling.
- To moderate every group message, disable Group Privacy for the bot through BotFather and make the bot a group administrator with permission to delete messages.
- Image scanning needs the `SIGHTENGINE_SECRET` and `TELEGRAM_BOT_TOKEN` secrets; both are stored outside the source code.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
