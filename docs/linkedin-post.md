# The Watcher — LinkedIn Post

## النسخة العربية

وأنا أشاهد مسلسل YOU، خطرت في بالي فكرة 💭

شو رأيكم لو في بوت يراقب أي حساب على إنستغرام، ويرسلك إشعار فوري على تيليغرام كل ما يصير أي تغيير — حتى لو الشخص بدّل صورة بروفايله بثانية؟

كمهندس أمن سيبراني، فضولي ما تركني… فتحت الـ IDE وبلشت 👨‍💻

اسم المشروع: **The Watcher**

ظنّيت إنه مشروع يومين. طلع رحلة طويلة. كل مرة كنت أحس إني وصلت، كان يطلعلي جدار جديد.

### ⚙️ المشاكل اللي واجهتها (بالترتيب اللي صار فيه):

**0️⃣ كيف ألاقي أصلاً الـ API الداخلي تبع إنستغرام؟**

إنستغرام ما عنده Public API للبروفايلات. حاولت بـ scraping عادي على HTML — يرجّعلي صفحة فاضية لأن كل شي محمّل بـ JavaScript بعد التحميل.

فتحت Burp Suite، ركّبت الـ CA cert، عدّلت الـ proxy في المتصفح، وقعدت أفلتر بمئات الـ requests… لحدّ ما لقيتها:

```
GET /api/v1/users/web_profile_info/?username=<u>
Host: www.instagram.com
x-ig-app-id: 936619743392459
```

هاد الـ endpoint غير موثّق، بس بيرجّع JSON كامل بكل التفاصيل. حسّيت حالي لقيت كنز.

**1️⃣ "ليش يا أخي شغّال من Burp وما شغّال من كودي؟"**

نسخت الـ request بالحرف من Burp إلى curl. النتيجة: 401. نفس headers، نفس كل شي. قعدت ساعات أحاول أفهم. مع كل reload للـ Burp، شغّال 200. مع كل request من curl أو httpx، 401.

اكتشفت إن إنستغرام بيفحص الـ TLS handshake نفسه (JA3/JA4 fingerprint) قبل ما يقرأ headers أصلاً. متصفحي شغّال لأن TLS handshake-ه طبيعي، بس Python's OpenSSL stack له بصمة معروفة وموسومة عند إنستغرام.

اضطريت أتعمّق بـ TLS protocol على مستوى الـ socket. وصلت لمكتبة `curl_cffi` اللي بتعمل impersonation للـ Chrome handshake.

ما اشتغل من أول مرة. جرّبت chrome146 — fail. chrome142 — fail. chrome133a — fail. chrome120 — finally ✅

**2️⃣ "شغّال محلياً، فاشل على السيرفر"**

رفعت الكود على Render، أول request: 401. ما فهمت شي. الكود نفسه، الـ TLS impersonation نفسه. قعدت أفحص لمدة يوم كامل.

اكتشفت إن سيرفر Render في فرانكفورت، و IPs الـ datacenter كلها مسوّمة عند إنستغرام كـ "bot traffic". المشكلة مش بالكود — المشكلة بمكان السيرفر.

**3️⃣ كيف أحلّ مشكلة IP بدون ما أدفع لـ proxy خاص؟**

حلّ تقليدي: تأجير residential proxies — بس $50+ شهرياً، والمفروض البوت يكون مجاني.

الحل اللي عملته: Cloudflare Workers. كتبت Worker بيعمل proxy شفّاف على نفس الـ endpoint، يدوّر بين 6 User Agents، ويعيد المحاولة 8 مرات. Cloudflare ما بتحجبها إنستغرام أبداً + الـ free tier 100,000 طلب يومياً. صفر تكلفة.

**4️⃣ قاعدة البيانات بدت تتضخم بسرعة جنونية**

كنت بحفظ snapshot كل فحص حتى لو ما تغيّر شي. مع 6 حسابات كل 8 ساعات، 21 صف يومياً من بيانات مكرّرة. بأقل من شهرين رح تنفجر قاعدة البيانات المجانية.

الحل: تغيير الـ logic ليحفظ snapshot فقط عند تغيير حقيقي + cron job يومي ينظف الصفوف الأقدم من 30 يوم ويفرّغ raw_response JSONB القديم تلقائياً.

**5️⃣ صور البروفايل وصلت pixelated على Telegram**

هاي كانت الأصعب نفسياً. لأنه ظنّيت إني خلصت المشروع. جرّبت كل شي:

- إرسال الصور كـ document بدل photo (Telegram يضغط الصور) ✅ بس مش كفاية
- استخدام `profile_pic_url_hd` بدل `profile_pic_url` ✅ بس بقي 320px
- جرّبت Instagram mobile API (i.instagram.com) — رجع 200 بس بدون `hd_profile_pic_url_info` ✗
- جرّبت InstaRaider's URL stripping trick — يحذف `/s320x320/` من الـ URL ✅ شغّل لكن جزئياً

آخر شي اكتشفته من الـ logs: إنستغرام يوقّع الـ `stp` parameter في الـ URL بـ HMAC. يعني ما فيك تعدّل الـ URL لتطلب صورة أكبر، لأن الـ signature ما رح تطابق وبيرجّع 403.

في الـ URL في field اسمه `efg`، فكّيت الـ base64 تبعه ولقيت:

```json
{"venc_tag":"profile_pic.django.1080.c2"}
```

