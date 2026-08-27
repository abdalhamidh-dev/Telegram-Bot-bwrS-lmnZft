# Telegram Bot

A Telegram bot starter that connects securely to Telegram, responds to core commands, and provides a clean foundation for custom workflows.

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

- `artifacts/api-server/src/lib/telegram.ts` — Telegram client, polling loop, command handlers, and bot status.
- `artifacts/api-server/src/routes/telegram.ts` — status and manual start endpoints.
- `artifacts/api-server/src/routes/health.ts` — API health check.

## Architecture decisions

- Telegram calls use the Replit-managed Telegram connection; bot credentials never enter application code or chat.
- The starter uses long polling so it works immediately in the development workflow without requiring a public webhook URL.
- Update offsets are held in memory for the starter build; persistent state can be added when the bot's product workflow is defined.

## Product

- Responds to `/start`, `/help`, and `/about`.
- Echoes non-command text with a prompt for the next custom workflow.
- Exposes bot connection status at `/api/telegram/status`.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Only one polling loop should run against a Telegram bot token at a time.
- Telegram long polling and webhooks are mutually exclusive; this starter intentionally uses polling.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
