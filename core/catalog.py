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
            "Send a link and I'll show you what's on it — the file lands <b>right "
            "here</b>, and a link that cannot be downloaded says why.\n"
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
            "لینک رو بفرست تا گزینه‌های موجود رو بهت نشون بدم — فایل <b>همین‌جا</b> "
            "برات می‌رسه، و اگر لینکی دانلود نشد علتش گفته می‌شه.\n"
            "\n"
            "👇 از دکمه‌های زیر شروع کن:"
        ),
    },
    # The command menu Telegram draws when someone types "/". Descriptions are
    # Telegram's own 256-character field, so they stay short and imperative.
    "cmd.start": {"en": "Start the bot", "fa": "شروع ربات"},
    "cmd.download": {"en": "Download a link", "fa": "دانلود یک لینک"},
    "cmd.profile": {"en": "My quota and status", "fa": "سهمیه و وضعیت من"},
    "cmd.premium": {"en": "Upgrade to VIP", "fa": "ارتقا به ویژه"},
    "cmd.language": {"en": "Change language", "fa": "تغییر زبان"},
    "cmd.help": {"en": "Help and supported sites", "fa": "راهنما و سایت‌های پشتیبانی‌شده"},
    "cmd.admin": {"en": "Admin panel", "fa": "پنل مدیریت"},
    "cmd.doctor": {"en": "Diagnose YouTube", "fa": "عیب‌یابی یوتیوب"},
    "cmd.oauth": {"en": "YouTube TV login (OAuth)", "fa": "لاگین یوتیوب با TV"},
    "cmd.blocks": {"en": "Block digest", "fa": "گزارش بلاک‌ها"},
    "cmd.trend": {"en": "Failures per day", "fa": "روند خطاها"},
    "cmd.refresh": {"en": "Re-export the cookie jar", "fa": "ساخت دوبارهٔ کوکی"},
    "cmd.fixlogin": {"en": "Fix the YouTube login", "fa": "رفع ورود یوتیوب"},
    "cmd.broadcast": {"en": "Message all users", "fa": "پیام به همهٔ کاربران"},
    "cmd.status": {"en": "Download status", "fa": "وضعیت دانلود"},
    "menu.profile": {"en": "👤 My profile", "fa": "👤 پروفایل من"},
    "menu.premium": {"en": "💎 Go VIP", "fa": "💎 ارتقا به ویژه (VIP)"},
    "menu.language": {"en": "🌐 Language", "fa": "🌐 زبان"},
    "menu.download": {"en": "⬇️ Download", "fa": "⬇️ دانلود"},
    # The one menu button an operator fills in: its target lives in the database.
    "menu.support": {"en": "💬 Support", "fa": "💬 پشتیبانی"},
    "menu.add_group": {"en": "👥 Add to a group", "fa": "👥 افزودن به گروه"},
    # Drawn only for ids in ADMIN_IDS: the panel is not a hidden feature, it is just
    # not a user's business.
    "menu.admin": {"en": "🛠 Admin panel", "fa": "🛠 پنل مدیریت"},
    "menu.back": {"en": "⬅️ Back", "fa": "⬅️ بازگشت"},
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
    # Bilingual on purpose: the reader's language is exactly what is not known
    # yet, so the first-run screen says it in both.
    "language.first_time": {
        "en": (
            "🙋 <b>Welcome!</b>\n\n"
            "برای شروع لطفا زبان خود را انتخاب کنید.\n\n"
            "To start, please select your language."
        ),
        "fa": (
            "🙋 <b>Welcome!</b>\n\n"
            "برای شروع لطفا زبان خود را انتخاب کنید.\n\n"
            "To start, please select your language."
        ),
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
    "profile.language": {"en": "🌐 Language: {language}", "fa": "🌐 زبان: {language}"},

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
    # Download
    # ---------------------------------------------------------------------
    "download.title": {"en": "⬇️ <b>Download</b>", "fa": "⬇️ <b>دانلود</b>"},
    "download.how": {
        "en": (
            "Send me a link — YouTube, Instagram, TikTok, X, Spotify or dozens of "
            "other sites — and I'll show you what's on it.\n\n"
            "🎬 video · 🎵 audio · 🖼️ photos\n\n"
            "Quality and format depend on what the link holds."
        ),
        "fa": (
            "لینک رو بفرست — یوتیوب، اینستاگرام، تیک‌تاک، ایکس، اسپاتیفای یا ده‌ها "
            "سایت دیگه — تا گزینه‌های موجود رو بهت نشون بدم.\n\n"
            "🎬 ویدیو · 🎵 صدا · 🖼️ تصاویر\n\n"
            "کیفیت و فرمت بسته به محتوای لینک قابل انتخابه."
        ),
    },

    # ---------------------------------------------------------------------
    # Link intake: the wait, the questions, the answers
    # ---------------------------------------------------------------------
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
    "intake.choose_what": {
        "en": "What should I download for you? 👇",
        "fa": "چی می‌خوای دانلود کنی؟ 👇",
    },
    "intake.choose_quality": {
        "en": "🎞 Pick the quality 👇",
        "fa": "🎞 کیفیتش رو انتخاب کن 👇",
    },
    "intake.choose_audio": {
        "en": "🎵 Pick the audio format 👇",
        "fa": "🎵 فرمت صدا رو انتخاب کن 👇",
    },
    "intake.choose_media": {
        "en": "This looks like a photo post — I will send whatever media it contains 👇",
        "fa": "این لینک پست عکسی است — هر رسانه‌ای که داشته باشد می‌فرستم 👇",
    },
    "intake.no_format": {
        "en": "That option is not available for this link — send the link again.",
        "fa": "این گزینه برای این لینک نیست؛ لینک را دوباره بفرست.",
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
    # A video-capable link whose qualities could *not* be discovered says so and
    # offers a retry — it never silently becomes a default download.
    "intake.probe_failed": {
        "en": (
            "❌ I couldn't read the qualities available for this link right now. "
            "Tap try again — or send the link again in a moment."
        ),
        "fa": (
            "❌ الان نتونستم کیفیت‌های موجود برای این لینک رو بخونم. «تلاش دوباره» "
            "رو بزن — یا لینک رو کمی بعد دوباره بفرست."
        ),
    },
    "intake.probe_retry_btn": {"en": "🔁 Try again", "fa": "🔁 تلاش دوباره"},
    # The instant menu's one door to the full probed ladder: the rows this link
    # has already produced lead the screen, and this re-checks the link itself.
    "intake.full_menu_btn": {"en": "🔎 All qualities", "fa": "🔎 همه کیفیت‌ها"},
    "intake.probe_still": {
        "en": "❌ Still no qualities to show — try again in a moment.",
        "fa": "❌ باز هم کیفیتی برای نمایش پیدا نشد — کمی بعد دوباره تلاش کن.",
    },
    # The deliberately opt-in fallback row (MENU_AUTO_BEST=1) — labelled as an
    # automatic pick, never as an exact quality.
    "intake.auto_best_btn": {
        "en": "⚡ Automatic (best available)",
        "fa": "⚡ خودکار (بهترین موجود)",
    },
    # Spotify's own streams are DRM'd: the honest thing is to say where the file
    # will really come from *before* the menu is drawn.
    "intake.spotify_note": {
        "en": (
            "ℹ️ Spotify's own streams are DRM-protected — this track is served "
            "from its public YouTube counterpart, and the quality follows that "
            "source."
        ),
        "fa": (
            "ℹ️ استریم‌های خود اسپاتیفای DRM هستند — این آهنگ از نسخهٔ عمومی "
            "یوتیوب آن پخش می‌شود و کیفیت تابع همان منبع است."
        ),
    },
    "intake.spotify_unresolved": {
        "en": (
            "❌ This Spotify track could not be mapped to a downloadable source "
            "right now. Try again in a moment."
        ),
        "fa": (
            "❌ این آهنگ اسپاتیفای الان به منبع قابل دانلودی نرسید. کمی بعد "
            "دوباره تلاش کن."
        ),
    },

    # ---------------------------------------------------------------------
    # Format / quality buttons and their headers
    # ---------------------------------------------------------------------
    "fmt.audio_mp3": {"en": "🎵 Audio — MP3 192k", "fa": "🎵 فقط صدا — MP3 192k"},
    "fmt.audio_m4a": {
        "en": "🎧 Audio — M4A (original, no re-encode)",
        "fa": "🎧 صدا — M4A (اصل، بدون تبدیل)",
    },
    "fmt.media": {"en": "🖼 Send the media of this post", "fa": "🖼 ارسال رسانهٔ این پست"},

    # The two-step audio menu: the container first, then how hard to press its
    # quality knob. The buttons say plain words on purpose — the bitrates behind
    # them are engine detail (services/extractor.py), not something a menu should
    # make anyone learn.
    "fmt.fmt_mp3": {"en": "🎧 MP3", "fa": "🎧 MP3"},
    "fmt.fmt_m4a": {"en": "🎧 M4A", "fa": "🎧 M4A"},
    "fmt.fmt_opus": {"en": "🎧 OPUS", "fa": "🎧 OPUS"},
    "fmt.fmt_wav": {"en": "🎧 WAV", "fa": "🎧 WAV"},
    "fmt.fmt_flac": {"en": "🎧 FLAC", "fa": "🎧 FLAC"},
    # The format's quality presets are labelled with their real bitrates in the
    # menu (handlers/user.py) — these keys name the one preset that is not one.
    "audio.choose_level_fmt": {
        "en": "🎚 Pick the {format} quality 👇",
        "fa": "🎚 کیفیت {format} رو انتخاب کن 👇",
    },
    # The one tier that is not a bitrate: the site's own stream, copied untouched
    # (the 💎 comes from the level marker in handlers/user.py, like every row).
    "audio.original_long": {
        "en": "Original · no re-encode",
        "fa": "کیفیت اصلی · بدون تبدیل",
    },
    # Shown only when the ladder was trimmed to the source's own rate — the rows
    # are the information, this one line is their footnote.
    "audio.source_rate": {
        "en": "ℹ️ Source quality: {rate}",
        "fa": "ℹ️ کیفیت منبع: {rate}",
    },
    # What a bitrate row *is*: an encoding target, not a source-quality promise.
    "audio.converted_note": {
        "en": (
            "ℹ️ Rate rows are conversion targets (the file is encoded at that "
            "rate); \u00abOriginal\u00bb is the source's own stream, untouched."
        ),
        "fa": (
            "ℹ️ ردیف‌های نرخ، هدف تبدیل‌اند (فایل با همان نرخ رمزگذاری می‌شود)؛ "
            "«کیفیت اصلی» همان جریان دست‌نخوردهٔ منبع است."
        ),
    },

    # ---------------------------------------------------------------------
    # The media card: the standard block for anything downloadable — the screen
    # that asks and the caption that arrives are the same four lines, so the
    # answer always names the question. A missing fact omits its line (see
    # services/delivery.py:media_card); "{height}p" and the card lines are
    # language-neutral and live in the catalogue anyway, one home per string.
    # ---------------------------------------------------------------------
    "media.line_title": {"en": "🎬 {title}", "fa": "🎬 {title}"},
    # A song introduces itself as a song: 🎵 its title, 🎤 who made it — the card
    # speaks the content's language, not one generic label for everything.
    "media.line_music": {"en": "🎵 {title}", "fa": "🎵 {title}"},
    "media.line_artist": {"en": "🎤 {artist}", "fa": "🎤 {artist}"},
    "media.line_album": {"en": "💿 {album}", "fa": "💿 {album}"},
    "media.line_duration": {"en": "⏱ {duration}", "fa": "⏱ {duration}"},
    "media.line_url": {"en": "🔗 {url}", "fa": "🔗 {url}"},
    "media.line_quality": {"en": "🎞 {quality}", "fa": "🎞 {quality}"},
    "media.line_audio_quality": {"en": "🎧 {quality}", "fa": "🎧 {quality}"},
    "media.line_bot": {"en": "🤖 {bot}", "fa": "🤖 {bot}"},
    "media.quality_p": {"en": "{height}p", "fa": "{height}p"},
    # The quality line of a copied stream: named honestly, never a bitrate.
    "media.original": {"en": "Original", "fa": "اصلی"},
    # Appended to a quality label when the rate on it is the encoder's target
    # over a weaker source — a true number that could still mislead about
    # quality, said where the label is read (the caption and its cached replay).
    "media.upscale_mark": {
        "en": "from a ≈{source} kbps source",
        "fa": "از منبع ≈{source} kbps",
    },
    # The one compact state a long download shows, appended to the card and
    # removed with it: never a sentence, never a second message.
    "media.wait": {"en": "⏳", "fa": "⏳"},
    "media.progress": {"en": "⏳ {percent:.0f}%", "fa": "⏳ {percent:.0f}%"},
    "media.retry": {"en": "🔄 Try again", "fa": "🔄 دوباره تلاش کن"},
    # On a quality row whose size the source never reported — the resolution is
    # real and stays, and the size says plainly that nobody knows it.
    "media.size_unknown": {"en": "size unknown", "fa": "حجم نامشخص"},

    # ---------------------------------------------------------------------
    # Worker: the download's own narration
    # ---------------------------------------------------------------------
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
    "work.fallback": {
        "en": "🛠 The main route could not serve this link; downloading through the fallback…",
        "fa": "🛠 مسیر اصلی به این لینک دسترسی نداشت؛ از مسیر جایگزین دانلود می‌شود…",
    },
    # The failure lands on the card the user is already watching: what went wrong
    # in their words ({error} is chosen by the engine's error *code* — see
    # core/i18n:error_message), then what to do about it. The technical detail is
    # the log's, never the chat's.
    "work.failed": {
        "en": "❌ Download failed.\n{error}",
        "fa": "❌ دانلود نشد.\n{error}",
    },
    "work.unexpected": {"en": "an unknown problem", "fa": "یه مشکل ناشناخته"},
    "work.internal_error": {
        "en": "Something went wrong on our side.",
        "fa": "یه مشکلی سمت ما پیش اومد.",
    },
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
    "err.OAUTH_REFUSED": {
        "en": "The bot's YouTube login method is misconfigured — an admin has been notified.",
        "fa": "تنظیمات لاگین یوتیوب ربات اشتباه است — ادمین‌ها خبردار شدند.",
    },
    "err.CONVERSION_MISMATCH": {
        "en": "The file that came back is not the format you chose — try another option.",
        "fa": "فایلی که برگشت فرمت انتخابی تو نبود — یک گزینهٔ دیگر رو امتحان کن.",
    },
    "err.FILE_TOO_LARGE": {
        "en": "The finished file is bigger than Telegram accepts here — pick a smaller quality.",
        "fa": "حجم فایل نهایی از سقف آپلود تلگرام بیشتر است — کیفیت کوچک‌تری را انتخاب کن.",
    },
    "err.DELIVERY_FAILED": {
        "en": "Telegram refused the file. Try again in a moment.",
        "fa": "تلگرام فایل را نپذیرفت؛ کمی بعد دوباره تلاش کن.",
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
    "admin.btn_blocks": {"en": "🚫 Blocks", "fa": "🚫 بلاک‌ها"},
    "admin.btn_users": {"en": "👥 Users", "fa": "👥 کاربران"},
    "admin.btn_groups": {"en": "👥 Groups", "fa": "👥 گروه‌ها"},
    "admin.groups_headline": {"en": "👥 Group usage", "fa": "👥 آمار گروه‌ها"},
    "admin.groups_totals": {
        "en": (
            "📥 Total group downloads: {total}\n"
            "✅ Successful: {ok}\n"
            "❌ Failed: {failed}\n"
            "👥 Active groups: {groups}"
        ),
        "fa": (
            "📥 کل دانلودهای گروهی: {total}\n"
            "✅ موفق: {ok}\n"
            "❌ ناموفق: {failed}\n"
            "👥 گروه‌های فعال: {groups}"
        ),
    },
    "admin.groups_top": {"en": "🔥 Top groups", "fa": "🔥 پرکاربردترین گروه‌ها"},
    "admin.groups_line": {
        "en": "{rank}. {name} — {total} downloads · {failed} failed",
        "fa": "{rank}. {name} — {total} دانلود · {failed} ناموفق",
    },
    "admin.groups_unknown": {"en": "Group ID: {id}", "fa": "شناسهٔ گروه: {id}"},
    "admin.groups_empty": {
        "en": "— no group downloads recorded yet.",
        "fa": "— هنوز دانلود گروهی ثبت نشده.",
    },
    "admin.groups_failures": {
        "en": "❌ Group failures: {failed}",
        "fa": "❌ شکست‌های گروهی: {failed}",
    },
    "admin.groups_code": {"en": "{code} — {count}", "fa": "{code} — {count}"},
    # Week-over-week: two whole weeks of the same seven local days, so the two
    # numbers are comparable (see services/panel.py:groups_text).
    "admin.groups_week_title": {
        "en": "📊 This week (7 days)",
        "fa": "📊 هفتهٔ جاری (۷ روز)",
    },
    "admin.groups_week_volume": {
        "en": "⬇️ Downloads: {total}",
        "fa": "⬇️ دانلود: {total}",
    },
    "admin.groups_week_volume_delta": {
        "en": "📈 vs last week: {delta} ({percent})",
        "fa": "📈 نسبت به هفتهٔ قبل: {delta} ({percent})",
    },
    "admin.groups_week_volume_first": {
        "en": "📈 vs last week: no baseline",
        "fa": "📈 نسبت به هفتهٔ قبل: مبنایی نیست",
    },
    "admin.groups_week_fail_rate": {
        "en": "❌ Failure rate: {rate}",
        "fa": "❌ نرخ خطا: {rate}",
    },
    "admin.groups_week_fail_delta": {
        "en": "📉 vs last week: {delta}",
        "fa": "📉 نسبت به هفتهٔ قبل: {delta}",
    },
    "admin.groups_week_fail_first": {
        "en": "📉 vs last week: no baseline",
        "fa": "📉 نسبت به هفتهٔ قبل: مبنایی نیست",
    },
    "admin.btn_system": {"en": "🖥 System", "fa": "🖥 سیستم"},
    "admin.btn_settings": {"en": "⚙️ Settings", "fa": "⚙️ تنظیمات"},
    # Backup & restore — the System submenu's owner-only pair. What may travel
    # in the file is services/backup.py's decision, and says so in the caption.
    "admin.btn_backup": {"en": "💾 Backup", "fa": "💾 پشتیبان"},
    "admin.btn_restore": {"en": "📤 Restore", "fa": "📤 بازگردانی"},
    "admin.owner_only": {
        "en": "Owner only — this action is for the bot owner.",
        "fa": "فقط مالک — این کار فقط برای مالک ربات است.",
    },
    "admin.backup_caption": {
        "en": (
            "💾 Backup — custom texts and settings. Tokens, credentials and "
            "runtime caches are deliberately not in this file."
        ),
        "fa": (
            "💾 پشتیبان — متن‌های سفارشی و تنظیمات. توکن‌ها، اطلاعات ورود و "
            "کش‌های اجرایی عمداً در این فایل نیستند."
        ),
    },
    "admin.restore_prompt": {
        "en": "Send the backup file (JSON, up to 10 MB).",
        "fa": "فایل پشتیبان را بفرستید (JSON، حداکثر ۱۰ مگابایت).",
    },
    "admin.restore_invalid": {
        "en": "This file cannot be restored ({reason}). Send another file, or cancel.",
        "fa": "این فایل بازگردانی نمی‌شود ({reason}). فایل دیگری بفرستید یا لغو کنید.",
    },
    "admin.restore_preview": {
        "en": (
            "📤 Restore preview\n"
            "\n"
            "📝 Custom texts: <b>{texts}</b>\n"
            "⚙️ Settings: <b>{settings}</b>\n"
            "🕓 Created: <code>{created}</code>\n"
            "\n"
            "Applying replaces the current texts and settings with this "
            "file's. An emergency backup of the current state is sent first."
        ),
        "fa": (
            "📤 پیش‌نمایش بازگردانی\n"
            "\n"
            "📝 متن‌های سفارشی: <b>{texts}</b>\n"
            "⚙️ تنظیمات: <b>{settings}</b>\n"
            "🕓 ساخته‌شده: <code>{created}</code>\n"
            "\n"
            "با ادامه، متن‌ها و تنظیمات فعلی با محتوای این فایل جایگزین می‌شود. "
            "پیش از اجرا، یک پشتیبان اضطراری از وضعیت فعلی فرستاده خواهد شد."
        ),
    },
    "admin.restore_confirm_btn": {"en": "✅ Restore now", "fa": "✅ بازگردانی"},
    "admin.restore_cancel_btn": {"en": "❌ Cancel", "fa": "❌ لغو"},
    "admin.restore_emergency_caption": {
        "en": (
            "⚠️ Emergency backup — the state before the restore. Keep it: "
            "with it, the restore can be undone."
        ),
        "fa": (
            "⚠️ پشتیبان اضطراری — وضعیت پیش از بازگردانی. نگهش دارید: با آن "
            "می‌توان بازگردانی را برگرداند."
        ),
    },
    "admin.restore_done": {
        "en": (
            "✅ Restored <b>{texts}</b> custom text(s) and <b>{settings}</b> "
            "setting(s). Every process speaks the new texts now."
        ),
        "fa": (
            "✅ بازگردانی انجام شد: <b>{texts}</b> متن سفارشی و <b>{settings}</b> "
            "تنظیم. همهٔ فرایندها همین حالا متن‌های جدید را می‌خوانند."
        ),
    },
    "admin.restore_failed": {
        "en": "The restore failed and was rolled back — nothing changed.\n<code>{detail}</code>",
        "fa": "بازگردانی ناموفق بود و برگشت داده شد — چیزی تغییر نکرد.\n<code>{detail}</code>",
    },
    "admin.restore_aborted": {
        "en": (
            "The restore was stopped before applying — nothing changed.\n"
            "<code>{detail}</code>"
        ),
        "fa": (
            "بازگردانی پیش از اجرا متوقف شد — چیزی تغییر نکرد.\n"
            "<code>{detail}</code>"
        ),
    },
    "admin.restore_sync_failed": {
        "en": (
            "✅ The restore was applied and committed.\n"
            "⚠️ Refreshing the text cache failed — some processes may show "
            "old texts until they reload. Nothing was rolled back.\n"
            "<code>{detail}</code>"
        ),
        "fa": (
            "✅ بازگردانی اعمال و ثبت شد.\n"
            "⚠️ تازه‌سازی کش متن ناموفق بود — ممکن است بعضی فرایندها تا بارگذاری "
            "دوباره متن‌های قدیمی را نشان دهند. هیچ چیزی برگشت داده نشد.\n"
            "<code>{detail}</code>"
        ),
    },
    "admin.restore_expired": {
        "en": (
            "This confirmation is no longer valid (used, expired, or not "
            "yours) — start the restore again."
        ),
        "fa": (
            "این تأیید دیگر معتبر نیست (استفاده‌شده، منقضی یا متعلق به شما نیست) "
            "— بازگردانی را دوباره آغاز کنید."
        ),
    },
    "admin.restore_cancelled": {"en": "Restore cancelled.", "fa": "بازگردانی لغو شد."},
    # The panel's category submenus — the hub lists these six, one screen deep
    # from each (see handlers/admin.py).
    "admin.cat_users": {"en": "👥 Users and groups", "fa": "👥 کاربران و گروه‌ها"},
    "admin.cat_downloads": {
        "en": "📥 Downloads and media",
        "fa": "📥 دانلودها و رسانه",
    },
    "admin.cat_sources": {
        "en": "🌐 Sources and extractors",
        "fa": "🌐 منابع و استخراج‌گرها",
    },
    "admin.cat_messages": {
        "en": "\u2709\ufe0f Messages and localization",
        "fa": "\u2709\ufe0f پیام‌ها و زبان",
    },
    "admin.cat_system": {
        "en": "🖥 System and configuration",
        "fa": "🖥 سیستم و پیکربندی",
    },
    "admin.cat_diagnostics": {
        "en": "🛠 Diagnostics and maintenance",
        "fa": "🛠 عیب‌یابی و نگهداری",
    },
    "admin.btn_home": {"en": "🏠 Home", "fa": "🏠 خانه"},
    "admin.btn_texts": {"en": "📝 Bot texts", "fa": "📝 متن‌های ربات"},
    "admin.btn_sources": {"en": "🌐 Sources status", "fa": "🌐 وضعیت منابع"},
    "admin.texts_reason_locked": {
        "en": "that text is internal or operator-facing and cannot be edited",
        "fa": "آن متن داخلی یا مخصوص اپراتور است و قابل ویرایش نیست",
    },
    "admin.sources_head": {
        "en": "🌐 <b>Sources &amp; extractors</b>",
        "fa": "🌐 <b>منابع و استخراج‌گرها</b>",
    },
    "admin.sources_tools": {
        "en": "🧰 <code>/fixlogin</code> — guided cookie login · <code>/oauth</code> — Smart-TV login",
        "fa": "🧰 <code>/fixlogin</code> — راهنمای لاگین کوکی · <code>/oauth</code> — لاگین اسمارت‌تی‌وی",
    },
    # --- The editable-texts screens (admin only) --------------------------
    "admin.texts_title": {
        "en": "📝 <b>Bot texts</b> — pick a category:",
        "fa": "📝 <b>متن‌های ربات</b> — یک دسته را انتخاب کن:",
    },
    "admin.texts_category": {
        "en": "📝 <b>{category}</b> — pick a text to edit:",
        "fa": "📝 <b>{category}</b> — متن مورد نظر را انتخاب کن:",
    },
    "admin.texts_key_title": {
        "en": "🔤 <code>{key}</code>\n\n🇬🇧 {en}\n\n🇮🇷 {fa}",
        "fa": "🔤 <code>{key}</code>\n\n🇬🇧 {en}\n\n🇮🇷 {fa}",
    },
    "admin.texts_edit_en": {"en": "✏️ Edit EN", "fa": "✏️ ویرایش EN"},
    "admin.texts_edit_fa": {"en": "✏️ Edit FA", "fa": "✏️ ویرایش FA"},
    "admin.texts_reset_en": {"en": "🔄 Reset EN", "fa": "🔄 بازنشانی EN"},
    "admin.texts_reset_fa": {"en": "🔄 Reset FA", "fa": "🔄 بازنشانی FA"},
    "admin.texts_preview": {"en": "👁 Preview", "fa": "👁 پیش‌نمایش"},
    "admin.texts_prompt": {
        "en": (
            "🔤 Send the new text for <code>{key}</code> ({lang}). Keep every "
            "placeholder exactly as the default has it."
        ),
        "fa": (
            "🔤 متن جدید <code>{key}</code> ({lang}) را بفرست. همهٔ جای‌نگهدارها "
            "را دقیقاً مثل متن پیش‌فرض نگه دار."
        ),
    },
    "admin.texts_saved": {
        "en": "✅ Saved <code>{key}</code> ({lang}).",
        "fa": "✅ <code>{key}</code> ({lang}) ذخیره شد.",
    },
    "admin.texts_reset_done": {
        "en": "🔄 <code>{key}</code> ({lang}) is back to its default.",
        "fa": "🔄 <code>{key}</code> ({lang}) به حالت پیش‌فرض برگشت.",
    },
    "admin.texts_invalid": {"en": "❌ Not saved — {reason}", "fa": "❌ ذخیره نشد — {reason}"},
    "admin.texts_reason_markup": {
        "en": "the HTML markup is unbalanced or uses unsupported tags",
        "fa": "چینش HTML نامتوازن است یا برچسب‌های پشتیبانی‌نشده دارد",
    },
    "admin.texts_reason_placeholder": {
        "en": "it introduces a placeholder the default does not have",
        "fa": "جای‌نگهداری دارد که متن پیش‌فرض ندارد",
    },
    "admin.texts_reason_length": {
        "en": "it is longer than Telegram messages allow",
        "fa": "طولانی‌تر از حد مجاز پیام‌های تلگرام است",
    },
    # The text categories — a menu of features, never a wall of keys.
    "admin.textcat_start": {"en": "Start & menu", "fa": "شروع و منو"},
    "admin.textcat_language": {"en": "Language", "fa": "زبان"},
    "admin.textcat_intake": {"en": "Link intake", "fa": "دریافت لینک"},
    "admin.textcat_media": {
        "en": "Media & quality menus",
        "fa": "منوهای رسانه و کیفیت",
    },
    "admin.textcat_download": {
        "en": "Download & progress",
        "fa": "دانلود و پیشرفت",
    },
    "admin.textcat_errors": {
        "en": "Errors & verification",
        "fa": "خطاها و راستی‌آزمایی",
    },
    "admin.textcat_profile": {"en": "Profile & premium", "fa": "پروفایل و ویژه"},
    "admin.textcat_pay": {"en": "Payments", "fa": "پرداخت‌ها"},
    "admin.btn_reload": {"en": "🔄 Refresh", "fa": "🔄 تازه‌سازی"},
    "admin.btn_search": {"en": "🔎 Search", "fa": "🔎 جستجو"},
    "admin.btn_prev": {"en": "◀️ Prev", "fa": "◀️ قبلی"},
    "admin.btn_next": {"en": "▶️ Next", "fa": "▶️ بعدی"},
    "admin.users": {
        "en": (
            "👥 <b>Users</b>\n"
            "\n"
            "👥 Total: <b>{users}</b> · 💎 VIP: {premium}\n"
            "🐣 New today: {new_users} · ⚡️ Active today: {active_today}\n"
            "🌐 Language mix: {languages}\n"
            "\n"
            "{listing}"
        ),
        "fa": (
            "👥 <b>کاربران</b>\n"
            "\n"
            "👥 همه: <b>{users}</b> · 💎 ویژه: {premium}\n"
            "🐣 امروز تازه: {new_users} · ⚡️ فعال امروز: {active_today}\n"
            "🌐 ترکیب زبان‌ها: {languages}\n"
            "\n"
            "{listing}"
        ),
    },
    "admin.users_head": {
        "en": "🗂 Newest accounts ({shown} of {total}):",
        "fa": "🗂 تازه‌ترین حساب‌ها ({shown} از {total}):",
    },
    "admin.users_line": {
        "en": "• <code>{telegram_id}</code> {username} · {language} · {joined}",
        "fa": "• <code>{telegram_id}</code> {username} · {language} · {joined}",
    },
    "admin.users_empty": {"en": "— no accounts yet.", "fa": "— هنوز حسابی نیست."},
    "admin.users_vip_mark": {"en": "💎", "fa": "💎"},
    "admin.users_search_prompt": {
        "en": "🔎 Send an <code>@username</code> or a Telegram id:",
        "fa": "🔎 یک <code>@username</code> یا شناسهٔ تلگرام بفرست:",
    },
    "admin.users_search_title": {
        "en": "🔎 <b>Search:</b> <code>{query}</code> — {count} hit(s)",
        "fa": "🔎 <b>جستجو:</b> <code>{query}</code> — {count} نتیجه",
    },
    "admin.users_search_none": {
        "en": "🔎 Nobody matches <code>{query}</code>.",
        "fa": "🔎 کسی با «<code>{query}</code>» پیدا نشد.",
    },
    "admin.trend_headline": {
        "en": "Failure trend over the last {days} days",
        "fa": "روند {days} روزهٔ شکست‌ها",
    },
    "admin.blocks_headline": {
        "en": "Failure digest — last {days} days",
        "fa": "گزارش {days} روزهٔ شکست‌ها",
    },
    "admin.title": {"en": "🛠 <b>Admin panel</b>", "fa": "🛠 <b>پنل مدیریت</b>"},
    "admin.subtitle": {
        "en": "Everything below is read from the running stack; the buttons are the tools.",
        "fa": "همه‌چیز از استک در حال اجرا خوانده می‌شود؛ دکمه‌ها ابزارها هستند.",
    },
    "admin.btn_stats": {"en": "📊 Statistics", "fa": "📊 آمار"},
    "admin.btn_health": {"en": "🩺 Health", "fa": "🩺 سلامت"},
    "admin.btn_queue": {"en": "🕒 Queue", "fa": "🕒 صف"},
    "admin.btn_tools": {"en": "🔧 Tools", "fa": "🔧 ابزارها"},
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
    "panel.cobalt_pool": {
        "en": "     🔁 {count} instances, in order: {nodes}",
        "fa": "     🔁 {count} نمونه، به ترتیب: {nodes}",
    },
    "panel.cobalt_node": {
        "en": "{url} ({dialect} • {state})",
        "fa": "{url} ({dialect} • {state})",
    },
    "panel.node_active": {"en": "in use", "fa": "در حال استفاده"},
    "panel.node_quarantined": {"en": "set aside", "fa": "کنار گذاشته"},
    "panel.node_standby": {"en": "standby", "fa": "ذخیره"},
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
            "🛠 <b>Maintenance</b>\n"
            "\n"
            "• 🩺 Doctor — one live probe of the whole YouTube path: one verdict and "
            "the next fix worth trying.\n"
            "• ♻️ Cookie export — read a browser profile into the jar, right now.\n"
            "\n"
            "Everything here is on call as commands too: "
            "/doctor /refresh /fixlogin /oauth /broadcast."
        ),
        "fa": (
            "🛠 <b>نگهداری</b>\n"
            "\n"
            "• 🩺 دکتر — یک پروب زنده از کل مسیر یوتیوب: یک حکم و قدم بعدی که ارزش "
            "امتحان دارد.\n"
            "• ♻️ اکسپورت کوکی — خواندن پروفایل مرورگر داخل جار، همین حالا.\n"
            "\n"
            "همهٔ این‌ها به‌صورت دستور هم کار می‌کنند: "
            "/doctor /refresh /fixlogin /oauth /broadcast."
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
