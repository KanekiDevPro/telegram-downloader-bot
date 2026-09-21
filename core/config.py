"""Application configuration loaded from environment variables / `.env` file."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Annotated, Literal, Mapping, TypeVar
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources.utils import parse_env_vars

from core.i18n import normalize_supported

BASE_DIR = Path(__file__).resolve().parent.parent

#: The official cloud Bot API refuses bot uploads larger than this. A self-hosted
#: ``telegram-bot-api`` raises the ceiling to 2000 MB (4 GB downloads in local mode).
CLOUD_API_UPLOAD_LIMIT_MB = 50

#: Hosts that mean "the Cobalt instance this stack runs": the compose service name
#: and the loopback port it publishes for a host-run bot. Anything else — including a
#: LAN address or a proxy — is somewhere else, and the reports say so, because "same
#: host" and "somewhere else" differ in exactly the way that matters here: only the
#: second one can serve a link this host's IP is blocked from.
LOCAL_COBALT_HOSTS: frozenset[str] = frozenset(
    {"cobalt", "localhost", "127.0.0.1", "::1", "host.docker.internal"}
)


def candidates_configured(candidates: list[str]) -> bool:
    """Whether any address in the list is non-empty (i.e. a pool is configured)."""
    return any(isinstance(candidate, str) and candidate.strip() for candidate in candidates)


def cobalt_instance_is_local(url: str) -> bool:
    """Whether a Cobalt URL points at the instance shipped in ``docker-compose``."""
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_COBALT_HOSTS or host.startswith("127.")


#: The public Cobalt instance, as published by the project itself
#: (https://github.com/imputnet/cobalt — the docs name this one as the official
#: instance). Only ever the *last* node tried, because it is the one we do not
#: control: it may require an API key (``error.api.auth.jwt.missing``), rate-limit
#: anonymous callers, and it sees every link handed to it. A second public node is
#: deliberately *not* listed here: mirrors come and go, and a hard-coded third-party
#: address in a production path is exactly the thing that rots into a dead default.
#: Operators who want more add them to ``COBALT_FALLBACK_URLS`` (the community keeps
#: a live list at https://instances.cobalt.best).
PUBLIC_COBALT_INSTANCES: tuple[str, ...] = ("https://api.cobalt.tools",)

#: Everything a person puts *between* two ids or two URLs: commas, semicolons,
#: whitespace (including the newline of one-per-line), in any mixture.
_ADMIN_SPLIT = re.compile(r"[,;\s]+")


def _admin_id(value: object, source: str) -> int:
    """One Telegram id, or a validation error naming the offender and the formats.

    Raising (rather than dropping the token) is the point: an ``ADMIN_IDS`` with a
    typo in it either means the operator is locked out of their own bot, or — if we
    guessed — that some *other* id silently became an admin. Neither is a thing to
    discover later, so the message says what to write instead.
    """
    text = str(value).strip().strip("'\"").strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    raise ValueError(
        f"ADMIN_IDS got {value!r} (from {source!r}) — numeric Telegram user ids are "
        "required, separated by commas or spaces, e.g. ADMIN_IDS=123456789, 987654321"
    )


#: Compose service names and the loopback port each is published on. The published
#: port equals the port the service listens on (``docker-compose.yml`` binds
#: ``127.0.0.1:9000:9000``), so a process on the host reaches the *same* instance.
COMPOSE_LOOPBACK_PORTS: dict[str, int] = {
    "cobalt": 9000,
    # The tunneled fallback instance. Published for the same reason as the others:
    # a bot running on the host must be able to reach the *same* instance, and
    # `cobalt-warp` does not resolve outside the compose network.
    "cobalt-warp": 9002,
    "pot-provider": 4416,
    "yt-session-generator": 8080,
    # The WARP tunnel. Its port is not published (nothing outside the network should
    # dial it), but a *host-run* bot reaches the published half of the same container
    # as 127.0.0.1:1080 — which is what makes a YTDLP_PROXY of `http://warp:1080`
    # usable from `python main.py` during development.
    "warp": 1080,
}

#: The YouTube clients yt-dlp is asked for, in this order, unless ``.env`` says
#: otherwise. ``web`` is last on purpose: it is the client whose *visitor binding*
#: produces the "the page needs to be reloaded" (``SESSION_STALE``) failure this
#: deployment keeps meeting, and the only one of these four that wants a PO token —
#: which the bgutil provider supplies. The other three are the token-free half of
#: yt-dlp's own table, and (except ``visionos``) all of them carry cookies, so a
#: signed-in jar keeps working.
#:
#: Measured from the installed yt-dlp (2026.08.19) rather than from folklore: in its
#: ``INNERTUBE_CLIENTS`` table ``web``, ``web_safari``, ``mweb``, ``android``,
#: ``android_vr``, ``ios`` and ``tv_simply`` all have ``GVS_PO_TOKEN_POLICY``
#: marked ``required``, while ``visionos``, ``web_embedded``, ``tv`` and
#: ``tv_downgraded`` do not. A device-spoofing list of ``android,ios`` is therefore
#: the *opposite* of a bypass in this version — those two are the clients that
#: demand the token. ``services.extractor.youtube_client_facts`` reads that same
#: table at runtime, and ``/doctor`` reports what it finds.
DEFAULT_YOUTUBE_CLIENTS: tuple[str, ...] = (
    "visionos",
    "web_embedded",
    "tv_downgraded",
    "web",
)

#: The setting each compose helper is reached through. A report that says
#: "unreachable" is only actionable if it also names the variable to look at.
COMPOSE_SERVICE_ENV: dict[str, str] = {
    "cobalt": "COBALT_API_URL",
    "cobalt-warp": "COBALT_FALLBACK_URLS",
    "pot-provider": "YTDLP_POT_PROVIDER_URL",
    "yt-session-generator": "YOUTUBE_SESSION_SERVER",
    "warp": "YTDLP_PROXY",
}


def in_container() -> bool:
    """Whether this process runs inside a container.

    The answer decides what a compose *service name* means: inside the network it
    resolves, outside it does not — which is exactly why the services are published
    on loopback.
    """
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def probe_url(url: str) -> str:
    """Where *this process* can reach a compose helper.

    A helper is configured with the name its *container* resolves
    (``http://pot-provider:4416``), and the scripts run on the host
    (``.dockerignore`` keeps them out of the image) where that name does not exist.
    One setting, two addresses: the service name inside the network, the published
    loopback port outside it. Without this a healthy zero-config stack would report
    its own helpers as unreachable when checked from the host — an operator chasing
    a problem that does not exist.

    Only a compose service name is translated; anything else is returned untouched.
    """
    parsed = urlparse(url)
    port = COMPOSE_LOOPBACK_PORTS.get((parsed.hostname or "").lower())
    if port is None or in_container():
        return url
    userinfo = ""
    if parsed.username:
        userinfo = f"{parsed.username}:{parsed.password}@" if parsed.password else f"{parsed.username}@"
    return parsed._replace(netloc=f"{userinfo}127.0.0.1:{parsed.port or port}").geturl()


def _strip_inline_comment(value: str) -> str:
    """Cut a trailing ``# comment`` off a single dotenv value, honouring quotes."""
    chars: list[str] = []
    quote: str | None = None
    for index, char in enumerate(value):
        if quote is not None:
            chars.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            chars.append(char)
        elif char == "#" and (index == 0 or value[index - 1] in " \t"):
            break
        else:
            chars.append(char)
    return "".join(chars)


def sanitize_env_text(text: str) -> str:
    """Strip inline comments from a whole dotenv document.

    python-dotenv only removes an inline comment when a value precedes it, so
    ``TELEGRAM_API_ID=      # from my.telegram.org`` ends up **as the comment
    text**: an ``int`` field dies at import time and — far worse — a ``str``
    field silently accepts the comment as its value. Stripping the comment here
    makes "left blank with an explanatory comment" behave as "left blank".
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key, _, value = line.partition("=")
        lines.append(f"{key}={_strip_inline_comment(value).rstrip()}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


class _CommentSafeDotEnvSource(DotEnvSettingsSource):
    """``.env`` reader that treats an inline comment after a blank value as blank."""

    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        text = file_path.read_text(encoding=self.env_file_encoding or "utf-8")
        values = dotenv_values(stream=StringIO(sanitize_env_text(text)))
        return parse_env_vars(
            values,
            self.case_sensitive,
            self.env_ignore_empty,
            self.env_parse_none_str,
        )


class _CommentSafeEnvSource(EnvSettingsSource):
    """Environment reader with the same guard for values that are really comments.

    ``docker compose``'s ``env_file`` parser shares python-dotenv's quirk, so the
    comment written after an empty value reaches the container *through the
    environment* instead of a file. No setting here can legitimately start with
    ``#``, so such a value is read as "left blank".
    """

    def _load_env_vars(self) -> Mapping[str, str | None]:
        return {
            key: "" if isinstance(value, str) and value.lstrip().startswith("#") else value
            for key, value in super()._load_env_vars().items()
        }


_SourceT = TypeVar("_SourceT", bound=EnvSettingsSource)


def _reparse(source: PydanticBaseSettingsSource, cls: type[_SourceT]) -> _SourceT:
    """Re-read ``source`` as ``cls``.

    pydantic-settings parses env files eagerly in the source's ``__init__`` (and
    the built sources hold resolved state such as ``_env_file=None``), so the
    clone reuses every attribute and only re-runs the reading step.
    """
    if not isinstance(source, EnvSettingsSource):
        raise TypeError(f"expected an env-based settings source, got {type(source).__name__}")
    clone = cls.__new__(cls)
    clone.__dict__.update(vars(source))
    clone.env_vars = clone._load_env_vars()  # noqa: SLF001
    return clone


def _resolve_path(value: object) -> Path:
    """Resolve a possibly relative path against the project root, not the CWD.

    Keeps the bot independent of how/where it was launched (systemd, supervisor,
    Docker, a shell in another directory).
    """
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


class Settings(BaseSettings):
    """Central settings object. Every field can be overridden via env or `.env`."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep the default source order, but read configuration comment-safely.

        Both the environment and the ``.env`` reader are rebuilt on top of the
        sources pydantic-settings already configured (they hold the resolved
        ``env_file`` — including ``Settings(_env_file=None)`` — plus encoding and
        prefixes); only the parsing of “empty value followed by a comment”
        changes.
        """
        return (
            init_settings,
            _reparse(env_settings, _CommentSafeEnvSource),
            _reparse(dotenv_settings, _CommentSafeDotEnvSource),
            file_secret_settings,
        )

    # --- Telegram -----------------------------------------------------------
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    # ``NoDecode`` keeps the raw env string so the validator below can accept
    # "1,2", "1 2" or "[1, 2]" — pydantic-settings would otherwise require JSON.
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list, alias="ADMIN_IDS")
    #: What a *new* user gets when their Telegram client does not speak a language
    #: this bot knows. 'en' (the product default) or 'fa'. A user whose locale is
    #: ``fa``/``fa-IR`` starts in Persian regardless; a stored choice always wins.
    default_language: str = Field(default="en", alias="DEFAULT_LANGUAGE")
    bot_mode: Literal["polling", "webhook"] = Field(default="polling", alias="BOT_MODE")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_path: str = Field(default="/webhook", alias="WEBHOOK_PATH")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    webhook_host: str = Field(default="0.0.0.0", alias="WEBHOOK_HOST")
    webhook_port: int = Field(default=8080, alias="WEBHOOK_PORT")

    # --- Local Telegram Bot API server (optional, recommended for >50 MB) ----
    # Empty base URL = talk to the official cloud API. Setting it routes every
    # call through a self-hosted telegram-bot-api container instead.
    telegram_api_base_url: str = Field(default="", alias="TELEGRAM_API_BASE_URL")
    telegram_api_local: bool = Field(default=False, alias="TELEGRAM_API_LOCAL")
    #: Only used by the telegram-bot-api container; validated here so a
    #: half-configured stack is caught at startup instead of mid-download.
    telegram_api_id: int = Field(default=0, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    #: Host directory mounted into telegram-bot-api, when it differs from
    #: DOWNLOAD_DIR (enables reading files off disk instead of re-uploading).
    telegram_api_files_dir: Path | None = Field(default=None, alias="TELEGRAM_API_FILES_DIR")

    #: Set at runtime by ``use_cloud_api_fallback`` when the configured local
    #: server turns out to be unreachable. Mirrored in ``docker-compose.yml``.
    cloud_api_fallback: bool = Field(default=False, exclude=True)

    # --- Infrastructure -----------------------------------------------------
    database_url: str = Field(
        default="postgresql://downloader:downloader@localhost:5432/downloader",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    queue_backend: Literal["redis", "memory"] = Field(default="redis", alias="QUEUE_BACKEND")
    queue_name: str = Field(default="dl:tasks", alias="QUEUE_NAME")

    # --- Downloader ---------------------------------------------------------
    download_dir: Path = Field(default=BASE_DIR / "downloads", alias="DOWNLOAD_DIR")
    max_file_size_mb: int = Field(default=2000, alias="MAX_FILE_SIZE_MB")
    extractor_timeout_s: int = Field(default=90, alias="EXTRACTOR_TIMEOUT_S")
    download_timeout_s: int = Field(default=1800, alias="DOWNLOAD_TIMEOUT_S")
    #: Extra attempts after a *retryable* extraction failure (YouTube's stale
    #: session) plus its exponential backoff. 0 disables retrying.
    extractor_retry_attempts: int = Field(default=2, ge=0, le=5, alias="EXTRACTOR_RETRY_ATTEMPTS")
    extractor_retry_backoff_s: float = Field(
        default=3.0, ge=0, le=60, alias="EXTRACTOR_RETRY_BACKOFF_S"
    )
    #: Defaults to <project>/cookies.txt so dropping a cookie jar in the root is
    #: enough to get past YouTube's bot detection. A missing file is ignored.
    cookie_file: Path | None = Field(default=BASE_DIR / "cookies.txt", alias="COOKIE_FILE")
    #: How often the running bot re-checks the cookie jar so it can tell the
    #: admins that a fresh export is waiting to be picked up (seconds). 0 turns
    #: the watcher off; it only ever sends one message per export.
    cookie_watch_interval_s: float = Field(
        default=60.0, ge=0, le=3600, alias="COOKIE_WATCH_INTERVAL_S"
    )
    #: How often the running bot asks the two YouTube helpers whether they are still
    #: alive, so a helper that dies at night is a recorded outage with a page rather
    #: than a mystery the next user runs into (seconds). 0 turns the watch off; it
    #: only ever writes a row when a state *changes*.
    helper_watch_interval_s: float = Field(
        default=300.0, ge=0, le=86400, alias="HELPER_WATCH_INTERVAL_S"
    )
    #: Optional proxy for yt-dlp. YouTube's bot check is IP-based, so a valid
    #: cookie jar is not always enough on a flagged host.
    #:
    #: Empty by default *here*, and set to the embedded WARP tunnel *there*: the
    #: default belongs to the deployment, not to the library — `docker-compose.yml`
    #: writes `http://warp:1080` (see the bot service), while a host run or a test
    #: starts with no proxy at all. When it is set, the bot probes it before use and
    #: goes direct if it does not answer (``services/proxy_health.py``), because
    #: every download goes through it.
    ytdlp_proxy: str = Field(default="", alias="YTDLP_PROXY")
    #: How hard to try before deciding the tunnel is down. WARP registers a few
    #: seconds *after* its container starts, so one probe would report a tunnel that
    #: is merely still coming up as broken — and the bot would spend that window
    #: downloading from the address the tunnel exists to escape. Bounded, because
    #: boot waits for this and a tunnel that never comes up is handled (downloads run
    #: direct, an admin is told), not waited on forever.
    tunnel_probe_attempts: int = Field(
        default=6, ge=1, le=60, alias="TUNNEL_PROBE_ATTEMPTS"
    )
    #: Seconds between those attempts.
    tunnel_probe_delay_s: float = Field(
        default=2.0, ge=0, le=60, alias="TUNNEL_PROBE_DELAY_S"
    )
    #: Base URL of a PO-token provider (bgutil HTTP server). Defaults to the one
    #: `docker-compose.yml` runs, because a PO token is the only fix for YouTube's
    #: bot check that needs no login at all. Empty = off.
    #: Requires the bgutil plugin, which the image installs.
    ytdlp_pot_provider_url: str = Field(
        default="http://pot-provider:4416", alias="YTDLP_POT_PROVIDER_URL"
    )
    #: ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` — read cookies straight from a
    #: browser profile instead of a ``cookies.txt``. Only useful when a real
    #: browser profile is reachable from the process (host runs, mounted profile).
    cookies_from_browser: str = Field(default="", alias="COOKIES_FROM_BROWSER")
    #: Same syntax as ``COOKIES_FROM_BROWSER``, but used to *rewrite* the jar when a
    #: download fails because YouTube treated us as anonymous: the profile is read,
    #: the jar is replaced atomically, and the result is probed and reported. Empty
    #: (default) = off. Only useful where a real profile is reachable — a host run,
    #: or a mounted profile; inside a plain container there is no browser to read.
    cookie_auto_export: str = Field(default="", alias="COOKIE_AUTO_EXPORT")
    #: JavaScript runtime for yt-dlp: ``auto`` (use whatever is installed),
    #: ``none``, or ``node[:/path/to/node]`` / ``deno`` / ``bun`` / ``quickjs``.
    #: YouTube extraction degrades without one.
    ytdlp_js_runtime: str = Field(default="auto", alias="YTDLP_JS_RUNTIME")
    #: YouTube clients to ask for, in order (``YTDLP_YOUTUBE_CLIENTS``, written the
    #: way a list usually is: ``visionos,web_embedded,tv_downgraded,web``). Empty =
    #: yt-dlp picks (its own default is ``visionos``+``web`` for an anonymous
    #: session and ``web_embedded``+``tv_downgraded``+``web`` with a jar). See
    #: ``DEFAULT_YOUTUBE_CLIENTS`` for why the shipped default looks like this.
    ytdlp_youtube_clients: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_YOUTUBE_CLIENTS),
        alias="YTDLP_YOUTUBE_CLIENTS",
    )
    #: Force IPv4 for every yt-dlp connection — the same thing as ``--force-ipv4``
    #: (yt-dlp implements both as ``source_address = 0.0.0.0``). On by default
    #: because of the tunnel: Cloudflare WARP's *IPv6* ranges are the ones YouTube
    #: flags hardest, and the family filter is applied inside yt-dlp's socket layer
    #: (``yt_dlp/networking/_helper.py``), so IPv6 candidates are never attempted
    #: rather than attempted and refused. Turn it off only when a source in the
    #: deployment has no IPv4 address at all.
    ytdlp_force_ipv4: bool = Field(default=True, alias="YTDLP_FORCE_IPV4")
    worker_count: int = Field(default=2, alias="WORKER_COUNT")

    # --- Fallback extractor (Cobalt) ----------------------------------------
    #: Base URL of a Cobalt instance. Used only when yt-dlp comes back *blocked* —
    #: a flagged IP or a jar the site refuses — so the user still gets the file.
    #: Empty = fallback off.
    #:
    #: The default is the instance `docker-compose.yml` runs next to the bot, so a
    #: Docker deployment has a working safety net with no configuration at all.
    #: (The public api.cobalt.tools is *not* a usable default any more: it retired
    #: its v7 endpoint and refuses anonymous callers.) Running the bot on the host
    #: against the containerised infra? `http://127.0.0.1:9000` reaches the same
    #: instance through the loopback port the compose file publishes.
    cobalt_api_url: str = Field(default="http://cobalt:9000", alias="COBALT_API_URL")
    #: Base URL of a YouTube *trusted session* server (`yt-session-generator` in
    #: `docker-compose.yml`). Cobalt reads this one, not us — it is the fallback
    #: engine's second way past YouTube's per-client bot check, and the only route
    #: that needs no login at all. The bot probes it so `/doctor` can say whether
    #: that route is alive. Empty = the fallback runs without one.
    youtube_session_server: str = Field(
        default="http://yt-session-generator:8080", alias="YOUTUBE_SESSION_SERVER"
    )
    #: Where that server's own browser reports which route it took (written by
    #: ``deploy/session_proxy/``, mounted read-only here). The setting it was given
    #: and the route it actually used are different facts, and only one of them
    #: explains a token that never arrives — so `/doctor` reads this instead of
    #: trusting the configuration. Empty = the row is not shown at all.
    session_route_file: Path | None = Field(default=None, alias="YT_SESSION_ROUTE_FILE")
    #: Some instances (self-hosted ones, and api.cobalt.tools) require a key; it
    #: is sent as ``Authorization: Api-Key <key>`` only when non-empty.
    #: Extra Cobalt instances to rotate through when the one above cannot serve a
    #: link — a second self-hosted node on another host, a friend's instance, or a
    #: public one. Comma/space separated (``COBALT_FALLBACK_URLS``); empty by
    #: default, because pointing a stranger's server at your users' links is the
    #: operator's decision, not ours.
    cobalt_fallback_urls: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="COBALT_FALLBACK_URLS"
    )
    #: Whether the official public instance is appended to that rotation. On by
    #: default: a VPS whose IP YouTube has flagged gets *no* fallback at all
    #: otherwise, since the embedded instance shares that IP. Costs one failed
    #: request per blocked link when it refuses anonymous callers, and is the first
    #: thing to switch off if you would rather no link ever left your network.
    cobalt_try_public_instances: bool = Field(
        default=True, alias="COBALT_TRY_PUBLIC_INSTANCES"
    )
    cobalt_api_key: str = Field(default="", alias="COBALT_API_KEY")
    #: How long a *metadata* resolution may take, and the separate budget for the
    #: file body. The resolve is one round trip; the download is a real transfer.
    cobalt_timeout_s: float = Field(default=30.0, gt=0, le=300, alias="COBALT_TIMEOUT_S")
    cobalt_download_timeout_s: float = Field(
        default=1800.0, gt=0, le=7200, alias="COBALT_DOWNLOAD_TIMEOUT_S"
    )
    #: Proxy for the fallback only. Deliberately *not* inherited from
    #: ``YTDLP_PROXY``: the point of the fallback is often a different path out,
    #: and a proxy that YouTube refuses may be accepted elsewhere.
    cobalt_proxy: str = Field(default="", alias="COBALT_PROXY")
    #: Where the cobalt-format ``cookies.json`` is generated from the bot's own
    #: jar, so one export keeps both engines signed in. This is the *host*
    #: directory `docker-compose.yml` mounts into the cobalt service
    #: (``COBALT_COOKIES_HOST_DIR``, defaulting to ``./cobalt``), and an empty
    #: value turns the generation off.
    cobalt_cookies_dir: Path | None = Field(
        default=BASE_DIR / "cobalt", alias="COBALT_COOKIES_DIR"
    )

    # --- Limits / plans -----------------------------------------------------
    default_daily_limit: int = Field(default=10, alias="DEFAULT_DAILY_LIMIT")
    premium_daily_limit: int = Field(default=60, alias="PREMIUM_DAILY_LIMIT")

    # --- Manual payment -----------------------------------------------------
    manual_card_number: str = Field(default="", alias="MANUAL_CARD_NUMBER")
    manual_card_holder: str = Field(default="", alias="MANUAL_CARD_HOLDER")

    # --- Misc ---------------------------------------------------------------
    timezone: str = Field(default="Asia/Tehran", alias="TIMEZONE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """Every way an operator writes a list of ids — and a loud error for a typo.

        Measured, because each shape below is a way the admin silently stops being an
        admin (or the bot refuses to start at all):

        * ``123456`` — one id;
        * ``123, 456`` / ``123 456`` / ``123;456`` / one per line — the separators a
          person actually types;
        * ``123,`` — a trailing separator, which ``int("")`` used to reject;
        * ``"123456"`` — quoted. A ``.env`` file has its quotes stripped by the
          dotenv parser, but ``docker compose``'s own ``environment:`` block, an
          ``export`` in a shell and ``docker run -e`` all pass them through, and
          ``int('"123"')`` ends the process with a ValidationError;
        * ``[123, "456"]`` — a JSON list (ids as strings are welcome);
        * ``123, oops`` — a typo, which raises a message naming the token and the
          accepted formats instead of a bare pydantic error nobody can act on.
        """
        if value is None:
            return []
        if not isinstance(value, str):
            return value
        raw = value.strip().strip("'\"").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                parts: list[object] = list(parsed)
                return [_admin_id(part, raw) for part in parts]
        return sorted({_admin_id(part, raw) for part in _ADMIN_SPLIT.split(raw) if part})

    @field_validator("default_language", mode="before")
    @classmethod
    def _parse_default_language(cls, value: object) -> object:
        """Accept only what the catalogue has; anything else is English.

        A typo here would otherwise decide which language every new user sees, and
        the failure mode (English text in a bot deployed for Persian speakers, and
        silently) is exactly the kind that survives months of uptime.
        """
        if value in (None, ""):
            return "en"
        return value if normalize_supported(value) else "en"

    @field_validator(
        "extractor_retry_attempts",
        "extractor_retry_backoff_s",
        "cookie_watch_interval_s",
        mode="before",
    )
    @classmethod
    def _blank_number_is_default(cls, value: object, info: ValidationInfo) -> object:
        """``KEY=`` in .env means "use the default", not a crash (see ADMIN_IDS)."""
        if isinstance(value, str) and not value.strip():
            field_name = info.field_name or ""
            return cls.model_fields[field_name].default
        return value

    @field_validator("cookie_file", mode="before")
    @classmethod
    def _blank_cookie_file_is_none(cls, value: object) -> object:
        """An empty ``COOKIE_FILE=`` means "no cookie file", not Path('.')."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "-"}:
            return None
        return _resolve_path(value)

    @field_validator("cobalt_cookies_dir", mode="before")
    @classmethod
    def _resolve_cobalt_cookies_dir(cls, value: object) -> object:
        """Blank means "do not generate one"; a relative dir lives under the project."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "-"}:
            return None
        return _resolve_path(value)

    @field_validator("download_dir", mode="before")
    @classmethod
    def _resolve_download_dir(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return BASE_DIR / "downloads"
        return _resolve_path(value)

    @field_validator("webhook_path", mode="before")
    @classmethod
    def _normalize_webhook_path(cls, value: object) -> object:
        """Guarantee a leading slash so the webhook URL is always well-formed."""
        if not isinstance(value, str) or not value.strip():
            return "/webhook"
        path = "/" + value.strip().strip("/")
        return path or "/webhook"

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def _blank_telegram_api_id_is_zero(cls, value: object) -> object:
        """``TELEGRAM_API_ID=`` (the .env.example default) must not crash parsing."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0
        return value

    @field_validator("telegram_api_local", "ytdlp_force_ipv4", mode="before")
    @classmethod
    def _parse_bool_flag(cls, value: object) -> object:
        """Accept 1/0, true/false, yes/no — and a blank value meaning "off"."""
        if isinstance(value, bool) or not isinstance(value, str):
            return value
        raw = value.strip().lower()
        if raw in {"", "0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return value  # anything else: let pydantic report a clear validation error

    @field_validator("telegram_api_files_dir", "session_route_file", mode="before")
    @classmethod
    def _blank_env_dir_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return _resolve_path(value)

    @field_validator(
        "telegram_api_base_url",
        "ytdlp_pot_provider_url",
        "cobalt_api_url",
        "cobalt_proxy",
        "youtube_session_server",
        mode="before",
    )
    @classmethod
    def _normalize_base_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip().rstrip("/")

    @field_validator("ytdlp_youtube_clients", mode="before")
    @classmethod
    def _parse_youtube_clients(cls, value: object) -> object:
        """``a,b`` / ``a b`` / ``[a, b]`` — lower-cased, because yt-dlp lower-cases.

        A blank value means "let yt-dlp decide", which is why it maps to an empty
        list and *not* to the default: the default is a choice this deployment makes,
        and an operator who writes an empty value is making a different one.
        """
        if value is None:
            return []
        if not isinstance(value, str):
            return value
        raw = value.strip().strip("'\"").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip().lower() for item in parsed if str(item).strip()]
        return [part.lower() for part in _ADMIN_SPLIT.split(raw) if part]

    @field_validator("cobalt_fallback_urls", mode="before")
    @classmethod
    def _parse_fallback_urls(cls, value: object) -> object:
        """``a,b`` / ``a b`` / ``[a, b]`` / blank — the same tolerance as ADMIN_IDS.

        Sharing the parsing habit matters here: an operator who wrote
        ``COBALT_API_URL`` as one address will write a list as one line, and a
        silent misfire would look like "the fallback does not work".
        """
        if value is None:
            return []
        if not isinstance(value, str):
            return value
        raw = value.strip().strip("'\"").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip().rstrip("/") for item in parsed if str(item).strip()]
        return [part.rstrip("/") for part in _ADMIN_SPLIT.split(raw) if part]

    @field_validator("cobalt_api_key", mode="before")
    @classmethod
    def _strip_api_key(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("cookies_from_browser", mode="before")
    @classmethod
    def _strip_browser_spec(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def max_file_size_bytes(self) -> int:
        """The operator's configured ceiling, before any transport cap."""
        return self.max_file_size_mb * 1024 * 1024

    @property
    def upload_limit_mb(self) -> int:
        """Effective per-file ceiling for the transport we are actually using.

        Falling back to the cloud API silently drops the ceiling to 50 MB, and a
        download that big would only fail *after* all the work — so the limit is
        lowered up front and the worker rejects the link immediately.
        """
        if self.cloud_api_fallback:
            return min(self.max_file_size_mb, CLOUD_API_UPLOAD_LIMIT_MB)
        return self.max_file_size_mb

    @property
    def upload_limit_bytes(self) -> int:
        return self.upload_limit_mb * 1024 * 1024

    def use_cloud_api_fallback(self) -> None:
        """Remember that the local Bot API server was unreachable (see main.py)."""
        self.cloud_api_fallback = True

    @property
    def admin_id_set(self) -> frozenset[int]:
        return frozenset(self.admin_ids)

    @property
    def wants_pot_provider(self) -> bool:
        """True when a PO-token provider is configured for yt-dlp."""
        return bool(self.ytdlp_pot_provider_url)

    @property
    def wants_session_server(self) -> bool:
        """True when the fallback has a YouTube session server to lean on."""
        return bool(self.youtube_session_server)

    @property
    def cobalt_enabled(self) -> bool:
        """True when at least one Cobalt instance is configured as the fallback.

        “Configured” is the only thing we can know cheaply: probing the instances
        on every link would cost more than the fallback saves. If it turns out to
        be down when it matters, the original yt-dlp error is what the user gets.
        """
        return bool(self.cobalt_endpoints)

    @property
    def cobalt_endpoints(self) -> tuple[str, ...]:
        """Every instance to try, in order: the one above, the extras, the public node.

        Order is the whole design of a fallback: the instance you control is asked
        first (it is fast, local and private), the operator's own mirrors next, and
        the public instance last — it is the one that may refuse anonymous callers,
        rate-limit us, or log a user's link, so nothing reaches it while a node you
        pay for can still answer.
        """
        candidates = [self.cobalt_api_url, *self.cobalt_fallback_urls]
        # Only as a *backup*: an operator who blanks every address is switching the
        # fallback off, and quietly reaching for a public instance instead would
        # undo that (and leak the links). The shipped default is non-empty, so a
        # zero-config deployment still gets the public node as its second hope.
        if self.cobalt_try_public_instances and candidates_configured(candidates):
            candidates.extend(PUBLIC_COBALT_INSTANCES)
        seen: list[str] = []
        for candidate in candidates:
            url = candidate.strip().rstrip("/") if isinstance(candidate, str) else ""
            if url and url not in seen:
                seen.append(url)
        return tuple(seen)

    @property
    def cobalt_embedded(self) -> bool:
        """True when ``COBALT_API_URL`` is the instance shipped in compose.

        Not a guess about quality — the point is *where* it runs: the embedded
        instance shares this host's network address, so it can fix an extractor
        that is broken or a per-client bot check (given a YouTube session), but it
        cannot fix an IP the site has flagged — for that, the instance needs its
        own way out (``HTTP_PROXY``/``HTTPS_PROXY`` on the cobalt service). The
        reports say which one an operator is looking at.
        """
        return cobalt_instance_is_local(self.cobalt_api_url)

    @property
    def uses_local_api(self) -> bool:
        """True when the bot is routed through a self-hosted Bot API server."""
        return bool(self.telegram_api_base_url)

    @property
    def local_api_configured(self) -> bool:
        """True when the local server's own credentials are present."""
        return bool(self.telegram_api_id and self.telegram_api_hash)

    def is_admin(self, user_id: int | str | None) -> bool:
        """True when ``user_id`` belongs to one of the configured admins.

        A ``str`` id is accepted deliberately: ids reach us from three places — the
        environment (text), an ``asyncpg`` row (int) and JSON payloads (either) —
        and ``"8116519481" in {8116519481}`` is ``False``. That comparison failing
        does not look like a bug; it looks like a bot that ignores its own operator,
        which is why the conversion happens here, once, instead of at every call.
        """
        if user_id is None:
            return False
        try:
            return int(user_id) in self.admin_id_set
        except (TypeError, ValueError):
            return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — safe to call from any module."""
    return Settings()
