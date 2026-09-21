"""Smart-cache key tests.

The key is SHA-256 of the canonical URL **plus** the requested format, which is
what stops an MP3 request from being served a previously cached video.
"""

from __future__ import annotations

from services.cache import cache_key

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def test_format_is_part_of_the_key() -> None:
    assert cache_key(URL, "video") != cache_key(URL, "audio")


def test_default_format_is_video() -> None:
    assert cache_key(URL) == cache_key(URL, "video")


def test_tracking_parameters_are_ignored() -> None:
    tagged = f"{URL}&utm_source=telegram&utm_medium=bot&fbclid=xyz"
    assert cache_key(tagged, "video") == cache_key(URL, "video")


def test_fragments_are_ignored() -> None:
    assert cache_key(f"{URL}#t=42", "audio") == cache_key(URL, "audio")


def test_different_urls_get_different_keys() -> None:
    assert cache_key(URL, "video") != cache_key("https://youtu.be/other", "video")


def test_key_is_a_64_char_hex_digest() -> None:
    key = cache_key(URL, "video")
    assert len(key) == 64
    assert all(char in "0123456789abcdef" for char in key)
