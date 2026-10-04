# 🚀 APEX on Vercel — ڕێنمایی دامەزراندن

ئارکیتێکچەر:
- **Vercel** (خۆڕایی): ئەپەکە (index.html) + هەموو API ـەکان + بۆتی تێلەگرام (webhook)
- **Neon** (خۆڕایی): داتابەیسی Postgres
- **GitHub Actions** (خۆڕایی): scanner ـی کاتژمێری بۆ سیگناڵەکان

## ١. داتابەیس — Neon (٥ خولەک)
1. لە [neon.tech](https://neon.tech) هەژمار بکەرەوە (خۆڕایی، کارتی بانکی ناوێت)
2. پڕۆژەیەکی نوێ → connection string ـەکە کۆپی بکە (Pooled):
   `postgresql://user:pass@ep-xxx.neon.tech/db?sslmode=require`
3. ئەمە دەبێتە `DATABASE_URL`

## ٢. GitHub
```bash
cd apex-vercel
git init && git add -A && git commit -m "apex vercel"
git remote add origin https://github.com/USERNAME/apex-vercel.git
git push -u origin main
```

## ٣. Vercel
1. [vercel.com](https://vercel.com) → New Project → repo ـەکە هەڵبژێرە
2. Environment Variables:
   - `APEX_BOT_TOKEN` = تۆکەنی بۆتەکە
   - `DATABASE_URL` = connection string ـی Neon
   - `BOT_USERNAME` = ggkurdbot (بێ @)
   - `APEX_WEBAPP_URL` = `https://<app>.vercel.app/`
3. Deploy → لینک: `https://<app>.vercel.app`

## ٤. Webhook ـی بۆت
یەک جار (لە براوسەر یان terminal):
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<app>.vercel.app/api/telegram
```
دەبێت `{"ok":true}` بگەڕێتەوە. (یان بیدە بە من، من دایدەنێم)

## ٥. Scanner — GitHub Actions
1. لە GitHub repo → Settings → Secrets → Actions:
   - `APEX_BOT_TOKEN`
   - `DATABASE_URL`
2. Actions tab → "APEX signal scanner" → چالاکە
3. خۆی هەر کاتژمێرێک scan دەکات (خۆڕایی، لە سنووری ٢٠٠٠ خولەک/مانگ ـدایە)

## ٦. دوگمەی ئەپ لە BotFather
@BotFather → `/setmenubutton` → بۆتەکە → URL: `https://<app>.vercel.app/` → ناو: `🚀 Open App`

## تێبینی سنوورەکان (Hobby خۆڕایی)
- API/webhook: خێرا و بێ سنووری بەرچاو ✅
- Scanner: کاتژمێری (لەبری ١٥ خولەک) — بۆ تایمفرەییمی 1h تەواو باشە ✅
- ئەگەر دەتەوێت scan ـی ١٥ خولەکی: Vercel Pro یان VPS
