-- scripts/init_db.sql
-- Creates tables required by the Telegram group moderation bot

-- جدول إعدادات المجموعات
CREATE TABLE IF NOT EXISTS group_settings (
  chat_id bigint PRIMARY KEY,
  title text,
  language varchar(10) NOT NULL DEFAULT 'ar',
  translation_enabled boolean NOT NULL DEFAULT true,
  alerts_enabled boolean NOT NULL DEFAULT true,
  weather_location text,
  alert_webhook_url text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- جدول أحداث الموديريشن
CREATE TABLE IF NOT EXISTS moderation_events (
  id serial PRIMARY KEY,
  chat_id bigint NOT NULL,
  message_id bigint,
  user_id bigint,
  username text,
  event_type varchar(40) NOT NULL,
  details jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- جدول توكنات لوحة التحكم
CREATE TABLE IF NOT EXISTS dashboard_tokens (
  token_hash varchar(64) PRIMARY KEY,
  chat_id bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
