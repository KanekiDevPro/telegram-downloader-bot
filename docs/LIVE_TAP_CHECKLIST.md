# Live-Tap Verification Checklist — Release Gate

Two flows still require **human verification on a real Telegram device** against
the deployed bot. Automated tests pin the behaviour (see `tests/`), but the
release gate is these taps: nothing here counts as verified until a human has
run it and recorded the result.

**Do not pre-fill any Status.** Every row starts as `NOT RUN`.

| Status | Meaning |
|---|---|
| `PASS` | Observed on a real device exactly as the Expected column says |
| `FAIL` | Observed behaviour differed — describe it in Notes and file the bug |
| `BLOCKED` | Could not be tested (environment, account, source) — say why in Notes |
| `NOT RUN` | Not attempted yet |

**Run context** (fill once per run):

| Field | Value |
|---|---|
| Date | |
| Tester | |
| Bot @username | |
| Build / commit | |
| Transport | cloud Bot API / local Bot API (`TELEGRAM_API_LOCAL`) |

---

## A. Video quality ladder

Suggested source: a multi-resolution video URL (e.g. a YouTube or Vimeo video
that is available in 480p–2160p). Also run one 720p-maximum source if handy.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| A1 | Send a supported video URL to the bot | The media/metadata card appears in one message | NOT RUN | |
| A2 | Look at the quality screen | Actual available resolutions are listed as buttons (e.g. `1080p · ~21.4 MB`) | NOT RUN | |
| A3 | Check the rows against the source | Resolutions appear **only** when the production chain can deliver them — no invented 2K/4K; on sources whose 1440p/2160p exist only in codecs the chain will not select there, those rows are absent by contract (see «Measured validation evidence») | NOT RUN | |
| A4 | Read all screen text | No "best quality", "highest", "up to", "smallest" or any ranking wording anywhere | NOT RUN | |
| A5 | Check sizes on the rows | Estimated size is shown beside every option the source reported a size for | NOT RUN | |
| A6 | Find a row whose size the source did not report | Row shows the honest `size unknown` / `حجم نامشخص` marker — the resolution is still listed | NOT RUN | |
| A7 | Tap a quality (e.g. 720p) | The keyboard disappears/replaces correctly — no stale sibling buttons | NOT RUN | |
| A8 | Watch the download | Exactly **one** download starts; exactly two user-visible messages total (card + file), no progress spam | NOT RUN | |
| A9 | Receive the file and check its resolution | The file's real resolution matches the tapped option | NOT RUN | |
| A10 | Read the final caption | Caption names the **delivered** quality and the file's **real** size (no `~` on the final size) | NOT RUN | |
| A11 | Send the same URL again | Cached replay arrives with the same caption contract and no duplicate messages | NOT RUN | |

## B. Spotify → format → bitrate → delivery

Suggested source: `https://open.spotify.com/track/3XcBnNDlch48NLxfPTl4ju`
(2:30 track — the exact case from the bug report).

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| B1 | Send the Spotify URL | The audio format grid appears | NOT RUN | |
| B2 | Read the format buttons | Only formats this source + engine + transport can really produce are offered (FLAC/WAV hidden when the file could not be delivered) | NOT RUN | |
| B3 | Tap **MP3** | Bitrate rows appear as real rates (`320 kbps`, `256 kbps`, …) | NOT RUN | |
| B4 | Check honesty vs the source | No misleading upscale presets above the source rate (e.g. no 320 kbps row for a 128 kbps source); source-rate note appears when the ladder was trimmed | NOT RUN | |
| B5 | Check the rows | Estimated size is shown beside **every** bitrate option (e.g. `320 kbps · ~6.1 MB`) | NOT RUN | |
| B6 | Tap **320 kbps** and receive the file | The delivered MP3 is really 320 kbps (check in a player/mediainfo) and the caption says `MP3 · 320 kbps` | NOT RUN | |
| B7 | Read the audio caption | 🎵 title, 🎤 artist, 💿 album·year, ⏱ duration, 🎧 real format·rate·**real** size, 🔗 the Spotify link, 🤖 bot | NOT RUN | |
| B8 | Send the URL again and tap **FLAC** (if offered) | FLAC is offered only if it can be produced **and** delivered within the active transport limit | NOT RUN | |
| B9 | Receive the FLAC selection | The delivered file is genuinely FLAC (never a renamed MP3); caption says `FLAC` | NOT RUN | |
| B10 | If FLAC is **not** offered | Its absence is correct for the transport (e.g. >50 MB on cloud API) — record which transport this run used | NOT RUN | |
| B11 | Watch the whole flow | Exactly two user-visible messages per download (card + file); keyboard never stale; no duplicate sends | NOT RUN | |
| B12 | Repeat one flow in a group chat | Same compact UI, no group status-message spam, file delivered | NOT RUN | |

---

## Measured validation evidence — automated live-taps (2026-09-24)

**These are downloader-level measurements, not device taps.** They were taken
from real production-path downloads (Live-tap A: YouTube quality ladder;
Live-tap B: Spotify → MP3; Live-tap C: Spotify → FLAC) and offline contract
tests. The A/B rows above keep their `NOT RUN` status — the release gate
remains the human tap on a real device. Measured facts are marked
**[measured]**; interpretations are marked **[interpretation]**.

**Run context (automated run):**

