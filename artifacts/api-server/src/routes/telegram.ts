import { Router, type IRouter } from "express";
import {
  getTelegramStatus,
  startTelegramBot,
} from "../lib/telegram";

const router: IRouter = Router();

router.get("/telegram/status", (_req, res) => {
  res.json(getTelegramStatus());
});

router.post("/telegram/start", async (req, res) => {
  try {
    const bot = await startTelegramBot();
    res.json({
      ok: true,
      bot: bot
        ? {
            id: bot.id,
            username: bot.username ?? null,
            firstName: bot.first_name,
          }
        : null,
    });
  } catch (error) {
    req.log.error({ err: error }, "Unable to start Telegram bot");
    res.status(502).json({
      ok: false,
      error: "Unable to connect to Telegram",
    });
  }
});

export default router;