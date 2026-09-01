# بوت حماية مجموعات تيليجرام — README

هذا المستودع يحتوي على بوت حماية لمجموعات تيليجرام (بورصة المنظفات). البوت مكتوب بـ TypeScript ويعمل مع قاعدة بيانات PostgreSQL ويُصمَّم للاستخدام في Replit باستخدام Replit Connectors.

ملخص سريع
- ملف البوت الرئيسي: `artifacts/api-server/src/telegram-bot.ts`
- سكربت إنشاء الجداول موجود: `scripts/init_db.sql`
- مثال متغيرات البيئة: `env.example`
- المشروع مُهيأ للعمل على Replit (انظر `.replit`).

المتطلبات الأساسية
1. حساب Replit (لتشغيل البوت بسهولة).  
2. حساب Supabase (أو أي قاعدة PostgreSQL) للحصول على `DATABASE_URL`.  
3. بوت تيليجرام من @BotFather للحصول على `TELEGRAM_BOT_TOKEN`.

إعداد Supabase (للمستخدمين الجدد)
1. افتح https://supabase.com وسجّل دخولك أو أنشئ حسابًا جديدًا.  
2. أنشئ Project جديد واختر خطة Free إن رغبت.  
3. بعد إنشاء المشروع: اذهب إلى Settings → Database → Connection string. انسخ القيمة (تبدأ بـ `postgres://`) — هذه هي قيمة `DATABASE_URL`.

إضافة المتغيرات البيئية في Replit
1. افتح مشروعك في Replit → Settings → Secrets (Environment variables).  
2. أضف القيم التالية (أسماء المتغيرات بالضبط كما أدناه):
   - `DATABASE_URL` = connection string من Supabase
   - `TELEGRAM_BOT_TOKEN` = توكن البوت من @BotFather
   - `USE_REPLIT_CONNECTORS` = `true`
   - `SIGHTENGINE_SECRET` = (اختياري) إذا تريد فحص الصور
   - `DASHBOARD_PUBLIC_URL` = (اختياري)
   - `LOG_LEVEL` = `debug` أو `info` (يفيد أثناء التطوير)

إنشاء الجداول (خياران)
- الخيار الموصى به (سهل): شغّل SQL الموجود مباشرة في Supabase
  1. افتح Supabase → Project → SQL Editor → New query.  
  2. افتح الملف `scripts/init_db.sql` في المستودع، انسخ محتواه وألصقه في محرّر SQL في Supabase ثم اضغط Run.  
  3. تحقق من وجود الجداول `group_settings`, `moderation_events`, `dashboard_tokens` في Table Editor.

- بديل: تشغيل مهاجرات Drizzle من Replit
  في حال رغبت استخدام Drizzle migrations يمكنك تنفيذ داخل Replit Terminal:

  ```bash
  pnpm install
  pnpm --filter @workspace/db run push
  ```

تشغيل المشروع على Replit
1. بعد ضبط Secrets وإعداد الجداول: في Replit اضغط Run أو من الـ Shell شغّل:

```bash
pnpm install
pnpm --filter @workspace/api-server run dev
```

2. راقب Console/Logs — ينبغي أن ترى رسائل مثل:
   - `Dashboard/API server listening`  
   - `Telegram bot connected` (حول اسم البوت و id)

اختبار البوت
- في Telegram أرسل للبوت `/start` في محادثة خاصة.  
- أضف البوت إلى مجموعة وامنحه صلاحيات مشرف (Delete Messages, Ban Members) ثم جرّب الأمر `/modstatus` داخل المجموعة.

مشاكل شائعة وحلول سريعة
- `DATABASE_URL must be set`: تأكد أنك أضفت SECRET باسم `DATABASE_URL` في Replit وأنه صحيح.  
- Drizzle push يفشل: تأكد من أن connection string صحيح وأن قاعدة البيانات تعمل. بإمكانك استخدام السكربت SQL في `scripts/init_db.sql` كبديل.  
- `TELEGRAM_BOT_TOKEN is not configured`: ضع توكن بوت تيليجرام في Secrets باسم `TELEGRAM_BOT_TOKEN`.  
- البوت لا يملك صلاحيات حذف الرسائل: أضفه كمشرف في المجموعة وفعل صلاحية حذف الرسائل.

نصائح أمنية
- لا تُشارك `TELEGRAM_BOT_TOKEN` أو `DATABASE_URL` علنًا. احفظهم كـ Secrets.  
- إن نشرت اللوجات هنا، احرص على حذف أي قيم حساسة منها قبل المشاركة.

تشغيل تجريبي بدون قاعدة بيانات (اختياري)
- إذا أردت اختبار سلوك بعض الأوامر دون قاعدة بيانات، أستطيع إضافة وضع "mock" يكتب الإعدادات مؤقتًا إلى ملف JSON محلي. هذا مفيد للاختبار لكنه لا يحفظ البيانات بين إعادة التشغيل.

ماذا أفعل بعدًا
- إن رغبت، أستطيع الآن:  
  1) إضافة مثال تشغيل محلي (mock storage) للاختبار بدون DB.  
  2) إضافة توجيهات مصوّرة خطوة‑بخطوة داخل README.  
  3) تعديل الكود لإزالة استثناءات Sightengine عندما لا يكون `SIGHTENGINE_SECRET` مضبوطًا.

إذا احتجت أي مساعدة إضافية — انسخ هنا أي خطأ من Console وسأصلحه فورًا.