| Field | Value |
|---|---|
| Date | 2026-09-24 |
| Tester | automated live-taps (agent) on the production path |
| Bot @username | left for the human run (unchanged from deployment) |
| Build / commit | working tree, uncommitted — **0 commits created during this run** (verified) |
| Transport | local Bot API (`TELEGRAM_API_LOCAL`) |

### Video ladder (Live-tap A)

| Item | Evidence | Result |
|---|---|---|
| YouTube 360p | delivered 640×360 H.264 + AAC in MP4, structurally valid | **PASS** |
| YouTube 480p | delivered 854×480 H.264 + AAC in MP4, structurally valid | **PASS** |
| YouTube 720p | delivered 1280×720 H.264 60fps + AAC in MP4, structurally valid | **PASS** |
| YouTube 1080p | delivered 1920×1080 H.264 60fps + AAC in MP4, structurally valid | **PASS** |
| 1440p request | **[measured]** resolves to the 1080p H.264 rung on this source | recorded |
| 2160p request | **[measured]** resolves to the 1080p H.264 rung on this source | recorded |

**[measured] Codec/source-format limitation for 1440p/2160p:** the source's
rungs above 1080p exist only as VP9/AV1, and a 23-ladder survey found **no**
YouTube source exposing H.264/HEVC at ≥1440p through the production client
set. The production chain prefers H.265 → H.264 → other codecs within the
requested height, so those requests trade down to the 1080p H.264 rung
(offline counterfactual confirms H.264/HEVC at those heights *would* be
delivered).

**[contract] Video menu/delivery contract (shipped 2026-09-24):** a quality row
is advertised only when the production chain delivers that exact rung for a tap
on it. This amends row **A3** above. Offline regression coverage:
`tests/test_menu_contract.py`, `tests/test_ladder_smoke.py`.

### Spotify → MP3 (Live-tap B)

| Item | Evidence | Result |
|---|---|---|
| MP3 128 kbps | delivered real 128 kbps CBR MP3, 44.1 kHz stereo, full-file decode clean | **PASS** |
| MP3 192 kbps | delivered real 192 kbps CBR MP3, 44.1 kHz stereo, full-file decode clean | **PASS** |
| MP3 256 kbps | delivered real 256 kbps CBR MP3, 44.1 kHz stereo, full-file decode clean | **PASS** |
| MP3 320 kbps | delivered real 320 kbps CBR MP3, 44.1 kHz stereo, full-file decode clean | **PASS** |

**[measured] Source-quality limitation:** the mapped source's audio is a lossy
**≈129.5 kbps AAC** stream (44.1 kHz). The 256/320 kbps outputs are genuine
256/320 kbps CBR encodes *of that lossy source*.
**[interpretation]** The requested **encoding target** was satisfied; the
**source-quality ceiling** is a separate fact — transcoding adds no source
information.

### Spotify → FLAC (Live-tap C)

| Item | Evidence | Result |
|---|---|---|
| FLAC | container `flac`, codec `flac`, `.flac` extension, full-file decode clean | **PASS** |
| FLAC representation | **[measured]** 24-bit, 44.1 kHz, stereo | recorded |

**[interpretation]** Lossy AAC → FLAC transcoding does **not** restore lost
information: this is a valid lossless FLAC *container*, not lossless source
quality. The measured 24-bit representation is the conversion pipeline's
output depth — it does not prove 24-bit detail in the source.

### Audio menu/delivery contract — [contract]

Rows name the **output encoding target** (e.g. `320 kbps`); re-encode tiers
above the known source rate are not advertised, and the source's rate is stated
whenever it is known (`ℹ️ Source quality: …`). Delivery verification compares
the measured rate against the requested tier inside a documented window
(`services/verify.py`), and a source-quality upscale is deliberately **not** a
delivery failure — it is a local observation only. Offline regression coverage:
`tests/test_audio_menu_contract.py`, `tests/test_verify.py`.

### Test evidence

| | Tests | Files |
|---|---|---|
| Historical baseline (before this run) | 1,810 | 57 |
| Post-change (this run, +13) | **1,823** | 57 |

Both suites fully passing; `ruff` clean; `mypy` clean (109 files) —
**[measured]** this run.

### Run accounting (verified 2026-09-24)

- Commits created during this validation run: **0** (verified against `git log`).
- Secrets exposed in changed files and generated output: **0** (verified by
  pattern review of the complete changed-file set).
- Temporary live-test artifacts remaining: **0** (verified — no live downloads
  were re-run for this documentation pass; earlier live-tap media, drivers and
  logs were deleted after validation).

### Unresolved — explicitly not verified (no new evidence)

- **yt-session-generator `/token`** — external endpoint still answering 503
  (separate Layer-2 issue). Untouched in these runs.
- **Chromium / session-generator compatibility** — the image's pinned Chromium
  build remains the suspected cause of the play-gated request-capture failure;
  untested here.
- **Cookie / session login state** — the bot's YouTube cookie jar is logged
  out; no login-state work was performed.
- **Tier above the source ceiling via crafted/stale callback** — the ceiling is
  enforced at menu visibility only; a crafted or stale callback can still
  request such a tier (delivery then verifies the requested tier only).
  Recommended follow-up: narrow validation at the callback handler.

---

## Sign-off

| Gate | Result | Date |
|---|---|---|
| Section A complete (no FAIL, no BLOCKED) | NOT RUN | |
| Section B complete (no FAIL, no BLOCKED) | NOT RUN | |

Any `FAIL` blocks the release until fixed and re-tapped. Any `BLOCKED` must be
listed with its reason in the release notes under "remaining manual items".