يعني الصورة الأصلية محفوظة عند إنستغرام بـ 1080px، بس مش متاحة بدون session cookie. هاي محدودية فيزيائية مش هندسية.

### 🌍 الناتج بعد كل هاد:

بوت يشتغل **24/7 بشكل مجاني بالكامل**:
- Render free tier (السيرفر)
- PostgreSQL مجاني (قاعدة البيانات)
- Cloudflare Workers مجاني (الـ proxy)
- Telegram Bot API (مجاني للأبد)

**تكلفة شهرية: $0**

### 🚀 الخطوة الجاية:

- توسيع البوت ليدعم كل منصات السوشيال ميديا (X, TikTok, إلخ)
- بناء واجهة Frontend بدل الاعتماد فقط على Telegram

أحلى مشاريع حياتي عادةً بتبلش من سؤال بسيط، وبتنتهي وأنا أقرأ RFCs الساعة 3 الصبح 😅

`#cybersecurity` `#python` `#burpsuite` `#automation` `#opensource` `#telegrambot` `#cloudflare` `#fastapi`

---

## English Version

While binge-watching YOU on Netflix, an idea hit me 💭

What if there was a bot that quietly watches any Instagram account and pings you on Telegram the moment anything changes — even a profile picture swap done within a second?

As a cybersecurity engineer, curiosity won. I opened my IDE and got to work 👨‍💻

Project: **The Watcher**

I thought it would take a weekend. It turned into a months-long journey. Every time I thought I was done, a new wall appeared.

### ⚙️ Here's the actual order things broke — and how I fixed them:

**0️⃣ How do I even find Instagram's internal API?**

Instagram has no public profile API. Plain HTML scraping returns an empty page — everything renders client-side. So I fired up Burp Suite, installed the CA cert, configured the browser proxy, and sat there filtering through hundreds of requests until I spotted it:

```
GET /api/v1/users/web_profile_info/?username=<u>
Host: www.instagram.com
x-ig-app-id: 936619743392459
```

Undocumented, but returns the full profile JSON. Felt like finding treasure.

**1️⃣ "Why does it work in Burp but not in my code?"**

I copied the request byte-for-byte from Burp into curl. Result: 401. Same headers. Same everything. I spent hours debugging. Burp kept returning 200. curl and Python httpx kept returning 401.

That's when I learned: Instagram inspects the TLS handshake itself (JA3/JA4 fingerprint) BEFORE it even reads the HTTP headers. My browser worked because its TLS handshake looks normal. Python's OpenSSL stack has a known, fingerprinted signature that Instagram blocks.

I had to dive deep into the TLS protocol at the socket level. Eventually found `curl_cffi`, a library that replays Chrome's exact TLS ClientHello.

It didn't work on the first try either. chrome146 — fail. chrome142 — fail. chrome133a — fail. chrome120 — finally ✅

**2️⃣ "Works on my machine, dies on the server"**

Pushed to Render, first request: 401. Same code, same TLS impersonation. I debugged for an entire day.

Turns out Render's Frankfurt datacenter IPs are flagged across the board by Instagram as bot traffic. The problem wasn't the code — it was geography.

**3️⃣ How do I fix an IP problem without paying for proxies?**

Traditional fix: residential proxy services — $50+/month. But the whole point was that this had to be free.

What I built instead: a Cloudflare Worker as a transparent proxy on the exact same endpoint, rotating across 6 user agents with 8 retries each. Instagram never blocks Cloudflare's edge IPs, and the free tier gives 100,000 requests/day. Zero cost.

**4️⃣ The database was bloating insanely fast**

I was inserting a snapshot on every single check, even when nothing changed. With 6 accounts on an 8-hour cycle, that's 21 rows of duplicate data per day. Less than two months until the free DB tier fills up.

Fix: only insert a snapshot when something actually changed + a daily cron at 03:00 UTC that purges anything older than 30 days and nulls out old `raw_response` JSONB columns.

**5️⃣ Profile pictures came through pixelated**

This one broke me mentally. I thought I was DONE. Tried everything:

- `send_document` instead of `send_photo` (Telegram compresses photos) ✅ helped, not enough
- Used `profile_pic_url_hd` instead of `profile_pic_url` ✅ still 320px
- Tried Instagram's mobile API (`i.instagram.com`) — returned 200 but no `hd_profile_pic_url_info` ✗
- Tried InstaRaider's CDN URL-stripping trick — strips `/s320x320/` from the URL ✅ partially worked

Final discovery from the logs: Instagram signs the `stp` parameter in the CDN URL with HMAC. You can't modify the URL to request a larger image because the signature breaks → 403.

Inside the URL there's a field called `efg`. Base64-decoded it:

```json
{"venc_tag":"profile_pic.django.1080.c2"}
```

So Instagram has the original 1080px version stored — but it's gated behind a session cookie. Hard limit, not an engineering one.

### 🌍 What I ended up with:

A bot running **24/7, completely free**:
- Render free tier (server)
- PostgreSQL free tier (database)
- Cloudflare Workers free tier (proxy)
- Telegram Bot API (free, always)

**Monthly cost: $0**

### 🚀 What's next:

- Extending it to every social media platform (X, TikTok, etc.)
- Building a proper frontend instead of Telegram-only

My favorite projects always start as a simple question and end with me reading RFCs at 3 AM 😅

`#cybersecurity` `#python` `#burpsuite` `#automation` `#opensource` `#telegrambot` `#cloudflare` `#fastapi`
