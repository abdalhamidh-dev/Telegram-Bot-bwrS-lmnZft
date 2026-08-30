import {
  bigint,
  boolean,
  integer,
  jsonb,
  pgTable,
  serial,
  text,
  timestamp,
  varchar,
} from "drizzle-orm/pg-core";

export const groupSettings = pgTable("group_settings", {
  chatId: bigint("chat_id", { mode: "number" }).primaryKey(),
  title: text("title"),
  language: varchar("language", { length: 10 }).notNull().default("ar"),
  translationEnabled: boolean("translation_enabled").notNull().default(true),
  alertsEnabled: boolean("alerts_enabled").notNull().default(true),
  weatherLocation: text("weather_location"),
  alertWebhookUrl: text("alert_webhook_url"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

export const moderationEvents = pgTable("moderation_events", {
  id: serial("id").primaryKey(),
  chatId: bigint("chat_id", { mode: "number" }).notNull(),
  messageId: bigint("message_id", { mode: "number" }),
  userId: bigint("user_id", { mode: "number" }),
  username: text("username"),
  eventType: varchar("event_type", { length: 40 }).notNull(),
  details: jsonb("details"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

export const dashboardTokens = pgTable("dashboard_tokens", {
  tokenHash: varchar("token_hash", { length: 64 }).primaryKey(),
  chatId: bigint("chat_id", { mode: "number" }).notNull(),
  expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});