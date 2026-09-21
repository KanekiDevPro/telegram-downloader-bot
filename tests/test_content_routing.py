"""What the buttons are drawn from: the link's shape, and nothing else.

The gateway cannot probe a link before asking what to do with it (that would be a
network round trip in front of a question), so the *shape* decides. That makes this
module a table, and a table is exactly the thing to pin: every rule here is a claim
about what a URL can hold, and a wrong claim either hides an option the user wanted
or offers one that cannot work.

The last section is the other half of that promise: a callback that was never
offered must not run a request the menu never showed.
"""

from __future__ import annotations

import pytest

from services import content


@pytest.mark.parametrize(
    ("url", "kind"),
    (
        # Host alone settles these.
        ("https://www.youtube.com/watch?v=abc", "video"),
        ("https://m.youtube.com/watch?v=abc", "video"),
        ("https://youtu.be/abc", "video"),
        ("https://www.youtube-nocookie.com/embed/abc", "video"),
        ("https://vimeo.com/12345", "video"),
        ("https://www.twitch.tv/videos/1", "video"),
        ("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", "audio"),
        ("https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC", "audio"),
        ("https://soundcloud.com/artist/track", "audio"),
        ("https://artist.bandcamp.com/track/song", "audio"),
        ("https://www.pinterest.com/pin/12345/", "image"),
        ("https://i.imgur.com/abcd.jpg", "image"),
        # Paths that override a host whose posts can be anything.
        ("https://www.tiktok.com/@user/photo/123", "gallery"),
        ("https://www.tiktok.com/@user/video/123", "video"),
        ("https://www.instagram.com/p/abc/", "gallery"),
        ("https://www.instagram.com/reel/abc/", "video"),
        ("https://www.facebook.com/photo?fbid=1", "image"),
        ("https://www.facebook.com/watch/?v=1", "video"),
        ("https://x.com/user/photo/1", "image"),
        # Posts that could be anything: the honest answer is "whatever is there".
        ("https://x.com/user/status/12345", "media"),
        ("https://twitter.com/user/status/12345", "media"),
        ("https://www.reddit.com/r/x/comments/1/y/", "media"),
        ("https://mastodon.social/@user/123", "media"),
        ("https://t.me/channel/12", "media"),
        ("https://some-unknown-site.example/v/1", "media"),
        ("not even a url", "media"),
        ("", "media"),
    ),
)
def test_a_link_is_classified_by_what_it_can_hold(url: str, kind: str) -> None:
    assert content.classify(url) == kind


def test_a_spoofed_host_is_not_the_host_it_names() -> None:
    """``youtube.com.evil.example`` is not YouTube, whatever it starts with."""
    assert content.classify("https://youtube.com.evil.example/watch?v=abc") == "media"


def test_credentials_and_ports_do_not_change_the_host() -> None:
    assert content.classify("https://user:pass@www.youtube.com:443/watch?v=abc") == "video"


def test_video_links_are_offered_quality_tiers() -> None:
    choices = content.routing_for("https://youtu.be/abc").choices

    assert [choice.quality for choice in choices] == ["best", "1080", "720", "480"]
    assert all(choice.media_format == "video" for choice in choices)
    assert content.routing_for("https://youtu.be/abc").header_key == "intake.choose_quality"


def test_music_links_are_offered_audio_formats() -> None:
    routing = content.routing_for("https://soundcloud.com/a/b")

    assert [choice.quality for choice in routing.choices] == ["m4a", "mp3"]
    assert all(choice.media_format == "audio" for choice in routing.choices)
    assert routing.header_key == "intake.choose_audio"


def test_photo_posts_are_not_offered_a_quality_menu() -> None:
    choices = content.routing_for("https://www.instagram.com/p/abc/").choices

    assert len(choices) == 1, "a single picture has no resolution to choose"
    assert choices[0].label_key == "fmt.media"
    assert choices[0].media_format == "video", "the request is 'whatever this post holds'"


def test_an_ambiguous_post_is_offered_media_and_audio() -> None:
    choices = content.routing_for("https://x.com/user/status/12345").choices

    assert [choice.label_key for choice in choices] == [
        "fmt.media",
        "fmt.audio_m4a",
        "fmt.audio_mp3",
    ]


def test_every_offered_choice_is_parseable_as_a_callback() -> None:
    """What the keyboard writes must be what the handler can read back."""
    for url in ("https://youtu.be/abc", "https://soundcloud.com/a/b", "https://x.com/u/status/1"):
        for choice in content.routing_for(url).choices:
            data = f"fmt:{choice.media_format}:{choice.quality}"
            media_format, quality = data.split(":")[1], data.split(":")[2]
            assert content.find_choice(url, media_format, quality) == choice


def test_a_tap_on_something_that_was_offered_is_accepted() -> None:
    url = "https://youtu.be/abc"

    found = content.find_choice(url, "video", "720")

    assert found is not None
    assert (found.media_format, found.quality) == ("video", "720")


@pytest.mark.parametrize(
    ("url", "media_format", "quality"),
    (
        ("https://youtu.be/abc", "audio", "mp3"),  # a YouTube link has no audio-only tier
        ("https://youtu.be/abc", "video", "2160"),  # a tier this bot does not offer
        ("https://youtu.be/abc", "video", ""),
        ("https://soundcloud.com/a/b", "video", "1080"),
        ("https://www.instagram.com/p/abc/", "audio", "mp3"),
        ("https://x.com/u/status/1", "video", "480"),
    ),
)
def test_a_request_that_was_never_offered_is_rejected(
    url: str, media_format: str, quality: str
) -> None:
    assert content.find_choice(url, media_format, quality) is None


def test_the_cache_key_and_the_menu_agree_on_what_a_tier_is() -> None:
    """The menu's words and the cache's key come from one place, so a tier cannot
    exist in one and not the other."""
    from core.utils import normalize_quality

    for choice in content.routing_for("https://youtu.be/abc").choices:
        assert normalize_quality(choice.quality, choice.media_format) == choice.quality
