import app from "./app";
import { logger } from "./lib/logger";
import { pool } from "@workspace/db";
import { startTelegramBot } from "./telegram-bot";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

const server = app.listen(port, () => {
  logger.info({ port }, "Dashboard/API server listening");
});

const telegramBot = startTelegramBot();

const shutdown = (signal: string) => {
  logger.info({ signal }, "Shutting down API and Telegram services");
  telegramBot.stop();
  server.close(() => {
    void pool.end().finally(() => process.exit(0));
  });
};

process.once("SIGTERM", () => shutdown("SIGTERM"));
process.once("SIGINT", () => shutdown("SIGINT"));

server.on("error", (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }
});
