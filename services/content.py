"""What is behind a link — decided *before* the buttons are drawn.

The gateway cannot afford a real probe: yt-dlp metadata for every link would put a
network round trip (and on a flagged host, a block) in front of the one question the
user is waiting to answer. It does not need one either, because the *shape* of a link
already tells most of the story: a Spotify track is audio, a TikTok ``/photo/`` post
is a set of images, a YouTube watch page is a video, and a Twitter status is whatever
its author put in it.

So this module is a table, not a guesser, and it is honest about the difference:

* a **confident** kind (video / audio / image / gallery) hides the choices that make
  no sense — no quality menu for a photo post, no photo option for a YouTube video;
* an **ambiguous** link (``x.com/…/status/…``, a Reddit thread, anything unknown) is
  ``media``: the bot offers "send whatever the post has" plus the audio formats,
  because promising a quality tier for a post that might be a single picture is how
  a menu starts lying.

The file still decides how something is *delivered* (see ``services/delivery.py``);
this only decides what can be *asked for*.

One kind of link is never asked about at all: the ones with a single possible answer
(a photo post has no tier to pick and no audio to extract). :attr:`Routing.solo`
reports those, so the gateway queues them instead of drawing a one-button menu — see
``handlers/user.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlparse

from core.utils import (
    AUDIO_FORMATS,
    AUDIO_LEVELS,
    AUDIO_TIERS,
    MediaFormat,
    Quality,
)

#: What the link most likely holds.
ContentKind = Literal["video", "audio", "image", "gallery", "media"]

#: The platform family a link belongs to, for the Downloads screen's sections.
#: ``other`` is any claimed host outside the four named sections — the menu
#: still serves those links, it just does not give them a section of their own.
Platform = Literal["youtube", "instagram", "spotify", "tiktok", "other"]

#: One button: a label key in the catalogue, and the request it stands for.
@dataclass(frozen=True)
class Choice:
    """A format/quality option offered for a link, in button order."""

    label_key: str
    media_format: MediaFormat
    quality: Quality


#: Host fragment → platform section. Read with the same suffix rule as the
#: kind tables (``music.youtube.com`` is YouTube, ``vm.tiktok.com`` TikTok).
_PLATFORM_HOSTS: tuple[tuple[tuple[str, ...], Platform], ...] = (
    (("youtube.com", "youtu.be", "youtube-nocookie.com"), "youtube"),
    (("instagram.com", "cdninstagram.com"), "instagram"),
    (("spotify.com", "spotify.link"), "spotify"),
    (("tiktok.com",), "tiktok"),
)

#: The four Downloads sections, in screen order, with the link shapes each one
#: names. A section promises *shapes the bot accepts*, never downloadability —
#: the engines and the fallback still get the final word on every link.
PLATFORM_SECTIONS: tuple[tuple[Platform, tuple[str, ...]], ...] = (
    ("youtube", ("watch", "playlist", "shorts")),
    ("instagram", ("reel", "post", "story")),
    ("spotify", ("track", "album", "playlist")),
    ("tiktok", ("video",)),
)


#: The four section names, in screen order — the *names*, for callers that only
#: need the vocabulary (the admin toggles, the intake gate).
PLATFORM_NAMES: tuple[Platform, ...] = tuple(name for name, _shapes in PLATFORM_SECTIONS)

#: The sample link each section screen shows (``Example: …``). Real and taken:
#: every one of these is a link this bot would download, spelled the way the
#: site's own share sheet spells it — a sample that does not resolve teaches the
#: wrong shape to the one user who copies it (`tests/test_content_routing.py`
#: pins each one against :func:`platform_for` and :func:`classify`).
PLATFORM_EXAMPLES: dict[Platform, str] = {
    "youtube": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "instagram": "https://www.instagram.com/reel/CxYzAbCdEfG/",
    "spotify": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    "tiktok": "https://www.tiktok.com/@username/video/7200000000000000000",
}


#: The badge a shape wears in a section screen — the same pictures the questions
#: under it use (🎬 a video, 🖼 photos, 💿 an album). Keyed by ``(platform, shape)``
#: rather than by the word alone, because one word means two things: a ``playlist``
#: is video on YouTube and music on Spotify. ``tests/test_content_routing.py`` pins
#: this table against ``PLATFORM_SECTIONS``, so a promised shape cannot ship bare.
SHAPE_ICONS: dict[tuple[str, str], str] = {
    ("youtube", "watch"): "🎬",
    ("youtube", "playlist"): "🎬",
    ("youtube", "shorts"): "🎬",
    ("instagram", "reel"): "🎬",
    ("instagram", "post"): "🖼️",
    ("instagram", "story"): "📸",
    ("spotify", "track"): "🎵",
    ("spotify", "album"): "💿",
    ("spotify", "playlist"): "🎵",
    ("tiktok", "video"): "🎬",
}

#: What a shape wears when nobody gave it an icon (never drawn today, see the pin
#: above — a plain link is a worse badge than an emoji, and a *missing* one here
#: would be a crash in a menu).
_SHAPE_FALLBACK = "🔗"


def shape_label(platform: str, shape: str) -> str:
    """One promised shape with its badge: ``reel`` → ``🎬 reel``."""
    return f"{SHAPE_ICONS.get((platform, shape), _SHAPE_FALLBACK)} {shape}"


def _switched_off(disabled: Iterable[object]) -> frozenset[str]:
    """The disabled names as a comparable set (trimmed, lowercased, blanks gone)."""
    return frozenset(
        name.strip().lower()
        for name in disabled
        if isinstance(name, str) and name.strip()
    )


def visible_sections(
    disabled: Iterable[object] = (),
) -> tuple[tuple[Platform, tuple[str, ...]], ...]:
    """The sections a picker shows: the four, minus the switches an admin turned off.

    One function for the menu, the intake gate and the admin screen, so the three can
    never disagree about what is on — and an empty tuple means every section is off,
    which the screens say out loud rather than quietly resurrecting the four.
    """
    off = _switched_off(disabled)
    return tuple((name, shapes) for name, shapes in PLATFORM_SECTIONS if name not in off)


def platform_for(url: str) -> Platform:
    """Which Downloads section this link belongs to (``other`` when none)."""
    host = url_host(url)
    for needles, platform in _PLATFORM_HOSTS:
        if any(_matches(host, needle) for needle in needles):
            return platform
    return "other"


def platform_is_on(url: str, disabled: Iterable[object] = ()) -> bool:
    """Whether the section this link belongs to is open (``other`` always is).

    The gate the intake reads: a link from a switched-off section is answered with
    the truth instead of being served — a switch that only hid a button would be
    cosmetic.
    """
    platform = platform_for(url)
    return platform == "other" or platform not in _switched_off(disabled)


@dataclass(frozen=True)
class Routing:
    """What to ask a user about this link: the header line and the buttons under it."""

    kind: ContentKind
    platform: Platform
    header_key: str
    #: Every request that may legitimately finish this link's flow — the menu's
    #: vocabulary, and what ``find_choice`` accepts.
    choices: tuple[Choice, ...]
    #: The "send this post's own media" button, for links that *are* posts.
    media_choice: Choice | None = None
    #: The two-step audio menu: which formats deserve a button here (empty when
    #: the link cannot produce audio at all).
    audio_formats: tuple[str, ...] = ()

    @property
    def solo(self) -> Choice | None:
        """The only answer this link has, when asking would be busywork.

        A photo post has exactly one possible request — "send its media" — because
        a gallery has no quality tier and no audio to extract. A menu of one honest
        button is still a menu: it costs a round trip and reads as if the bot had
        forgotten the other options. But one video *tier* is not a solo answer:
        «download it» on an unprobed link is a fallback row, and the question is
        still where real resolutions may appear. ``None`` means ask.
        """
        if len(self.choices) != 1:
            return None
        only = self.choices[0]
        if only.media_format == "video" and only != _MEDIA_CHOICE:
            return None
        return only


#: Video quality tiers, best first — the order a person thinks in.
#: One deliberate tap when a link tells us nothing: «download it». The engine
#: picks within its senses and the caption names what actually arrived — a ladder
#: invented here would be "up to" theatre with no facts behind it.
_VIDEO_CHOICES: tuple[Choice, ...] = (Choice("menu.download", "video", "best"),)

#: The audio menu is two taps deep on purpose: first the *container* the user
#: wants to receive (a file named .mp3 is a different promise than one named
#: .m4a), then — for the codecs that have a quality knob — how hard to press it.
#: The button labels stay plain words ("Best", "Small size"); the bitrates are an
#: engine detail (services/extractor.py) that no menu should make anyone learn.
AUDIO_FORMAT_LABELS: dict[str, str] = {
    "mp3": "fmt.fmt_mp3",
    "m4a": "fmt.fmt_m4a",
    "opus": "fmt.fmt_opus",
    "flac": "fmt.fmt_flac",
    "wav": "fmt.fmt_wav",
}


def audio_tier_levels(codec: str) -> tuple[tuple[str, Quality], ...]:
    """``((level, tier), ...)`` for a codec, best first — ``()`` for e.g. wav.

    The one place the level→tier map is read, so the menu that renders the tiers
    and the vocabulary that validates the taps share both order and spelling.
    """
    return tuple(
        (level, AUDIO_TIERS[(codec, level)])
        for level in AUDIO_LEVELS
        if (codec, level) in AUDIO_TIERS
    )


def audio_level_choices(codec: str) -> tuple[Choice, ...]:
    """The presets a codec genuinely has, best first (``()`` for e.g. wav).

    Validation vocabulary: what a tap may ask for. The *labels* are deliberately
    not here — a level row names its real bitrate and estimated size
    (``handlers/user.py:_level_keyboard``) — so the tuple carries the format's
    own key and no screen ever shows it.
    """
    return tuple(
        Choice(AUDIO_FORMAT_LABELS[codec], "audio", tier)
        for _level, tier in audio_tier_levels(codec)
    )


def _audio_choices() -> tuple[Choice, ...]:
    """Every audio request accepted for a link, format by format, best first.

    A format with no presets (wav, flac — raw and lossless output) *is* its own
    request: its button submits directly and it appears here by its own name.
    """
    per_format = (
        choice for fmt in AUDIO_FORMATS for choice in audio_level_choices(fmt)
    )
    # The knob-less formats *are* their own request — spelled as literals so the
    # tier type stays honest (the routing tests pin both).
    direct = (
        Choice(AUDIO_FORMAT_LABELS[fmt], "audio", fmt)
        for fmt in ("flac", "wav")
    )
    return (*per_format, *direct)


_AUDIO_CHOICES: tuple[Choice, ...] = _audio_choices()

#: Everything else: "send the media of this post" is a *video* request to the engine,
#: because that is the request that fetches whatever the post holds — and the image
#: case is served by the fallback engine and delivered as photos either way.
_MEDIA_CHOICE = Choice("fmt.media", "video", "best")

_MEDIA_CHOICES: tuple[Choice, ...] = (_MEDIA_CHOICE, *_AUDIO_CHOICES)

#: Kinds whose links can yield audio, and therefore get the format buttons.
_AUDIO_MENU_KINDS: frozenset[str] = frozenset({"audio", "media"})

#: Extensions that *are* the answer. A URL ending in ``.jpg`` is not a page that
#: might contain a photo — it is the photo, whatever host serves it. This is the
#: strongest evidence available, so it is read before every host and path rule: the
#: links people actually copy are often the file itself (a Discord CDN URL, a
#: ``preview.redd.it`` image, a ``pbs.twimg.com`` one), and those are the links a
#: format menu is most obviously wrong for. ``.gif`` is here rather than under
#: video because Telegram plays it as an animation and YouTube cannot download it.
_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".avif",
        ".heic",
        ".heif",
        ".bmp",
        ".tif",
        ".tiff",
        ".gif",
    }
)
_VIDEO_SUFFIXES: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}
)
#: Audio files are *not* an auto-download: a file that is already a track is exactly
#: the case where the m4a/"as it is" distinction is worth a question.
_AUDIO_SUFFIXES: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg", ".aac"}
)

#: Host → kind, for the hosts where the host alone settles it.
#: ``pbs.twimg.com`` and friends are *CDNs*: everything they serve is media, so no
#: path rule has to guess. ``video.twimg.com`` is deliberately absent — its URLs do
#: name their file (``…/ext_tw_video/…/pu/…mp4``), and the suffix rule reads that
#: better than a host rule could.
_HOST_KINDS: tuple[tuple[tuple[str, ...], ContentKind], ...] = (
    (
        (
            "youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
            "vimeo.com",
            "twitch.tv",
            "dailymotion.com",
        ),
        "video",
    ),
    (
        (
            "spotify.com",
            "spotify.link",
            "soundcloud.com",
            "bandcamp.com",
            "music.apple.com",
            "deezer.com",
        ),
        "audio",
    ),
    (("pinterest.com", "imgur.com", "flickr.com", "500px.com"), "image"),
    (
        (
            "pbs.twimg.com",
            "cdninstagram.com",
            "fbcdn.net",
            "i.redd.it",
            "preview.redd.it",
            "external-preview.redd.it",
            "pinimg.com",
            "i.gyazo.com",
            "media.tenor.com",
            "c.tenor.com",
            "images.unsplash.com",
            "images.pexels.com",
            "i.ibb.co",
            "upload.wikimedia.org",
            "staticflickr.com",
        ),
        "image",
    ),
)

#: Path fragments that speak for *any* host (checked in order). Deliberately
#: generic — ``/watch`` or ``/shorts/`` would be a claim about a website from a path
#: only YouTube uses, and a host that merely *looks* like YouTube would inherit it.
#: The hosts that really use those paths are listed in ``_HOST_PATHS`` instead.
_PATH_KINDS: tuple[tuple[tuple[str, ...], ContentKind], ...] = (
    # Plural/collective shapes first: ``/photos/`` is a set even though it starts
    # with ``/photo``, and a set is a gallery on every host that uses the word.
    (("/photo/", "/photos/", "/photos", "/gallery", "/album/", "/albums"), "gallery"),
    (("/photo", "/image", "/images", "/img/", "/picture", "/pics/"), "image"),
    (("/video/", "/videos/", "/embed/", "/reel/"), "video"),
    (("/track/", "/playlist/", "/sets/"), "audio"),
)

#: Host → path fragments *that host* uses, checked before the generic ones. A post
#: URL is host-specific syntax (``/p/`` means a post on instagram and a blog page
#: elsewhere), so it is only read where it means what it looks like.
_HOST_PATHS: dict[str, tuple[tuple[tuple[str, ...], ContentKind], ...]] = {
    "youtube.com": (
        (("/shorts/", "/embed/", "/watch", "/live/"), "video"),
        (("/playlist",), "video"),
    ),
    # ``/share/…`` is deliberately absent: that link can be a reel or a post, and a
    # share link is the one case where the question (quality? audio?) is still worth
    # asking. ``/p/`` and ``/stories`` cannot be anything but media.
    "instagram.com": (
        (("/p/", "/stories"), "gallery"),
        (("/reel", "/tv/"), "video"),
    ),
    "tiktok.com": ((("/photo/",), "gallery"), (("/video/",), "video")),
    "facebook.com": (
        (("/photo",), "image"),
        (("/watch", "/videos/", "/reel", "/video/"), "video"),
    ),
    # No trailing slash on ``/photo``: the share button of a multi-photo post hands
    # out ``…/status/123/photo/1``, but the *app* link is ``…/status/123/photo`` —
    # and a menu is equally wrong for both.
    "twitter.com": ((("/photo",), "image"), (("/video/",), "video")),
    "x.com": ((("/photo",), "image"), (("/video/",), "video")),
    "reddit.com": (
        (("/gallery/",), "gallery"),
        # ``reddit.com/media?url=…`` is a redirect to the image itself.
        (("/media",), "image"),
    ),
    "pinterest.com": ((("/pin/",), "image"),),
}

#: Hosts whose *posts* are one thing or another depending on the author.
_AMBIGUOUS_HOSTS: tuple[str, ...] = (
    "x.com",
    "twitter.com",
    "reddit.com",
    "redd.it",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "tiktok.com",
    "t.me",
    "mastodon",
    "threads.net",
    "linkedin.com",
)

_HEADERS: dict[ContentKind, str] = {
    "video": "intake.choose_quality",
    "audio": "intake.choose_audio",
    "image": "intake.choose_media",
    "gallery": "intake.choose_media",
    "media": "intake.choose_what",
}

_CHOICES: dict[ContentKind, tuple[Choice, ...]] = {
    "video": _VIDEO_CHOICES,
    "audio": _AUDIO_CHOICES,
    "image": (_MEDIA_CHOICE,),
    "gallery": (_MEDIA_CHOICE,),
    "media": _MEDIA_CHOICES,
}


def _query_format(query: str) -> str:
    """The extension a CDN puts in the query when it has none in the path.

    ``pbs.twimg.com/media/ABC?format=jpg&name=large`` is what X's own share button
    produces — the URL has no suffix at all, so the only place the answer lives is
    ``format=``. Read from exactly that parameter (not ``fmt``/``ext``, which pages
    use for unrelated things) and only when the path says nothing.
    """
    for key, value in parse_qsl(query, keep_blank_values=False):
        if key.lower() == "format" and value:
            return f".{value.strip().strip('.').lower()}"
    return ""


def _file_kind(path: str, query: str = "") -> ContentKind | None:
    """What a link *is*, when it names a file — the strongest evidence there is."""
    suffix = Path(unquote(path or "")).suffix.lower()
    if not suffix and query:
        suffix = _query_format(query)
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    if suffix in _AUDIO_SUFFIXES:
        return "audio"
    return None


def unwrap_media_url(url: str) -> str:
    """The file a viewer/wrapper link names in one of its parameters, or itself.

    Reddit's share links land on ``reddit.com/media?url=…`` — a media *viewer*
    page no extractor handles, while the URL inside its parameter is a plain
    ``i.redd.it/….jpeg`` file that downloads as-is. The wrapper says so out loud:
    an absolute URL to a media file, in a parameter. Read only that (a path with
    its own file suffix is already the real thing, and ``format=`` means a
    different thing on CDNs — see ``_query_format``).
    """
    parsed = urlparse(url or "")
    if _file_kind(parsed.path, parsed.query) is not None:
        return url
    for _key, value in parse_qsl(parsed.query, keep_blank_values=False):
        candidate = value.strip()
        if not candidate.startswith(("http://", "https://")):
            continue
        inner = urlparse(candidate)
        if inner.netloc and _file_kind(inner.path, inner.query) is not None:
            return candidate
    return url


def url_host(url: str) -> str:
    """The lowercased host of a link, without credentials or port (``""`` if none).

    Credentials first, then the port — ``user:pass@host`` carries a colon of its own.
    """
    netloc = urlparse(url or "").netloc.lower()
    return netloc.rpartition("@")[2].partition(":")[0]


def _matches(host: str, needle: str) -> bool:
    """Host-suffix match. A dotted needle is a domain (``x.com`` and ``www.x.com``);
    anything else is a fragment of the host (``mastodon`` covers every instance)."""
    if "." in needle:
        return host == needle or host.endswith(f".{needle}")
    return needle in host


def _host_kind(host: str) -> ContentKind | None:
    for hosts, kind in _HOST_KINDS:
        if any(_matches(host, candidate) for candidate in hosts):
            return kind
    return None


def _path_kind(path: str, host: str = "") -> ContentKind | None:
    lowered = path.lower()
    for known, rules in _HOST_PATHS.items():
        if not _matches(host, known):
            continue
        for fragments, kind in rules:
            if any(fragment in lowered for fragment in fragments):
                return kind
        return None  # a known host with no matching path stays ambiguous
    for fragments, kind in _PATH_KINDS:
        if any(fragment in lowered for fragment in fragments):
            return kind
    return None


def knows_host(url: str) -> bool:
    """Whether this bot claims the link's host — the router has an opinion on it.

    A claimed host is supported whatever one engine's URL catalogue says: the
    engines and the fallback still get the final word on a link, but
    "unsupported" is for links nobody here recognizes.
    """
    return _host_kind(url_host(url)) is not None


#: The platforms this bot advertises, in every shape they come in — not just the
#: main host. `redd.it` and `v.redd.it` are Reddit as much as `reddit.com` is,
#: and a short or CDN link is exactly the shape a strict URL catalogue misses.
#: Being listed here promises nothing about the download: the engines and the
#: fallback still get the final word (and their real diagnosis reaches the user
#: if both fail) — it only keeps a recognized platform out of the dead end where
#: the intake gate brands its own supported platforms "unsupported".
PLATFORM_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "music.youtube.com",
        "instagram.com",
        "tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "x.com",
        "twitter.com",
        "t.co",
        "facebook.com",
        "fb.watch",
        "reddit.com",
        "redd.it",
        "v.redd.it",
        "i.redd.it",
        "vimeo.com",
        "soundcloud.com",
        "dailymotion.com",
        "twitch.tv",
        "bilibili.com",
        "b23.tv",
        "vk.com",
    }
)


def claims_platform(url: str) -> bool:
    """Whether the link's host belongs to a platform this bot advertises.

    Family-wise: ``old.reddit.com`` is reddit.com, and a bare short link is the
    platform's own front door.
    """
    host = url_host(url)
    return any(host == name or host.endswith(f".{name}") for name in PLATFORM_HOSTS)


def classify(url: str) -> ContentKind:
    """The kind of media this link most likely holds.

    Path beats host when it has an opinion (a TikTok ``/photo/`` link is a gallery on
    a host that is otherwise video), the host decides the rest, and an unknown host —
    or an ambiguous post — is ``media``: the choice that promises nothing and
    delivers whatever is there.
    """
    parsed = urlparse(url or "")
    host = url_host(url)
    path = parsed.path or ""
    if not host:
        return "media"
    if (file_kind := _file_kind(path, parsed.query)) is not None:
        # A file beats every other rule: `/photo/123.jpg` is a photo, not a gallery
        # page, and `movie.mp4` on a host whose posts are ambiguous is a video.
        return file_kind
    path_kind = _path_kind(path, host)
    if path_kind is not None:
        # Only trust the path when the host does not *contradict* it: a YouTube
        # ``/watch`` is video because YouTube says so, not because of the fragment.
        host_kind = _host_kind(host)
        if host_kind is None or host_kind == path_kind:
            return path_kind
    if host_kind := _host_kind(host):
        return host_kind
    if any(_matches(host, candidate) for candidate in _AMBIGUOUS_HOSTS):
        return "media"
    return "media"


#: Spotify serves genuine audio only: the mapped YouTube stand-in could produce
#: anything, but the song the user asked for is an MP3 or a FLAC — never a video
#: rung, never a dummy row. m4a/opus/wav are conversions of a conversion here,
#: and the menu does not offer what the song is not.
SPOTIFY_AUDIO_FORMATS: tuple[str, ...] = ("mp3", "flac")


def routing_for(url: str) -> Routing:
    """The header and the buttons this link deserves."""
    kind = classify(url)
    platform = platform_for(url)
    if platform == "spotify" and kind in _AUDIO_MENU_KINDS:
        audio_formats: tuple[str, ...] = SPOTIFY_AUDIO_FORMATS
    else:
        audio_formats = AUDIO_FORMATS if kind in _AUDIO_MENU_KINDS else ()
    return Routing(
        kind=kind,
        platform=platform,
        header_key=_HEADERS[kind],
        choices=_CHOICES[kind],
        media_choice=_MEDIA_CHOICE if kind == "media" else None,
        audio_formats=audio_formats,
    )


def find_choice(url: str, media_format: str, quality: str) -> Choice | None:
    """The offered choice matching a callback, or ``None`` if it was never offered.

    Deliberately strict: a callback is data from a client, and honouring one that
    came from an older menu (or a hand-crafted tap) would run a request the user was
    never shown. ``None`` means "not an option", and the caller re-asks.
    """
    normalized = str(quality or "").strip().lower()
    for choice in routing_for(url).choices:
        if choice.media_format == media_format and choice.quality == normalized:
            return choice
    return None
