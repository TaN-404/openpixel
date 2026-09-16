# OpenPixel TV

OpenPixel TV is a browser-based IPTV player built entirely with Python and
[Reflex](https://reflex.dev/). It downloads public channel and stream metadata
from [IPTV-org](https://github.com/iptv-org/iptv), presents it as a searchable
TV guide, and lets each user build a personal numbered channel list.

The interface is designed to behave more like a television than a conventional
web page: the player fills the viewport, menus appear as translucent overlays,
and assigned channels can be selected by typing their number.

## Features

- Full-viewport video player with native playback controls.
- Searchable IPTV catalogue using IPTV-org channel and stream data.
- Highest advertised-quality stream selected when a channel has multiple
  compatible sources.
- Search by channel name, ID, country, category, quality, or assigned number.
- Paginated catalogue with 50 channels rendered at a time.
- Mouse and keyboard navigation.
- Personal channel numbers from `1` to `999`.
- Three-second channel-number entry timeout.
- Immediate channel switching by pressing `Enter`.
- A dedicated **My Channels** directory for assigned channels.
- Channel assignments saved in browser local storage.
- Automatic startup playback of the lowest assigned channel number.
- Satellite-TV-style channel-number overlay.
- Translucent channel menus that leave the video mounted and playing behind
  them.
- Browser fullscreen button.
- Protection against repeated Space-key events and stale pause events during
  channel changes.
- Dark responsive interface with one-, two-, and three-column guide layouts.

## Keyboard controls

### Player view

| Input | Action |
| --- | --- |
| `Alt + M` | Open or close the complete channel guide |
| `Alt + L` | Open or close **My Channels** |
| `Space` | Pause or resume playback |
| Number keys | Enter an assigned channel number |
| `Enter` | Switch immediately to the entered channel number |
| `Backspace` | Remove the most recently entered digit |
| `Escape` | Clear the current channel-number entry |

If `Enter` is not pressed, OpenPixel automatically tries the entered channel
number three seconds after the last digit.

### Complete channel guide

| Input | Action |
| --- | --- |
| `Arrow Up` / `Arrow Down` | Move through the visible search results |
| `Enter` | Play the selected channel |
| `Shift + Enter` | Open the number-assignment dialog |
| `Escape` | Return to the player |

The guide also supports mouse selection, **Play**, **Assign**, searching, and
page navigation.

### My Channels

| Input | Action |
| --- | --- |
| `Arrow Up` / `Arrow Down` | Move through assigned channels |
| `Enter` | Play the selected channel |
| `Escape` | Return to the player |

The list can be searched by channel number or name.

## How channel numbering works

1. Open the channel guide with `Alt + M`.
2. Find a channel using search or the page controls.
3. Select **Assign**, or highlight it and press `Shift + Enter`.
4. Enter a number from `1` to `999`.
5. Select **Save Number**.
6. Return to the player and type that number to switch channels.

A number cannot be assigned to two different channels. Reassigning an existing
channel replaces its previous number. Assignments are stored in the current
browser, so separate browsers or browser profiles maintain separate lists.

## Requirements

- Python 3.10 or newer. The project has been developed with Python 3.12.
- Reflex.
- HTTPX.
- Node.js requirements installed automatically by Reflex.
- An internet connection for downloading IPTV metadata and playing streams.

## Installation

Clone or create the Reflex project, then move into its root directory:

```bash
cd openpixel
```

Create and activate a virtual environment:

```bash
python3 -m venv env
source env/bin/activate
```

On Windows PowerShell, activate it with:

```powershell
env\Scripts\Activate.ps1
```

Install the Python dependencies:

```bash
pip install reflex httpx
```

If this is a new directory rather than an existing Reflex project, initialize
it first:

```bash
reflex init
```

Place the application file at:

```text
openpixel/
├── openpixel/
│   └── openpixel.py
├── rxconfig.py
└── requirements.txt
```

## Running the application

From the project root, with the virtual environment activated, run:

```bash
reflex run
```

Open the frontend address printed in the terminal. Reflex commonly uses:

```text
http://localhost:3000
```

The first catalogue load can take longer because the backend downloads and
combines the IPTV-org channel and stream datasets. Later sessions reuse the
in-memory catalogue while the backend process remains running.

## Data flow

OpenPixel reads two public IPTV-org endpoints:

- `https://iptv-org.github.io/api/channels.json`
- `https://iptv-org.github.io/api/streams.json`

During loading, the application:

1. Creates a lookup table from the channel metadata.
2. Matches every usable stream with its channel information.
3. Excludes NSFW entries.
4. Excludes streams that require custom referrer or user-agent headers, since
   the browser player cannot provide them directly.
5. Compares duplicate sources and retains the stream with the highest
   advertised resolution.
6. Uses HTTPS and HLS as tie-breakers when advertised quality is equal.
7. Sorts the resulting catalogue alphabetically.
8. Keeps the complete catalogue on the Python backend and sends only the
   current 50-channel page to the browser.

This backend-windowing approach keeps the guide responsive without discarding
the rest of the searchable catalogue.

## Project architecture

The current version is intentionally contained in one Python module while the
prototype is evolving.

| Part | Responsibility |
| --- | --- |
| `VideoState` | Reactive application state and event handlers |
| `_BASE_CATALOG_CACHE` | Shared in-memory catalogue cache for the running backend |
| `_channel_catalog` | Complete searchable catalogue kept on the backend |
| `streams` | Current 50-channel page sent to the frontend |
| `numbered_channels_json` | Browser-local persistent number assignments |
| `player_view()` | Full-viewport video player and player overlays |
| `menu_view()` | Complete searchable channel guide |
| `assigned_view()` | Searchable list of numbered channels |
| `channel_number_dialog()` | Number assignment and validation |

The player is always mounted. Opening either guide draws an overlay above it
instead of replacing it, which allows the selected stream to continue playing
while the user browses channels.

Most behavior uses Reflex state and events. Small browser scripts are used only
for browser-specific behavior such as the Fullscreen API, removing native video
keyboard focus, and scrolling the keyboard-selected guide item into view.

## Stream quality

For duplicate sources belonging to the same channel, OpenPixel scores quality
labels such as `720p`, `1080p`, `FHD`, `4K`, and `8K`. The source with the
highest advertised resolution is selected. HTTPS and HLS are preferred only
when resolution scores are equal.

This provides the best source described by the IPTV metadata, but it cannot
increase the source's real resolution or bitrate. Adaptive HLS playlists may
also change playback quality according to bandwidth and provider behavior.

## Storage and privacy

Channel-number assignments are stored in browser local storage under:

```text
openpixel_numbered_channels
```

The assignments are not stored in a database or synchronized between devices.
Clearing the site's browser data will remove them.

## Known limitations

- OpenPixel does not host, rebroadcast, or control any IPTV stream.
- Public stream URLs can stop working, move, become geoblocked, or buffer.
- Some providers may block playback because of CORS or regional restrictions.
- HTTP streams may work during local development but be blocked as mixed
  content when OpenPixel is deployed over HTTPS.
- Browser autoplay rules may require one initial user interaction before audio
  playback is permitted.
- The quality label comes from IPTV metadata and is not independently measured.
- Assignments are browser-local and have no backup or account synchronization.
- True browser fullscreen must be initiated by the user; it cannot be entered
  automatically on page load.

## Troubleshooting

### The application still shows the Reflex welcome page

Make sure the code is inside the package shown by the welcome page, normally:

```text
openpixel/openpixel.py
```

Run `reflex run` from the directory containing `rxconfig.py`, then perform a
hard browser refresh.

### A channel is listed but does not play

The public source may be offline, geoblocked, rejecting browser requests, or
blocked as mixed content. Try another channel and inspect the browser developer
console for network or CORS errors.

### Audio does not start automatically

Click the video or its native Play control once. Browsers commonly restrict
autoplay with audio until the user interacts with the page.

### My assigned channels disappeared

Assignments belong to the browser profile that created them. Check that you are
using the same browser/profile and have not cleared the site's local storage.

### Space pauses and immediately resumes

Make sure the current version of the application is running. It debounces the
global Space handler and removes keyboard focus from the native video element
to prevent two independent playback toggles.

## Suggested next steps

- Split the single module into API, state, component, and utility modules.
- Add channel logos and programme-guide/EPG data.
- Add country and category filters.
- Add export and import for numbered-channel assignments.
- Add stream health checks and fallback sources.
- Add configurable startup and last-watched channels.
- Add user accounts only if cross-device synchronization becomes necessary.
- Add automated tests for searching, quality scoring, pagination, and channel
  assignment conflicts.

## Responsible use

Use only streams that you are legally permitted to access in your location.
OpenPixel is a player and catalogue interface; availability and rights remain
the responsibility of each stream provider and user.

## Acknowledgements

- [Reflex](https://reflex.dev/) for the Python web framework.
- [IPTV-org](https://github.com/iptv-org) for the public channel and stream
  metadata.

