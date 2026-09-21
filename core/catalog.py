"""The message catalogue: every user-facing line in English and Persian.

Layout rule, and the whole reason this is one file: a key's two translations sit on
adjacent lines. A translator can compare them without opening anything else, a
reviewer sees a missing Persian sentence in the diff, and ``tests/test_i18n.py``
enforces the rest (both languages present, identical ``{placeholders}``).

Placeholders are ``str.format`` fields named for what they hold — ``{name}``,
``{used}``, ``{limit}`` — never positional. Values that arrive from a user, a title
or a URL are escaped by the caller (``core.utils.escape_html``); the catalogue
carries markup, not data.

Error codes (``err.<CODE>``) are keys like any other, but :func:`core.i18n.error_message`
treats a missing one as "say what the engine said" rather than as a bug: the engine
names failures for the operator, and that list can grow before this file does.
"""

from __future__ import annotations

from typing import Final

#: ``key → {language → template}``. Deliberately plain: the machinery lives in
#: :mod:`core.i18n`, and this module must stay importable by it (no cycle).
Catalog = dict[str, dict[str, str]]

MESSAGES: Final[Catalog] = {
    # ---------------------------------------------------------------------
    # Start screen, menu, language
    # ---------------------------------------------------------------------
    "start.welcome": {
        "en": (
            "<b>Hi {name} 👋</b>\n"
            "\n"
            "🎬 <b>Premium downloader</b> — send a link, the rest is on me:\n"
            "▫️ YouTube • Twitter/X • Instagram • TikTok\n"
            "▫️ Spotify • SoundCloud • Facebook • Reddit\n"
            "▫️ Pinterest • Vimeo • Twitch • Dailymotion\n"
            "▫️ and dozens of other sites\n"
            "\n"
            "⚙️ Downloads run in the background and the file lands <b>right here</b>: you "
            "follow the progress in the same message, and a link that cannot be downloaded "
            "says why instead of failing silently.\n"
            "\n"
            "👇 Start with the buttons below:"
        ),
        "fa": (
            "<b>سلام {name} 👋</b>\n"
            "\n"
            "🎬 <b>دانلودر حرفه‌ای</b> — فقط لینک را بفرست، بقیه‌اش با من:\n"
            "▫️ یوتیوب • توییتر/X • اینستاگرام • تیک‌تاک\n"
            "▫️ اسپاتیفای • ساندکلاود • فیسبوک • ریدیت\n"
            "▫️ پینترست • ویمیو • توییچ • دیلی‌موشن\n"
            "▫️ و ده‌ها سرویس دیگر\n"
            "\n"
            "⚙️ دانلود در پس‌زمینه انجام می‌شود و فایل <b>همین‌جا</b> برایت ارسال می‌شود؛ "
            "پیشرفت را در همان پیام می‌بینی، و اگر لینکی دانلود نشد علتش گفته می‌شود.\n"
            "\n"
            "👇 از دکمه‌های زیر شروع کن:"
        ),
    },
    "menu.profile": {"en": "👤 My profile", "fa": "👤 پروفایل من"},
    "menu.premium": {"en": "💎 Go VIP", "fa": "💎 ارتقا به ویژه (VIP)"},
    "menu.help": {"en": "❓ Help", "fa": "❓ راهنما"},
    "menu.language": {"en": "🌐 Language", "fa": "🌐 زبان"},
    # The one menu button an operator fills in: its target lives in the database.
    "menu.support": {"en": "💬 Support", "fa": "💬 پشتیبانی"},
    "menu.back": {"en": "🔙 Back", "fa": "🔙 بازگشت"},
    "support.text": {
        "en": "💬 <b>Support</b>\n\nQuestions, a payment that needs a look, or a "
        "download that keeps failing? Write to {contact} — the operator reads it.",
        "fa": "💬 <b>پشتیبانی</b>\n\nسؤال، بررسی یک پرداخت، یا دانلودی که مدام خطا "
        "می‌دهد؟ به {contact} پیام بده — خود ادمین می‌خواند.",
    },
    "support.unset": {
        "en": "💬 Support is not set up on this bot yet — try again later.",
        "fa": "💬 هنوز راه ارتباطی پشتیبانی روی این ربات تنظیم نشده است؛ بعداً امتحان کن.",
    },
    "language.title": {
        "en": "🌐 <b>Choose the bot's language</b>\n\nIt is saved with your account, so "
        "every message — including background downloads — arrives in it.",
        "fa": "🌐 <b>زبان ربات را انتخاب کن</b>\n\nزبان با حساب تو ذخیره می‌شود، پس همهٔ "
        "پیام‌ها — حتی دانلودهای پس‌زمینه — با همان زبان می‌رسند.",
    },
    "language.set": {
        "en": "✅ Language set to <b>{name}</b>.",
        "fa": "✅ زبان روی <b>{name}</b> تنظیم شد.",
    },
    "language.usage": {
        "en": "🌐 Usage: <code>/language en</code> or <code>/language fa</code>.",
        "fa": "🌐 استفاده: <code>/language en</code> یا <code>/language fa</code>.",
    },

    # ---------------------------------------------------------------------
    # Profile / status
    # ---------------------------------------------------------------------
    "profile.title": {"en": "👤 <b>My profile</b>", "fa": "👤 <b>پروفایل من</b>"},
    "profile.id": {
        "en": "🆔 Telegram id: <code>{telegram_id}</code>",
        "fa": "🆔 شناسهٔ تلگرام: <code>{telegram_id}</code>",
    },
    "profile.username": {"en": "🔗 Username: {username}", "fa": "🔗 نام کاربری: {username}"},
    "profile.status": {"en": "💎 Status: {status}", "fa": "💎 وضعیت: {status}"},
    "profile.status.free": {"en": "free 🪙", "fa": "رایگان 🪙"},
    "profile.status.admin": {
        "en": "VIP 💎 — admin (permanent, payment bypassed)",
        "fa": "ویژه 💎 — ادمین (دائمی، بدون پرداخت)",
    },
    "profile.status.premium_days": {
        "en": "VIP 💎 — {days} days left (until {date})",
        "fa": "ویژه 💎 — {days} روز مانده (تا {date})",
    },
    "profile.status.premium_lifetime": {"en": "VIP 💎 — permanent", "fa": "ویژه 💎 — دائمی"},
    "profile.quota_left": {
        "en": "📥 Today's quota: {used} of {limit} — {left} left",
        "fa": "📥 سهمیهٔ امروز: {used} از {limit} — {left} باقی مانده",
    },
    "profile.quota_used_up": {
        "en": "📥 Today's quota: {used} of {limit} — all used",
        "fa": "📥 سهمیهٔ امروز: {used} از {limit} — تمام شد",
    },
    "profile.quota_unlimited": {
        "en": "📥 Today's quota: {used} — unlimited ♾",
        "fa": "📥 سهمیهٔ امروز: {used} — بی‌نهایت ♾",
    },
    "profile.queue": {"en": "🕒 Jobs in queue: {depth}", "fa": "🕒 کارهای در صف: {depth}"},

    # ---------------------------------------------------------------------
    # Premium pitch
    # ---------------------------------------------------------------------
    "premium.title": {
        "en": (
            "💎 <b>VIP subscription</b>\n"
            "\n"
            "Daily quota: {free} → <b>{premium}</b> downloads\n"
            "Priority queue, no ads, made for archiving and everyday use.\n"
            "\n"
            "Plans 👇"
        ),
        "fa": (
            "💎 <b>اشتراک ویژه (VIP)</b>\n"
            "\n"
            "سهمیهٔ روزانه: {free} → <b>{premium}</b> دانلود\n"
            "صف با اولویت، بدون تبلیغ، برای آرشیو و استفادهٔ روزمره.\n"
            "\n"
            "تعرفه‌ها 👇"
        ),
    },
    "premium.admin_note": {
        "en": "👑 You are an admin: VIP is always on for you and nothing here costs anything.",
        "fa": "👑 تو ادمین هستی: اشتراک ویژه همیشه برایت فعال است و چیزی اینجا هزینه ندارد.",
    },

    # ---------------------------------------------------------------------
    # Help
    # ---------------------------------------------------------------------
    "help.title": {"en": "❓ <b>Help</b>", "fa": "❓ <b>راهنما</b>"},
    "help.body": {
        "en": (
            "1️⃣ Send a link (YouTube, Instagram, TikTok, Twitter/X, Spotify, SoundCloud, "
            "Facebook, Reddit and many more).\n"
            "2️⃣ Pick what you want — the bot only offers what that link actually has "
            "(1080p/720p/480p video, MP3 or M4A audio, or the images of a photo post).\n"
            "3️⃣ The download happens in the background and the file is sent right here — "
            "stay and watch, or leave; it will arrive anyway.\n"
            "\n"
            "Progress is edited into the same message, and a download that fails says why. "
            "Private links, live streams and playlists are not supported.\n"
            "\n"
            "<b>Commands</b>\n"
            "• /profile — your account, status and today's quota\n"
            "• /premium — VIP subscription\n"
            "• /status — account summary\n"
            "• /language — English / فارسی\n"
            "• /cancel — leave the current step\n"
            "• /start — this menu\n"
            "\n"
            "Every download uses one unit of the daily quota; /premium multiplies it."
        ),
        "fa": (
            "1️⃣ لینک رو بفرست (یوتیوب، اینستاگرام، تیک‌تاک، توییتر/X، اسپاتیفای، ساندکلاود، "
            "فیسبوک، ریدیت و خیلی‌های دیگر).\n"
            "2️⃣ انتخاب کن چی می‌خوای — ربات فقط گزینه‌هایی را نشان می‌دهد که آن لینک واقعاً "
            "دارد (ویدیو 1080p/720p/480p، صدای MP3 یا M4A، یا عکس‌های یک پست عکسی).\n"
            "3️⃣ دانلود در پس‌زمینه انجام می‌شه و فایل همین‌جا برات فرستاده می‌شه — می‌تونی "
            "همون‌جا منتظر بمونی یا بری، خبرش می‌رسه.\n"
            "\n"
            "پیشرفت دانلود را در همان پیام می‌بینی، و اگر لینکی دانلود نشد علتش گفته می‌شه. "
            "لینک خصوصی، پخش زنده و playlist دانلود نمی‌شه.\n"
            "\n"
            "<b>دستورها</b>\n"
            "• /profile — پروفایل، وضعیت و سهمیهٔ امروز\n"
            "• /premium — اشتراک ویژه (VIP)\n"
            "• /status — خلاصهٔ وضعیت حساب\n"
            "• /language — English / فارسی\n"
            "• /cancel — لغو مرحلهٔ فعلی\n"
            "• /start — همین منو\n"
            "\n"
            "هر دانلود یک واحد از سهمیهٔ روزانه کم می‌کند؛ با /premium سهمیه چند برابر می‌شود."
        ),
    },

    # ---------------------------------------------------------------------
    # Link intake: the wait, the questions, the answers
    # ---------------------------------------------------------------------
    "intake.analyse": {"en": "🔍 Analysing the link…", "fa": "🔍 در حال تحلیل لینک…"},
    "intake.queueing": {
        "en": "🔍 Analysing and queueing… ⏳",
        "fa": "🔍 در حال تحلیل و ارسال به صف پردازش... ⏳",
    },
    "intake.invalid_link": {
        "en": "❌ That link is not valid — it has to start with http:// or https://.",
        "fa": "❌ لینک نامعتبر است. لینک باید با http:// یا https:// شروع شود.",
    },
    "intake.unsupported": {
        "en": "❌ The extraction engine does not support this link.",
        "fa": "❌ این لینک توسط موتور استخراج پشتیبانی نمی‌شود.",
    },
    "intake.no_link_found": {
        "en": (
            "I could not find a link in your message. Send a full URL "
            "(like https://youtube.com/watch?v=...) or /cancel"
        ),
        "fa": (
            "لینکی در پیامت پیدا نکردم. یک URL کامل بفرست "
            "(مثل https://youtube.com/watch?v=...) یا /cancel"
        ),
    },
    "intake.link_updated": {
        "en": "Link updated — now pick what you want 👇",
        "fa": "لینک به‌روزرسانی شد؛ حالا انتخاب کن 👇",
    },
    "intake.step_in_progress": {
        "en": "Another step is still running; use /cancel to start over.",
        "fa": "یک مرحلهٔ دیگه هنوز در جریانه؛ با /cancel از نو شروع کن.",
    },
    "intake.choose_what": {"en": "What do you want? 👇", "fa": "چی می‌خوای؟ 👇"},
    "intake.choose_quality": {
        "en": "Pick a quality — the bot never upscales, so a lower ceiling simply gives "
        "you the best version up to it 👇",
        "fa": "کیفیت را انتخاب کن — ربات هرگز کیفیت را بالا نمی‌برد، پس سقف پایین‌تر یعنی "
        "بهترین نسخه تا همان حد 👇",
    },
    "intake.choose_audio": {
        "en": "Pick the audio format 👇",
        "fa": "فرمت صدا را انتخاب کن 👇",
    },
    "intake.choose_media": {
        "en": "This looks like a photo post — I will send whatever media it contains 👇",
        "fa": "این لینک پست عکسی است — هر رسانه‌ای که داشته باشد می‌فرستم 👇",
    },
    "intake.no_format": {
        "en": "Format: none of the choices are not an option; send the link again.",
        "fa": "فرمت مشخص نشد؛ لینک را دوباره بفرست.",
    },
    "intake.stale": {
        "en": "⚠️ This message is no longer live; send the link again.",
        "fa": "⚠️ این پیام دیگه در دسترس نیست؛ لطفاً لینک رو دوباره بفرست.",
    },
    "intake.link_expired": {
        "en": "The link expired; send it again.",
        "fa": "لینک منقضی شد؛ دوباره لینک رو بفرست.",
    },
    "intake.documents_only": {
        "en": "Send me links, text, or a link inside a caption — I download media.",
        "fa": "لینک بفرست (یا لینک داخل کپشن) — من رسانه دانلود می‌کنم.",
    },
    "intake.photo_auto": {
        "en": "🖼 A photo post — downloading its media now 👇",
        "fa": "🖼 پست عکسی است — همین حالا رسانه‌اش دانلود می‌شود 👇",
    },
    "intake.queued": {
        "en": "⏳ Added to the queue (position ≈ {depth}).",
        "fa": "⏳ لینک در صف پردازش قرار گرفت (موقعیت تقریبی: {depth}).",
    },
    "intake.queued_background": {
        "en": "Downloading and sending happen in the background — I will tell you right here.",
        "fa": "دانلود و ارسال در پس‌زمینه انجام می‌شه — همین‌جا خبرت می‌کنیم.",
    },
    "intake.cache_hit": {
        "en": "⚡️ This link was downloaded before — the file came from the cache.",
        "fa": "⚡️ این لینک قبلاً دانلود شده — فایل از حافظهٔ کش ارسال شد.",
    },
    "intake.quota_exhausted": {
        "en": "⛔️ Your daily quota ({used} of {limit}) is used up — /premium raises it.",
        "fa": "⛔️ سهمیهٔ دانلود امروزت ({used} از {limit}) تمام شده است — با /premium "
        "سهمیه‌ات را بیشتر کن.",
    },
    "intake.quota_exhausted_alert": {
        "en": "⛔️ Daily quota used up.",
        "fa": "⛔️ سهمیهٔ روزانه تمام شده است.",
    },
    "intake.download_usage": {
        "en": "Usage: /download &lt;link&gt; — or just send the link.",
        "fa": "استفاده: /download <لینک> — یا لینک رو مستقیم بفرست.",
    },
    "intake.cancelled": {"en": "✅ Cancelled.", "fa": "✅ لغو شد."},

    # ---------------------------------------------------------------------
    # Format / quality buttons and their headers
    # ---------------------------------------------------------------------
    "fmt.video_best": {"en": "🎬 Best available quality", "fa": "🎬 بهترین کیفیت موجود"},
    "fmt.video_1080": {"en": "🎬 1080p (up to)", "fa": "🎬 1080p (تا سقف)"},
    "fmt.video_720": {"en": "🎬 720p (up to)", "fa": "🎬 720p (تا سقف)"},
    "fmt.video_480": {"en": "🎬 480p (smallest file)", "fa": "🎬 480p (کم‌حجم‌ترین)"},
    "fmt.audio_mp3": {"en": "🎵 Audio — MP3 192k", "fa": "🎵 فقط صدا — MP3 192k"},
    "fmt.audio_m4a": {
        "en": "🎧 Audio — M4A (original, no re-encode)",
        "fa": "🎧 صدا — M4A (اصل، بدون تبدیل)",
    },
    "fmt.media": {"en": "🖼 Send the media of this post", "fa": "🖼 ارسال رسانهٔ این پست"},

    # ---------------------------------------------------------------------
    # Worker: the download's own narration
    # ---------------------------------------------------------------------
    "work.processing": {"en": "🔄 Processing the link…", "fa": "🔄 در حال پردازش لینک…"},
    "work.cache_resend": {
        "en": "⚡️ Already downloaded — sending from the cache…",
        "fa": "⚡️ لینک قبلاً دانلود شده؛ در حال ارسال از کش…",
    },
    "work.cache_done": {"en": "✅ Sent (from the cache)", "fa": "✅ ارسال شد (از حافظهٔ کش)"},
    "work.cache_caption": {
        "en": "📥 From the cache (a previous download) ⚡️",
        "fa": "📥 ارسال از حافظهٔ کش (دانلود قبلی) ⚡️",
    },
    "work.spotify_lookup": {
        "en": "🎵 Fetching this song…",
        "fa": "🎵 در حال آماده‌سازی این آهنگ…",
    },
    "work.retry": {
        "en": "⏳ Attempt {attempt} of {attempts}…",
        "fa": "⏳ تلاش {attempt} از {attempts}…",
    },
    "work.live": {"en": "⛔️ Live streams cannot be downloaded.", "fa": "⛔️ پخش زنده قابل دانلود نیست."},
    "work.too_big": {
        "en": "⛔️ The file ({size}) is larger than the {limit} MB limit.",
        "fa": "⛔️ حجم فایل ({size}) از سقف {limit} مگابایت بیشتر است.",
    },
    "work.final_too_big": {
        "en": "⛔️ The finished file is larger than the allowed limit.",
        "fa": "⛔️ حجم فایل نهایی از سقف مجاز بیشتر است.",
    },
    "work.account_missing": {
        "en": "⛔️ Account not found — send /start again.",
        "fa": "⛔️ حساب کاربر پیدا نشد؛ دوباره /start بزن.",
    },
    "work.quota_exhausted": {
        "en": "⛔️ Today's download quota ({limit}) is used up.",
        "fa": "⛔️ سهمیهٔ دانلود امروز ({limit}) تمام شده است.",
    },
    "work.downloading": {"en": "⬇️ Downloading: <b>{title}</b>", "fa": "⬇️ در حال دانلود: <b>{title}</b>"},
    "work.progress": {
        "en": "⬇️ Downloading: {percent:.0f}% ({done} / {total})",
        "fa": "⬇️ دانلود: {percent:.0f}% ({done} / {total})",
    },
    "work.fallback": {
        "en": "🛠 The main route could not serve this link; downloading through the fallback…",
        "fa": "🛠 مسیر اصلی به این لینک دسترسی نداشت؛ از مسیر جایگزین دانلود می‌شود…",
    },
    "work.uploading": {"en": "⬆️ Uploading to Telegram…", "fa": "⬆️ در حال ارسال به تلگرام…"},
    "work.done": {"en": "✅ Downloaded and sent.", "fa": "✅ دانلود و ارسال شد."},
    "work.failed": {"en": "❌ The download failed:\n{error}", "fa": "❌ دانلود انجام نشد:\n{error}"},
    "work.unexpected": {"en": "unexpected error", "fa": "خطای نامشخص"},
    "work.internal_error": {
        "en": "internal error: {detail}",
        "fa": "خطای داخلی: {detail}",
    },
    "work.caption_platform": {"en": "🌐 {platform}", "fa": "🌐 {platform}"},
    # A music link is captioned as the song, not as the site it was fetched from:
    # the file carries the same metadata as its tags, so the caption agrees with the
    # client's own player.
    "work.caption_artist": {"en": "🎤 {artist}", "fa": "🎤 {artist}"},
    "work.caption_album": {"en": "💿 {album}", "fa": "💿 {album}"},
    "work.caption_size": {"en": "📦 {size}", "fa": "📦 {size}"},
    "work.caption_duration": {"en": "⏱ {duration}", "fa": "⏱ {duration}"},
    "work.caption_quality": {"en": "🎬 {resolution}", "fa": "🎬 کیفیت {resolution}"},
    "work.login_block": {
        "en": (
            "🔒 This link can only be downloaded with a signed-in account, and the bot's "
            "current session is not allowed to — the problem is not on your side.\n"
            "The admins have been told; once it is fixed, send the same link again."
        ),
        "fa": (
            "🔒 این لینک فقط با یک حساب واردشده (لاگین) قابل دانلود است و اتصال فعلی ربات "
            "اجازهٔ دانلود را ندارد — مشکل از سمت شما نیست.\n"
            "موضوع را به ادمین اطلاع دادیم؛ بعد از این‌که برطرف شد، همین لینک را دوباره بفرست."
        ),
    },

    # ---------------------------------------------------------------------
    # Errors, by the code the engine raised
    # ---------------------------------------------------------------------
    "err.GENERAL": {
        "en": "The extraction failed unexpectedly. Try again in a moment.",
        "fa": "خطای غیرمنتظره در استخراج رخ داد؛ کمی بعد دوباره تلاش کنید.",
    },
    "err.UNSUPPORTED_URL": {
        "en": "The extraction engine does not support this link.",
        "fa": "این لینک توسط موتور استخراج پشتیبانی نمی‌شود.",
    },
    "err.PRIVATE_VIDEO": {
        "en": "This media is not available (private or deleted).",
        "fa": "ویدیو در دسترس نیست (خصوصی یا حذف‌شده).",
    },
    "err.AGE_RESTRICTED": {
        "en": "This video is age-restricted and needs a signed-in account.",
        "fa": "ویدیو محدودیت سنی دارد و نیاز به تأیید حساب دارد.",
    },
    "err.GEO_RESTRICTED": {
        "en": "This content is not available in your region.",
        "fa": "این محتوا در منطقه شما در دسترس نیست.",
    },
    "err.LIVE_STREAM": {"en": "Live streams cannot be downloaded.", "fa": "پخش زنده قابل دانلود نیست."},
    "err.PLAYLIST_NOT_SUPPORTED": {
        "en": "Playlists cannot be downloaded — send a single link.",
        "fa": "دانلود لیست پخش (playlist) پشتیبانی نمی‌شود.",
    },
    "err.FFMPEG_REQUIRED": {
        "en": "Converting to MP3 needs ffmpeg on the server; try the M4A audio option instead.",
        "fa": "تبدیل به MP3 نیاز به نصب ffmpeg دارد؛ گزینهٔ صدای M4A را امتحان کنید.",
    },
    "err.TIMEOUT": {
        "en": "The connection to the source timed out; try again.",
        "fa": "ارتباط با سرور مبدأ قطع شد؛ دوباره تلاش کنید.",
    },
    "err.EXTRACTOR_BLOCKED": {
        "en": "The site blocked the download request; try again in a little while.",
        "fa": "سایت مبدأ دانلود را مسدود کرد؛ کمی بعد دوباره تلاش کنید.",
    },
    "err.SESSION_STALE": {
        "en": "YouTube refused this request (stale session); try again in a moment.",
        "fa": "یوتیوب این درخواست را نپذیرفت (سشن کهنه است)؛ چند لحظه بعد دوباره تلاش کنید.",
    },
    "err.DRM_PROTECTED": {
        "en": "This content is DRM-protected and the extraction engine does not support it.",
        "fa": "این محتوا با DRM محافظت می‌شود و موتور استخراج از دانلودش پشتیبانی نمی‌کند.",
    },
    "err.IMAGE_ONLY": {
        "en": "This link has no downloadable video (a photo post, or no media at all).",
        "fa": "این لینک ویدیوی قابل دانلود ندارد (پست عکسی یا بدون رسانه).",
    },
    "err.SPOTIFY_NOT_A_TRACK": {
        "en": "From Spotify only a single track link is supported — not albums or playlists.",
        "fa": "از اسپاتیفای فقط لینک یک آهنگ (Track) پشتیبانی می‌شود؛ آلبوم و پلی‌لیست نه.",
    },
    "err.SPOTIFY_LOOKUP_FAILED": {
        "en": "I could not read this song's details from Spotify. Send it again in a moment.",
        "fa": "اطلاعات این آهنگ از اسپاتیفای خوانده نشد؛ کمی بعد دوباره بفرست.",
    },
    "err.SPOTIFY_NO_MATCH": {
        "en": "The YouTube version of this song was not found. (Spotify's own files are "
        "DRM-protected and cannot be downloaded either.)",
        "fa": "نسخهٔ یوتیوب این آهنگ پیدا نشد. (خودِ اسپاتیفای هم به خاطر DRM قابل دانلود نیست.)",
    },

    # ---------------------------------------------------------------------
    # Preflight: said before the wait instead of after it
    # ---------------------------------------------------------------------
    "preflight.risky": {
        "en": "ℹ️ Note: the bot's cookies are not signed in to YouTube, so if this video "
        "needs a login the download may fail — if it does, the admins are told.",
        "fa": "ℹ️ نکته: کوکی ربات الان لاگین یوتیوب نیست، پس اگر این ویدیو لاگین لازم داشته "
        "باشد دانلود ممکن است شکست بخورد — در آن صورت به ادمین گزارش می‌شود.",
    },
    "preflight.risky_fallback": {
        "en": "ℹ️ Note: the bot's cookies are not signed in to YouTube, but if the request "
        "is refused the link is downloaded through the fallback — and if that fails too, "
        "the admins are told.",
        "fa": "ℹ️ نکته: کوکی ربات الان لاگین یوتیوب نیست، ولی اگر درخواست رد شود از مسیر "
        "جایگزین دانلود می‌شود — و اگر آن هم نشد، به ادمین گزارش می‌شود.",
    },
    "preflight.blocked": {
        "en": (
            "🚧 This YouTube link cannot be downloaded right now.\n\n"
            "Recent attempts showed YouTube treating the bot's requests as anonymous and "
            "refusing them; the cause is known (the cookies are not signed in) and the "
            "admins have been told.\n"
            "Please send it again a little later — non-YouTube links are unaffected."
        ),
        "fa": (
            "🚧 این لینک یوتیوب همین حالا دانلود نمی‌شود.\n\n"
            "آخرین تلاش‌ها نشان داده یوتیوب درخواست‌های فعلی ربات را ناشناس می‌بیند و رد "
            "می‌کند؛ علتش هم پیدا شده (لاگین نبودن کوکی) و به ادمین گزارش شده است.\n"
            "لطفاً کمی بعد دوباره بفرست — لینک‌های غیر یوتیوب مشکلی ندارند."
        ),
    },
    "preflight.fallback_queue": {
        "en": "ℹ️ Note: YouTube is refusing the bot's anonymous requests; if it comes to it, "
        "this link is downloaded through the fallback.",
        "fa": "ℹ️ نکته: یوتیوب درخواست‌های ناشناس ربات را رد می‌کند؛ اگر لازم شود همین لینک "
        "از مسیر جایگزین دانلود می‌شود.",
    },

    # ---------------------------------------------------------------------
    # Payments (manual, card to card)
    # ---------------------------------------------------------------------
    "pay.currency": {"en": "Toman", "fa": "تومان"},
    "pay.pick_plan": {
        "en": "Pick one of the subscription plans 👇",
        "fa": "یکی از پلن‌های اشتراک رو انتخاب کن 👇",
    },
    "pay.no_plans": {"en": "There is nothing for sale right now.", "fa": "فعلاً پلنی برای فروش نداریم."},
    "pay.plan_button": {
        "en": "{name} — {price} {currency}",
        "fa": "{name} — {price} {currency}",
    },
    "pay.card_title": {
        "en": (
            "💳 <b>Card-to-card payment</b>\n"
            "\n"
            "Plan: <b>{plan}</b>\n"
            "Amount: <b>{price} {currency}</b>\n"
            "\n"
            "Card holder: {holder}\n"
            "Card number: <code>{card}</code>\n"
            "\n"
            "After the transfer, send the <b>receipt photo</b> here.\n"
            "To cancel: /cancel"
        ),
        "fa": (
            "💳 <b>پرداخت کارت به کارت</b>\n"
            "\n"
            "پلن: <b>{plan}</b>\n"
            "مبلغ: <b>{price} {currency}</b>\n"
            "\n"
            "به نام: {holder}\n"
            "شماره کارت: <code>{card}</code>\n"
            "\n"
            "بعد از واریز، <b>عکس رسید</b> رو همین‌جا بفرست.\n"
            "برای انصراف: /cancel"
        ),
    },
    "pay.stale": {
        "en": "⚠️ This message is no longer live; use /subscribe again.",
        "fa": "⚠️ این پیام دیگه در دسترس نیست؛ /subscribe رو دوباره بزن.",
    },
    # The three plans the installer seeds. Their *name* is a row in
    # `subscription_plans` — data an operator may rewrite — so only the labels the
    # installer itself wrote are translated (:func:`core.i18n.plan_name`); a renamed
    # plan is shown exactly as stored, in either language.
    "plan.week": {"en": "1 week", "fa": "۱ هفته"},
    "plan.month": {"en": "1 month", "fa": "۱ ماه"},
    "plan.quarter": {"en": "3 months", "fa": "۳ ماه"},
    "pay.plan_invalid": {"en": "Invalid plan.", "fa": "پلن نامعتبر."},
    "pay.plan_missing": {"en": "Plan not found.", "fa": "پلن پیدا نشد."},
    "pay.txn_invalid": {
        "en": "Invalid transaction; use /subscribe and try again.",
        "fa": "تراکنش نامعتبر است؛ /subscribe بزن و دوباره تلاش کن.",
    },
    "pay.txn_missing": {
        "en": "That receipt does not belong to an open transaction; use /subscribe again.",
        "fa": "این رسید به تراکنش فعالی تعلق نداره؛ /subscribe بزن و دوباره امتحان کن.",
    },
    "pay.txn_gone": {
        "en": "That transaction was not found; use /subscribe and try again.",
        "fa": "این تراکنش پیدا نشد؛ /subscribe بزن و دوباره تلاش کن.",
    },
    "pay.receipt_received": {
        "en": "✅ Receipt received. Your subscription activates as soon as an admin approves it.",
        "fa": "✅ رسید دریافت شد. پس از تأیید ادمین، اشتراکت فعال می‌شه.",
    },
    "pay.receipt_caption": {
        "en": (
            "🧾 <b>New payment receipt</b>\n"
            "\n"
            "User: {user} (ID: <code>{telegram_id}</code>)\n"
            "Plan: {plan}\n"
            "Amount: {price} {currency}\n"
            "Transaction: <code>{txn_id}</code>\n"
            "Method: card to card"
        ),
        "fa": (
            "🧾 <b>رسید پرداخت جدید</b>\n"
            "\n"
            "کاربر: {user} (ID: <code>{telegram_id}</code>)\n"
            "پلن: {plan}\n"
            "مبلغ: {price} {currency}\n"
            "شناسه تراکنش: <code>{txn_id}</code>\n"
            "روش: کارت به کارت"
        ),
    },
    "pay.approve": {"en": "✅ Approve", "fa": "✅ تأیید"},
    "pay.reject": {"en": "❌ Reject", "fa": "❌ رد"},
    "pay.approved_note": {"en": "✅ Approved", "fa": "✅ تأیید شد"},
    "pay.rejected_note": {"en": "❌ Rejected", "fa": "❌ رد شد"},
    "pay.admin_only": {"en": "⛔️ Admins only.", "fa": "⛔️ فقط ادمین می‌تونه."},
    "pay.already_decided": {
        "en": "This transaction has already been decided.",
        "fa": "این تراکنش قبلاً پردازش شده.",
    },
    "pay.decided_by": {"en": "{status} by {admin}", "fa": "{status} توسط {admin}"},
    "pay.granted_user": {
        "en": "🎉 Your payment was approved! VIP is active — thank you 🚀",
        "fa": "🎉 پرداختت تأیید شد! اشتراک پریمیوم فعال شد — ممنون از همراهی‌ات 🚀",
    },
    "pay.declined_user": {
        "en": "❌ Your payment was rejected. If you think that is wrong, contact support.",
        "fa": "❌ پرداختت رد شد. اگه فکر می‌کنی اشتباهه با پشتیبانی در ارتباط باش.",
    },

    # ---------------------------------------------------------------------
    # Admin panel
    # ---------------------------------------------------------------------
    "admin.only": {"en": "⛔️ Admins only.", "fa": "⛔️ فقط ادمین می‌تونه."},
    "admin.title": {"en": "🛠 <b>Admin panel</b>", "fa": "🛠 <b>پنل مدیریت</b>"},
    "admin.subtitle": {
        "en": "Everything below is read from the running stack; the buttons are the tools.",
        "fa": "همه‌چیز از استک در حال اجرا خوانده می‌شود؛ دکمه‌ها ابزارها هستند.",
    },
    "admin.btn_stats": {"en": "📊 Stats", "fa": "📊 آمار"},
    "admin.btn_health": {"en": "🩺 Health", "fa": "🩺 سلامت"},
    "admin.btn_queue": {"en": "🕒 Queue", "fa": "🕒 صف"},
    "admin.btn_tools": {"en": "🔧 Tools", "fa": "🔧 ابزارها"},
    "admin.btn_back": {"en": "🔙 Back to panel", "fa": "🔙 بازگشت به پنل"},
    "admin.btn_doctor": {"en": "🩺 Run the YouTube doctor", "fa": "🩺 اجرای دکتر یوتیوب"},
    "admin.btn_refresh": {"en": "♻️ Re-export the cookie jar now", "fa": "♻️ اکسپورت دوبارهٔ کوکی"},
    "admin.btn_broadcast": {"en": "📣 Broadcast", "fa": "📣 پیام همگانی"},
    "admin.btn_support": {"en": "💬 Support button", "fa": "💬 دکمهٔ پشتیبانی"},
    "admin.broadcast_intro": {
        "en": (
            "📣 <b>Broadcast</b>\n"
            "\n"
            "One message to every account the bot has — <b>{users}</b> of them, of "
            "which the ones who blocked the bot cannot be reached (they are counted, "
            "not failed).\n"
            "\n"
            "Press the button, then send what it should say: your next message is the "
            "draft, and you see it back before anything goes out."
        ),
        "fa": (
            "📣 <b>پیام همگانی</b>\n"
            "\n"
            "یک پیام برای همهٔ حساب‌هایی که ربات دارد — <b>{users}</b> حساب؛ آن‌هایی "
            "که ربات را بلاک کرده‌اند قابل دسترسی نیستند (جدا شمرده می‌شوند، خطا حساب "
            "نمی‌شوند).\n"
            "\n"
            "دکمه را بزن و بعد متن را بفرست: پیام بعدی تو پیش‌نویس است و قبل از ارسال "
            "دوباره نشانت داده می‌شود."
        ),
    },
    "admin.broadcast_start": {"en": "✍️ Write the message", "fa": "✍️ نوشتن پیام"},
    "admin.broadcast_prompt": {
        "en": "✍️ Send me the message to broadcast (text, with any formatting you like).",
        "fa": "✍️ پیامی که می‌خواهی همگانی شود را بفرست (متن، با هر فرمتی که می‌خواهی).",
    },
    "admin.broadcast_preview": {
        "en": "👆 That is exactly what {users} users would receive. Send it?",
        "fa": "👆 دقیقاً همین برای {users} کاربر فرستاده می‌شود. ارسال شود؟",
    },
    "admin.broadcast_go": {"en": "✅ Send to all", "fa": "✅ ارسال به همه"},
    "admin.broadcast_cancel": {"en": "❌ Cancel", "fa": "❌ لغو"},
    "admin.broadcast_cancelled": {
        "en": "❌ Broadcast cancelled — nothing was sent.",
        "fa": "❌ پیام همگانی لغو شد — چیزی فرستاده نشد.",
    },
    "admin.broadcast_empty": {
        "en": "That was not text — send the announcement as a message.",
        "fa": "این متن نبود — متن پیام را به‌صورت پیام بفرست.",
    },
    "admin.broadcast_no_users": {
        "en": "There is nobody to message yet.",
        "fa": "هنوز کاربری برای ارسال وجود ندارد.",
    },
    "admin.broadcast_sending": {
        "en": "📣 Sending… {sent}/{total}",
        "fa": "📣 در حال ارسال… {sent}/{total}",
    },
    "admin.broadcast_done": {
        "en": (
            "✅ <b>Broadcast finished</b>\n"
            "\n"
            "📨 Sent: <b>{sent}</b> of {total}\n"
            "🚫 Blocked the bot: <b>{blocked}</b>\n"
            "⚠️ Failed: <b>{failed}</b>"
        ),
        "fa": (
            "✅ <b>پیام همگانی تمام شد</b>\n"
            "\n"
            "📨 ارسال‌شده: <b>{sent}</b> از {total}\n"
            "🚫 ربات را بلاک کرده‌اند: <b>{blocked}</b>\n"
            "⚠️ ناموفق: <b>{failed}</b>"
        ),
    },
    "admin.broadcast_failed": {
        "en": "⚠️ The broadcast stopped early — the log says why; the numbers below are "
        "from before it stopped.",
        "fa": "⚠️ پیام همگانی نیمه‌کاره ماند — دلیلش در لاگ است؛ گزارش ناقص است.",
    },
    "admin.support_intro": {
        "en": (
            "💬 <b>Support button</b>\n"
            "\n"
            "Every user's menu has a 💬 Support button, and it points wherever you say: "
            "an <code>@username</code>, a full URL (a web form, a group invite) — or "
            "plain text, which is shown as it is.\n"
            "\n"
            "Right now: <b>{contact}</b>"
        ),
        "fa": (
            "💬 <b>دکمهٔ پشتیبانی</b>\n"
            "\n"
            "منوی هر کاربر یک دکمهٔ 💬 پشتیبانی دارد و هر جا بگویی اشاره می‌کند: "
            "<code>@username</code>، یک URL کامل (فرم وب، دعوت گروه) — یا متن ساده که "
            "همان‌طور نمایش داده می‌شود.\n"
            "\n"
            "الان: <b>{contact}</b>"
        ),
    },
    "admin.support_none": {"en": "— (no button is shown)", "fa": "— (دکمه‌ای نشان داده نمی‌شود)"},
    "admin.support_set": {"en": "✍️ Set the contact", "fa": "✍️ تنظیم راه ارتباطی"},
    "admin.support_clear": {"en": "🗑 Remove the button", "fa": "🗑 حذف دکمه"},
    "admin.support_prompt": {
        "en": "✍️ Send the support contact: <code>@username</code>, or a full URL.",
        "fa": "✍️ راه ارتباطی پشتیبانی را بفرست: <code>@username</code> یا یک URL کامل.",
    },
    "admin.support_saved": {
        "en": "✅ Support button set to {contact} ({kind}). It is live for every user "
        "right now.",
        "fa": "✅ دکمهٔ پشتیبانی روی {contact} تنظیم شد ({kind}). همین حالا برای همهٔ "
        "کاربران فعال است.",
    },
    "admin.support_linked": {"en": "opens as a link", "fa": "به‌صورت لینک باز می‌شود"},
    "admin.support_plain": {
        "en": "shown as plain text — Telegram cannot link it",
        "fa": "به‌صورت متن ساده نمایش داده می‌شود — تلگرام نمی‌تواند لینکش کند",
    },
    "admin.support_cleared": {
        "en": "🗑 The support button was removed from the user menu.",
        "fa": "🗑 دکمهٔ پشتیبانی از منوی کاربران حذف شد.",
    },
    "admin.stats": {
        "en": (
            "📊 <b>Stats</b>\n"
            "\n"
            "👥 Users: <b>{users}</b> ({premium} VIP)\n"
            "🐣 New today: <b>{new_users}</b>\n"
            "📥 Downloads today: <b>{downloads_today}</b> by {active_today} user(s)\n"
            "⚡️ Cache rows: <b>{cache_rows}</b>\n"
            "🚧 Failures (24h): <b>{blocks_24h}</b>\n"
            "💳 Pending payments: <b>{pending_txns}</b>\n"
            "🌐 Language mix: {languages}"
        ),
        "fa": (
            "📊 <b>آمار</b>\n"
            "\n"
            "👥 کاربران: <b>{users}</b> ({premium} ویژه)\n"
            "🐣 امروز تازه: <b>{new_users}</b>\n"
            "📥 دانلود امروز: <b>{downloads_today}</b> توسط {active_today} کاربر\n"
            "⚡️ ردیف‌های کش: <b>{cache_rows}</b>\n"
            "🚧 شکست‌ها (۲۴ ساعت): <b>{blocks_24h}</b>\n"
            "💳 پرداخت‌های در انتظار: <b>{pending_txns}</b>\n"
            "🌐 ترکیب زبان‌ها: {languages}"
        ),
    },
    "admin.language_count": {"en": "{language}: {count}", "fa": "{language}: {count}"},
    "admin.stats_brief": {
        "en": "👥 {users} users · 📥 {downloads_today} downloads today · 🚧 {blocks_24h} failures (24h)",
        "fa": "👥 {users} کاربر · 📥 {downloads_today} دانلود امروز · 🚧 {blocks_24h} شکست (۲۴ ساعت)",
    },
    "panel.cobalt_line": {
        "en": "{icon} Fallback extractor ({where}, {url}, {dialect}): {state}",
        "fa": "{icon} موتور جایگزین ({where}، {url}، {dialect}): {state}",
    },
    "panel.embedded": {"en": "embedded", "fa": "داخلی"},
    "panel.remote": {"en": "remote", "fa": "بیرونی"},
    "panel.cobalt.error": {
        "en": "❔ Fallback extractor: the probe itself failed — see the logs",
        "fa": "❔ موتور جایگزین: خود پروب خطا داد — لاگ را ببینید",
    },
    "panel.cobalt.ready": {"en": "ready", "fa": "آماده به کار"},
    "panel.cobalt.quarantined": {"en": "quarantined", "fa": "قرنطینه"},
    "panel.cobalt.auth": {"en": "needs an API key", "fa": "نیازمند کلید احراز هویت"},
    "panel.cobalt.youtube": {"en": "no YouTube session/cookies", "fa": "برای یوتیوب سشن/کوکی ندارد"},
    "panel.cobalt.unreachable": {"en": "unreachable", "fa": "در دسترس نیست"},
    "panel.cobalt.degraded": {
        "en": "answered, but did not serve the last link",
        "fa": "پاسخ داد، ولی این لینک را نگرفت",
    },
    "panel.cobalt.off": {"en": "off (not configured)", "fa": "خاموش (تنظیم نشده)"},
    "panel.cobalt.unknown": {"en": "not tested yet", "fa": "تست نشد"},
    "panel.pot_line": {
        "en": "🔑 {name} ({url}): {state}",
        "fa": "🔑 {name} ({url}): {state}",
    },
    "panel.session_line": {
        "en": "🎫 {name} ({url}): {state}",
        "fa": "🎫 {name} ({url}): {state}",
    },
    "panel.helper_off": {
        "en": "⚪ {name}: not configured",
        "fa": "⚪ {name}: تنظیم نشده",
    },
    "admin.health": {
        "en": (
            "🩺 <b>Health</b>\n"
            "\n"
            "🗄 Database: {database}\n"
            "🧠 Redis / queue: {redis}\n"
            "📨 Queue depth: {depth}\n"
            "👷 Workers: {workers}\n"
            "\n"
            "{cobalt}\n"
            "{pot}\n"
            "{session}"
        ),
        "fa": (
            "🩺 <b>سلامت</b>\n"
            "\n"
            "🗄 پایگاه داده: {database}\n"
            "🧠 ردیس / صف: {redis}\n"
            "📨 عمق صف: {depth}\n"
            "👷 کارگرها: {workers}\n"
            "\n"
            "{cobalt}\n"
            "{pot}\n"
            "{session}"
        ),
    },
    "admin.check_ok": {"en": "🟢 online", "fa": "🟢 در دسترس"},
    "admin.check_fail": {"en": "🔴 unreachable", "fa": "🔴 خطا"},
    "admin.queue": {
        "en": (
            "🕒 <b>Queue</b>\n"
            "\n"
            "📨 Waiting tasks: <b>{depth}</b>\n"
            "👷 Workers: <b>{workers}</b> (queue backend: {backend})\n"
            "\n"
            "A worker takes one task at a time; the depth is what a user is waiting behind."
        ),
        "fa": (
            "🕒 <b>صف</b>\n"
            "\n"
            "📨 کارهای در انتظار: <b>{depth}</b>\n"
            "👷 کارگرها: <b>{workers}</b> (بک‌اند صف: {backend})\n"
            "\n"
            "هر کارگر یک کار را همزمان برمی‌دارد؛ عمق صف یعنی کاربر چند کار عقب‌تر ایستاده است."
        ),
    },
    "admin.tools": {
        "en": (
            "🔧 <b>Tools</b>\n"
            "\n"
            "• 🩺 YouTube doctor — the full chain (cookies, PO token, JS runtime, proxy) "
            "plus a live probe, one verdict and the next fix.\n"
            "• ♻️ Cookie re-export — read a browser profile into the jar now, verify it with "
            "a probe, and tell every admin the outcome.\n"
            "• 📣 Broadcast — one message to every user, with a preview first and a "
            "report after.\n"
            "• 💬 Support button — point the button in the user menu at a username or a "
            "URL, or remove it.\n"
            "\n"
            "The commands are still there: /doctor /refresh /blocks /trend /fixlogin "
            "/broadcast."
        ),
        "fa": (
            "🔧 <b>ابزارها</b>\n"
            "\n"
            "• 🩺 دکتر یوتیوب — کل زنجیره (کوکی، PO token، رانتایم JS، پروکسی) به‌همراه "
            "پروب زنده، یک حکم و قدم بعدی.\n"
            "• ♻️ اکسپورت دوبارهٔ کوکی — خواندن پروفایل مرورگر داخل جار، تأیید با پروب و "
            "اطلاع نتیجه به همهٔ ادمین‌ها.\n"
            "• 📣 پیام همگانی — یک پیام برای همهٔ کاربران، اول پیش‌نمایش و بعد گزارش.\n"
            "• 💬 دکمهٔ پشتیبانی — دکمهٔ منوی کاربر به یوزرنیم یا URL وصل می‌شود، یا حذف.\n"
            "\n"
            "دستورها هم سر جای خودشان هستند: /doctor /refresh /blocks /trend /fixlogin "
            "/broadcast."
        ),
    },
    "admin.stale": {
        "en": "⚠️ This panel is out of date; send /admin again.",
        "fa": "⚠️ این پنل به‌روز نیست؛ /admin را دوباره بزن.",
    },
    "admin.login_block_alert": {
        "en": (
            "🍪 <b>Downloads are being refused because the bot is not signed in</b>\n"
            "\n"
            "Last failed link: <code>{url}</code>\n"
            "Cause: {cause}\n"
            "\n"
            "👉 Refresh the cookies and drop the jar in; the next download picks it up "
            "(no restart needed). Full report: /doctor"
        ),
        "fa": (
            "🍪 <b>دانلودها به خاطر لاگین نبودن ربات رد می‌شوند</b>\n"
            "\n"
            "آخرین لینک ناموفق: <code>{url}</code>\n"
            "علت: {cause}\n"
            "\n"
            "👉 کوکی را تازه کنید و بفرستید؛ دانلود بعدی خودش برمی‌دارد (بدون ری‌استارت). "
            "گزارش کامل: /doctor"
        ),
    },
    "admin.no_cookies_cause": {
        "en": (
            "there is no usable YouTube cookie jar (COOKIE_FILE empty or unreadable) — "
            "every request goes out anonymous."
        ),
        "fa": (
            "کوکی قابل‌استفاده‌ای برای یوتیوب نیست (COOKIE_FILE خالی/غیرقابل خواندن) — "
            "هر درخواست ناشناس می‌رود."
        ),
    },

    # ---------------------------------------------------------------------
    # Misc
    # ---------------------------------------------------------------------
    "misc.unknown_language": {
        "en": "🌐 Unknown language. Supported: {options}.",
        "fa": "🌐 زبان ناشناخته. پشتیبانی‌شده: {options}.",
    },
    "misc.friend": {"en": "there", "fa": "دوست عزیز"},
    # A file whose size is genuinely unknown (a live-ish stream, an estimate that
    # never arrived) — the word goes where the number would.
    "misc.unknown_size": {"en": "unknown", "fa": "نامشخص"},
    "menu.language_hint": {
        "en": "🌐 Language: /language — English / فارسی",
        "fa": "🌐 زبان: /language — English / فارسی",
    },
}

#: The plan labels the installer seeds, and the catalogue key each one resolves to.
#:
#: Keys, not translations, because these strings are *already in other databases*:
#: every deployment seeded so far holds the Persian labels, and a bot that learned a
#: second language must keep recognising them rather than showing them as if an
#: operator had typed them. Nothing else is listed here on purpose — a plan an
#: operator renamed is theirs, and is never overwritten by a translation.
SEEDED_PLAN_ALIASES: Final[dict[str, str]] = {
    "۱ هفته": "plan.week",
    "۱ ماه": "plan.month",
    "۳ ماه": "plan.quarter",
}
