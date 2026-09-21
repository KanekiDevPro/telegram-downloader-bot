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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from core.utils import MediaFormat, Quality

#: What the link most likely holds.
ContentKind = Literal["video", "audio", "image", "gallery", "media"]

#: One button: a label key in the catalogue, and the request it stands for.
@dataclass(frozen=True)
class Choice:
    """A format/quality option offered for a link, in button order."""

    label_key: str
    media_format: MediaFormat
    quality: Quality


@dataclass(frozen=True)
class Routing:
    """What to ask a user about this link: the header line and the buttons under it."""

    kind: ContentKind
    header_key: str
    choices: tuple[Choice, ...]


#: Video quality tiers, best first — the order a person thinks in.
_VIDEO_CHOICES: tuple[Choice, ...] = (
    Choice("fmt.video_best", "video", "best"),
    Choice("fmt.video_1080", "video", "1080"),
    Choice("fmt.video_720", "video", "720"),
    Choice("fmt.video_480", "video", "480"),
)

#: Audio: the untouched stream first (it is lossless-from-source and never cheaper
#: than the MP3 re-encode it would replace), then the universally-compatible MP3.
_AUDIO_CHOICES: tuple[Choice, ...] = (
    Choice("fmt.audio_m4a", "audio", "m4a"),
    Choice("fmt.audio_mp3", "audio", "mp3"),
)

#: Everything else: "send the media of this post" is a *video* request to the engine,
#: because that is the request that fetches whatever the post holds — and the image
#: case is served by the fallback engine and delivered as photos either way.
_MEDIA_CHOICE = Choice("fmt.media", "video", "best")

_MEDIA_CHOICES: tuple[Choice, ...] = (_MEDIA_CHOICE, *_AUDIO_CHOICES)

#: Host → kind, for the hosts where the host alone settles it.
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
)

#: Path fragments that speak for *any* host (checked in order). Deliberately
#: generic — ``/watch`` or ``/shorts/`` would be a claim about a website from a path
#: only YouTube uses, and a host that merely *looks* like YouTube would inherit it.
#: The hosts that really use those paths are listed in ``_HOST_PATHS`` instead.
_PATH_KINDS: tuple[tuple[tuple[str, ...], ContentKind], ...] = (
    (("/photo/", "/photos/", "/gallery"), "gallery"),
    (("/video/", "/videos/", "/embed/", "/reel/"), "video"),
    (("/track/", "/album/", "/playlist/", "/sets/"), "audio"),
)

#: Host → path fragments *that host* uses, checked before the generic ones. A post
#: URL is host-specific syntax (``/p/`` means a post on instagram and a blog page
#: elsewhere), so it is only read where it means what it looks like.
_HOST_PATHS: dict[str, tuple[tuple[tuple[str, ...], ContentKind], ...]] = {
    "youtube.com": (
        (("/shorts/", "/embed/", "/watch", "/live/"), "video"),
        (("/playlist",), "video"),
    ),
    "instagram.com": (
        (("/p/", "/stories"), "gallery"),
        (("/reel", "/tv/"), "video"),
    ),
    "tiktok.com": ((("/photo/",), "gallery"), (("/video/",), "video")),
    "facebook.com": (
        (("/photo",), "image"),
        (("/watch", "/videos/", "/reel", "/video/"), "video"),
    ),
    "twitter.com": ((("/photo/",), "image"), (("/video/",), "video")),
    "x.com": ((("/photo/",), "image"), (("/video/",), "video")),
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


def routing_for(url: str) -> Routing:
    """The header and the buttons this link deserves."""
    kind = classify(url)
    return Routing(kind=kind, header_key=_HEADERS[kind], choices=_CHOICES[kind])


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
