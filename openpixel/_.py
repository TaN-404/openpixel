import asyncio
import json
import re
import time

import httpx
import reflex as rx


STREAMS_API = "https://iptv-org.github.io/api/streams.json"
CHANNELS_API = "https://iptv-org.github.io/api/channels.json"

RESULTS_PER_PAGE = 50
CHANNEL_NUMBER_TIMEOUT_SECONDS = 3
PLAYER_TRANSITION_GRACE_SECONDS = 1.5

# Reused by every browser session while the Reflex backend is running.  The
# catalogue contains public IPTV metadata only; personal number assignments
# are overlaid separately for each browser.
_BASE_CATALOG_CACHE: list[dict[str, str]] = []


def quality_score(quality: object, url: str) -> tuple[int, int, int]:
    """Return a sortable score for selecting a channel's best stream.

    IPTV-org normally describes quality as values such as 720p, 1080p or 4K.
    Resolution is the most important part of the score.  HTTPS and HLS are
    used only as tie-breakers because they are the most browser-friendly.
    """

    quality_text = str(quality or "").strip().lower()
    height = 0

    aliases = {
        "8k": 4320,
        "uhd": 2160,
        "4k": 2160,
        "fhd": 1080,
        "full hd": 1080,
        "hd": 720,
        "sd": 480,
    }

    for label, alias_height in aliases.items():
        if label in quality_text:
            height = max(height, alias_height)

    resolution_match = re.search(r"(?<!\d)(\d{3,4})(?:p|i)?(?!\d)", quality_text)
    if resolution_match:
        height = max(height, int(resolution_match.group(1)))

    is_https = int(url.lower().startswith("https://"))
    is_hls = int(".m3u8" in url.lower())
    return height, is_https, is_hls


def is_direct_browser_stream(stream: dict) -> bool:
    """Keep streams that a browser player can request without custom headers."""

    url = stream.get("url")
    return bool(
        isinstance(url, str)
        and url.startswith(("https://", "http://"))
        and not stream.get("referrer")
        and not stream.get("user_agent")
    )


class VideoState(rx.State):
    """Reactive state for the OpenPixel IPTV player."""

    # Large collections stay on the Python backend.  Only one result page is
    # sent to the browser through `streams`.
    _channel_catalog: list[dict[str, str]] = []
    _filtered_catalog: list[dict[str, str]] = []

    # These guards prevent stale media events and repeated keydown events from
    # making playback rapidly alternate between pause and play.
    _ignore_pause_until: float = 0.0
    _space_key_held: bool = False

    channel_number_buffer: str = ""
    channel_number_message: str = ""
    number_entry_version: int = 0

    numbered_channels: dict[str, dict[str, str]] = {}
    numbered_channels_json: str = rx.LocalStorage(
        "{}",
        name="openpixel_numbered_channels",
        sync=True,
    )

    show_number_dialog: bool = False
    channel_number_input: str = ""
    assignment_error: str = ""
    pending_channel: dict[str, str] = {}

    # `is_playing` is the requested player state. `actual_is_playing` records
    # what the media element most recently reported.
    is_playing: bool = False
    actual_is_playing: bool = False

    selected_index: int = 0
    search_text: str = ""
    current_view: str = "player"

    stream_url: str = ""
    stream_title: str = ""

    # The browser receives at most RESULTS_PER_PAGE channel dictionaries.
    streams: list[dict[str, str]] = []
    page_index: int = 0
    total_results: int = 0
    total_pages: int = 1
    has_previous_page: bool = False
    has_next_page: bool = False

    is_loading: bool = False
    error_message: str = ""

    def load_streams(self):
        """Download IPTV-org data and build the searchable catalogue once."""

        global _BASE_CATALOG_CACHE

        self.restore_numbered_channels()

        if self._channel_catalog:
            self._refresh_visible_channels(reset_page=True)
            return

        self.is_loading = True
        self.error_message = ""
        yield

        try:
            if _BASE_CATALOG_CACHE:
                base_catalog = [dict(channel) for channel in _BASE_CATALOG_CACHE]
            else:
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    streams_response = client.get(STREAMS_API)
                    channels_response = client.get(CHANNELS_API)

                streams_response.raise_for_status()
                channels_response.raise_for_status()

                api_streams = streams_response.json()
                api_channels = channels_response.json()

                channels_by_id = {
                    channel["id"]: channel
                    for channel in api_channels
                    if channel.get("id")
                }

                # IPTV-org can contain several stream URLs for one channel.
                # Keep the entry with the highest declared resolution.
                best_record_by_id: dict[str, dict[str, str]] = {}
                best_score_by_id: dict[str, tuple[int, int, int]] = {}

                for stream in api_streams:
                    channel_id = stream.get("channel")
                    if not channel_id or not is_direct_browser_stream(stream):
                        continue

                    channel_information = channels_by_id.get(channel_id)
                    if not channel_information:
                        continue

                    # Keep the normal catalogue family-friendly by default.
                    if channel_information.get("is_nsfw", False):
                        continue

                    url = stream["url"]
                    score = quality_score(stream.get("quality"), url)

                    if (
                        channel_id in best_score_by_id
                        and score <= best_score_by_id[channel_id]
                    ):
                        continue

                    category_list = channel_information.get("categories") or []
                    best_record_by_id[channel_id] = {
                        "channel_id": channel_id,
                        "title": str(
                            channel_information.get("name")
                            or stream.get("title")
                            or channel_id
                        ),
                        "url": url,
                        "quality": str(stream.get("quality") or "Adaptive/unknown"),
                        "country": str(
                            channel_information.get("country") or "Unknown"
                        ),
                        "categories": (
                            ", ".join(str(item) for item in category_list)
                            if category_list
                            else "Uncategorized"
                        ),
                        "number": "",
                    }
                    best_score_by_id[channel_id] = score

                base_catalog = sorted(
                    best_record_by_id.values(),
                    key=lambda channel: channel["title"].casefold(),
                )
                _BASE_CATALOG_CACHE = [dict(channel) for channel in base_catalog]

            self._install_catalog(base_catalog)

            if not self._channel_catalog:
                self.error_message = "No browser-compatible IPTV channels were found."

        except httpx.HTTPError as error:
            self.error_message = f"Could not download IPTV data: {error}"
        except (TypeError, ValueError, KeyError):
            self.error_message = "The IPTV API returned data in an unexpected format."
        finally:
            self.is_loading = False

    def _install_catalog(self, base_catalog: list[dict[str, str]]):
        """Overlay this browser's saved channel numbers on the shared data."""

        number_by_channel_id = {
            channel.get("channel_id", ""): number
            for number, channel in self.numbered_channels.items()
            if isinstance(channel, dict) and channel.get("channel_id")
        }
        refreshed_assignments = dict(self.numbered_channels)
        installed_catalog: list[dict[str, str]] = []

        for base_channel in base_catalog:
            channel = dict(base_channel)
            saved_number = number_by_channel_id.get(channel["channel_id"], "")
            channel["number"] = saved_number
            installed_catalog.append(channel)

            if saved_number:
                refreshed_assignments[saved_number] = {
                    "channel_id": channel["channel_id"],
                    "title": channel["title"],
                    "url": channel["url"],
                }

        self._channel_catalog = installed_catalog
        self.numbered_channels = refreshed_assignments
        self.numbered_channels_json = json.dumps(refreshed_assignments)
        self._refresh_visible_channels(reset_page=True)

    def update_search(self, value: str):
        """Search the complete backend catalogue, not only the visible page."""

        self.search_text = value
        self._refresh_visible_channels(reset_page=True)

    def _refresh_visible_channels(self, reset_page: bool = False):
        search = self.search_text.strip().casefold()

        if search:
            matches = []
            for channel in self._channel_catalog:
                searchable_text = " ".join(
                    (
                        channel["title"],
                        channel["channel_id"],
                        channel["country"],
                        channel["categories"],
                        channel["quality"],
                        channel["number"],
                    )
                ).casefold()
                if search in searchable_text:
                    matches.append(channel)
        else:
            matches = self._channel_catalog

        self._filtered_catalog = matches
        self.total_results = len(matches)
        self.total_pages = max(
            1,
            (self.total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE,
        )

        if reset_page:
            self.page_index = 0
        else:
            self.page_index = min(self.page_index, self.total_pages - 1)

        self._show_current_page()

    def _show_current_page(self):
        start = self.page_index * RESULTS_PER_PAGE
        end = start + RESULTS_PER_PAGE
        self.streams = self._filtered_catalog[start:end]
        self.selected_index = 0
        self.has_previous_page = self.page_index > 0
        self.has_next_page = self.page_index + 1 < self.total_pages

    def previous_page(self):
        if self.page_index > 0:
            self.page_index -= 1
            self._show_current_page()

    def next_page(self):
        if self.page_index + 1 < self.total_pages:
            self.page_index += 1
            self._show_current_page()

    def open_menu(self):
        self.current_view = "menu"
        self.selected_index = 0

    def close_menu(self):
        self.show_number_dialog = False
        self.current_view = "player"

    def play_stream(self, url: str, title: str):
        """Mount the selected channel and request playback."""

        self.stream_url = url
        self.stream_title = title
        self.current_view = "player"
        self.actual_is_playing = False
        self.is_playing = True
        self._ignore_pause_until = time.monotonic() + PLAYER_TRANSITION_GRACE_SECONDS

    def handle_menu_key(self, key: str, modifiers: dict[str, bool]):
        if not self.streams:
            return

        if key == "ArrowDown":
            self.selected_index = (self.selected_index + 1) % len(self.streams)
        elif key == "ArrowUp":
            self.selected_index = (self.selected_index - 1) % len(self.streams)
        elif key == "Enter":
            selected_channel = self.streams[self.selected_index]
            if modifiers.get("shift_key", False):
                self.open_number_dialog(
                    selected_channel["channel_id"],
                    selected_channel["title"],
                    selected_channel["url"],
                )
            else:
                self.play_stream(
                    selected_channel["url"],
                    selected_channel["title"],
                )

    def handle_global_key(self, key: str, modifiers: dict[str, bool]):
        """Handle application-wide keyboard input."""

        ctrl_pressed = modifiers.get("ctrl_key", False)
        alt_pressed = modifiers.get("alt_key", False)
        meta_pressed = modifiers.get("meta_key", False)

        if ctrl_pressed and key.lower() == "m":
            self.channel_number_buffer = ""
            self.channel_number_message = ""
            self.number_entry_version += 1
            if self.current_view == "player":
                self.open_menu()
            else:
                self.close_menu()
            return

        if key == "Escape":
            if self.show_number_dialog:
                self.close_number_dialog()
            elif self.current_view == "menu":
                self.close_menu()
            else:
                self.channel_number_buffer = ""
                self.channel_number_message = ""
                self.number_entry_version += 1
            return

        if self.current_view != "player":
            return

        if key in (" ", "Space", "Spacebar"):
            # Browsers repeatedly fire keydown while a key is held.  Wait for
            # keyup before accepting another Space press.
            if self._space_key_held:
                return
            self._space_key_held = True
            self.toggle_playback()
            return

        has_modifier = ctrl_pressed or alt_pressed or meta_pressed

        if key.isdigit() and not has_modifier:
            if len(self.channel_number_buffer) >= 3:
                return

            self.channel_number_buffer += key
            self.channel_number_message = ""
            self.number_entry_version += 1
            current_version = self.number_entry_version
            return VideoState.tune_after_delay(current_version)

        if key == "Backspace":
            if not self.channel_number_buffer:
                return

            self.channel_number_buffer = self.channel_number_buffer[:-1]
            self.channel_number_message = ""
            self.number_entry_version += 1

            if self.channel_number_buffer:
                current_version = self.number_entry_version
                return VideoState.tune_after_delay(current_version)
            return

        if key == "Enter" and self.channel_number_buffer:
            self.number_entry_version += 1
            self._tune_to_channel_number()

    def handle_global_key_up(self, key: str, _modifiers: dict[str, bool]):
        if key in (" ", "Space", "Spacebar"):
            self._space_key_held = False

    def toggle_playback(self):
        if self.current_view == "player" and self.stream_url:
            self.is_playing = not self.is_playing

    def player_started(self):
        """Record the real media state without initiating another toggle."""

        self.actual_is_playing = True
        self.is_playing = True

    def player_paused(self):
        """Ignore stale pause events emitted while replacing a stream."""

        self.actual_is_playing = False
        if time.monotonic() >= self._ignore_pause_until:
            self.is_playing = False

    def open_number_dialog(self, channel_id: str, title: str, url: str):
        self.pending_channel = {
            "channel_id": channel_id,
            "title": title,
            "url": url,
        }
        self.channel_number_input = ""
        self.assignment_error = ""

        for number, channel in self.numbered_channels.items():
            if channel.get("channel_id") == channel_id:
                self.channel_number_input = number
                break

        self.show_number_dialog = True

    def close_number_dialog(self):
        self.show_number_dialog = False
        self.channel_number_input = ""
        self.assignment_error = ""
        self.pending_channel = {}

    def update_channel_number(self, value: str):
        digits_only = "".join(character for character in value if character.isdigit())
        self.channel_number_input = digits_only[:3]
        self.assignment_error = ""

    def save_channel_number(self):
        entered_number = self.channel_number_input.strip()
        if not entered_number:
            self.assignment_error = "Enter a channel number."
            return

        channel_number = str(int(entered_number))
        if channel_number == "0":
            self.assignment_error = "Channel numbers must be between 1 and 999."
            return

        if not self.pending_channel:
            self.assignment_error = "Select a channel first."
            return

        existing_channel = self.numbered_channels.get(channel_number)
        if (
            existing_channel
            and existing_channel.get("channel_id")
            != self.pending_channel.get("channel_id")
        ):
            self.assignment_error = (
                f"Channel {channel_number} is already assigned to "
                f"{existing_channel.get('title', 'another channel')}."
            )
            return

        updated_assignments = dict(self.numbered_channels)
        for old_number, channel in list(updated_assignments.items()):
            if channel.get("channel_id") == self.pending_channel["channel_id"]:
                del updated_assignments[old_number]

        updated_assignments[channel_number] = dict(self.pending_channel)
        self.numbered_channels = updated_assignments
        self.numbered_channels_json = json.dumps(updated_assignments)

        updated_catalog = []
        for channel in self._channel_catalog:
            updated_channel = dict(channel)
            if updated_channel["channel_id"] == self.pending_channel["channel_id"]:
                updated_channel["number"] = channel_number
            updated_catalog.append(updated_channel)

        self._channel_catalog = updated_catalog
        self._refresh_visible_channels(reset_page=False)
        self.close_number_dialog()

    def handle_assignment_key(self, key: str):
        if key == "Enter":
            self.save_channel_number()

    def _tune_to_channel_number(self):
        if not self.channel_number_buffer:
            return

        channel_number = str(int(self.channel_number_buffer))
        assigned_channel = self.numbered_channels.get(channel_number)

        if not assigned_channel:
            self.channel_number_message = f"Channel {channel_number} is not assigned."
            self.channel_number_buffer = ""
            return

        self.stream_url = assigned_channel["url"]
        self.stream_title = assigned_channel["title"]
        self.actual_is_playing = False
        self.is_playing = True
        self._ignore_pause_until = time.monotonic() + PLAYER_TRANSITION_GRACE_SECONDS
        self.channel_number_buffer = ""
        self.channel_number_message = ""

    def restore_numbered_channels(self):
        try:
            stored_data = json.loads(self.numbered_channels_json or "{}")
            if isinstance(stored_data, dict):
                self.numbered_channels = {
                    str(number): channel
                    for number, channel in stored_data.items()
                    if isinstance(channel, dict)
                }
            else:
                self.numbered_channels = {}
        except (json.JSONDecodeError, TypeError):
            self.numbered_channels = {}
            self.numbered_channels_json = "{}"

    @rx.event(background=True)
    async def tune_after_delay(self, timer_version: int):
        """Tune three seconds after the most recent number key."""

        await asyncio.sleep(CHANNEL_NUMBER_TIMEOUT_SECONDS)
        async with self:
            if timer_version != self.number_entry_version:
                return
            if self.channel_number_buffer:
                self._tune_to_channel_number()


def channel_number_overlay() -> rx.Component:
    return rx.cond(
        VideoState.channel_number_buffer != "",
        rx.box(
            rx.text(VideoState.channel_number_buffer, size="8", weight="bold"),
            position="fixed",
            top="30px",
            right="30px",
            padding="15px 25px",
            background_color="rgba(0, 0, 0, 0.80)",
            color="white",
            border_radius="10px",
            z_index="1000",
        ),
        rx.cond(
            VideoState.channel_number_message != "",
            rx.box(
                rx.text(VideoState.channel_number_message, weight="bold"),
                position="fixed",
                top="30px",
                right="30px",
                padding="15px 25px",
                background_color="var(--red-9)",
                color="white",
                border_radius="10px",
                z_index="1000",
            ),
        ),
    )


def channel_number_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Assign channel number"),
            rx.dialog.description(
                "Assign a number between 1 and 999 to ",
                VideoState.pending_channel["title"],
                ".",
            ),
            rx.input(
                placeholder="Example: 101",
                value=VideoState.channel_number_input,
                on_change=VideoState.update_channel_number,
                on_key_down=VideoState.handle_assignment_key,
                input_mode="numeric",
                max_length=3,
                auto_focus=True,
                width="100%",
            ),
            rx.cond(
                VideoState.assignment_error != "",
                rx.callout(
                    VideoState.assignment_error,
                    icon="triangle-alert",
                    color_scheme="red",
                    width="100%",
                ),
            ),
            rx.hstack(
                rx.spacer(),
                rx.button(
                    "Cancel",
                    on_click=VideoState.close_number_dialog,
                    variant="soft",
                    color_scheme="gray",
                ),
                rx.button("Save Number", on_click=VideoState.save_channel_number),
                width="100%",
                margin_top="20px",
            ),
        ),
        open=VideoState.show_number_dialog,
    )


def channel_button(
    stream: rx.Var[dict[str, str]],
    index: rx.Var[int],
) -> rx.Component:
    is_selected = index == VideoState.selected_index

    return rx.card(
        rx.hstack(
            rx.cond(is_selected, rx.icon("chevron-right"), rx.box(width="24px")),
            rx.vstack(
                rx.text(stream["title"], weight="bold"),
                rx.hstack(
                    rx.hstack(
                        rx.text(stream["country"], size="1", color_scheme="gray"),
                        rx.text("•", size="1", color_scheme="gray"),
                        rx.text(stream["categories"], size="1", color_scheme="gray"),
                        rx.text("•", size="1", color_scheme="gray"),
                        rx.text(stream["quality"], size="1", color_scheme="gray"),
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.cond(
                        stream["number"] != "",
                        rx.badge(
                            rx.text("Channel ", stream["number"]),
                            color_scheme="blue",
                        ),
                        rx.badge("No number", color_scheme="gray"),
                    ),
                ),
                align_items="start",
                spacing="1",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("hash"),
                "Assign",
                on_click=VideoState.open_number_dialog(
                    stream["channel_id"], stream["title"], stream["url"]
                ),
                variant="soft",
            ),
            rx.button(
                rx.icon("play"),
                "Play",
                on_click=VideoState.play_stream(stream["url"], stream["title"]),
            ),
            width="100%",
            align_items="center",
        ),
        background_color=rx.cond(is_selected, "var(--accent-5)", "var(--gray-2)"),
        border=rx.cond(
            is_selected,
            "2px solid var(--accent-9)",
            "2px solid transparent",
        ),
        width="100%",
        padding="10px",
    )


def player_view() -> rx.Component:
    return rx.center(
        rx.vstack(
            channel_number_overlay(),
            rx.hstack(
                rx.heading("📺 OpenPixel TV", size="7"),
                rx.spacer(),
                rx.button(
                    rx.icon("menu"),
                    "Channel Menu",
                    rx.text.kbd("Ctrl"),
                    "+",
                    rx.text.kbd("M"),
                    on_click=VideoState.open_menu,
                ),
                width="100%",
                align_items="center",
            ),
            rx.cond(
                VideoState.stream_url != "",
                rx.vstack(
                    rx.video(
                        url=VideoState.stream_url,
                        key=VideoState.stream_url,
                        controls=True,
                        playing=VideoState.is_playing,
                        on_play=VideoState.player_started,
                        on_pause=VideoState.player_paused,
                        id="tv-player",
                        width="100%",
                        height="auto",
                        aspect_ratio="16 / 9",
                    ),
                    rx.hstack(
                        rx.text(VideoState.stream_title, weight="bold", size="4"),
                        rx.spacer(),
                        rx.text("Space: pause/play", color_scheme="gray", size="2"),
                        width="100%",
                    ),
                    width="100%",
                    align_items="center",
                    spacing="3",
                ),
                rx.center(
                    rx.vstack(
                        rx.icon("tv", size=60, color="var(--gray-8)"),
                        rx.text(
                            "Open the menu and select a channel.",
                            color_scheme="gray",
                        ),
                        align_items="center",
                        spacing="3",
                    ),
                    width="100%",
                    aspect_ratio="16 / 9",
                    background_color="var(--gray-3)",
                    border_radius="10px",
                ),
            ),
            width="100%",
            max_width="1000px",
            spacing="4",
        ),
        width="100vw",
        min_height="100vh",
        padding="20px",
        background_color="var(--gray-2)",
    )


def pagination_controls() -> rx.Component:
    return rx.hstack(
        rx.button(
            rx.icon("chevron-left"),
            "Previous",
            on_click=VideoState.previous_page,
            disabled=~VideoState.has_previous_page,
            variant="soft",
        ),
        rx.spacer(),
        rx.text(
            "Page ",
            VideoState.page_index + 1,
            " of ",
            VideoState.total_pages,
            " • ",
            VideoState.total_results,
            " channels",
            color_scheme="gray",
        ),
        rx.spacer(),
        rx.button(
            "Next",
            rx.icon("chevron-right"),
            on_click=VideoState.next_page,
            disabled=~VideoState.has_next_page,
            variant="soft",
        ),
        width="100%",
        align_items="center",
    )


def menu_view() -> rx.Component:
    return rx.container(
        rx.vstack(
            rx.hstack(
                rx.heading("Channels", size="7"),
                rx.spacer(),
                rx.button(
                    rx.icon("arrow-left"),
                    "Back to TV",
                    rx.text.kbd("Esc"),
                    on_click=VideoState.close_menu,
                    variant="soft",
                ),
                width="100%",
                align_items="center",
            ),
            rx.input(
                placeholder="Search name, country, category, quality, ID or number...",
                value=VideoState.search_text,
                on_change=VideoState.update_search,
                on_key_down=VideoState.handle_menu_key,
                auto_focus=True,
                width="100%",
                size="3",
            ),
            rx.cond(
                VideoState.is_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size="3"),
                        rx.text("Loading the IPTV catalogue..."),
                        align_items="center",
                        spacing="3",
                    ),
                    width="100%",
                    padding="40px",
                ),
                rx.cond(
                    VideoState.streams.length() > 0,
                    rx.vstack(
                        pagination_controls(),
                        rx.foreach(VideoState.streams, channel_button),
                        pagination_controls(),
                        width="100%",
                        spacing="2",
                    ),
                    rx.center(
                        rx.text("No matching channels found.", color_scheme="gray"),
                        width="100%",
                        padding="40px",
                    ),
                ),
            ),
            rx.cond(
                VideoState.error_message != "",
                rx.callout(
                    VideoState.error_message,
                    icon="triangle-alert",
                    color_scheme="red",
                    width="100%",
                ),
            ),
            width="100%",
            spacing="4",
            padding_y="30px",
        ),
        max_width="1000px",
    )


def index() -> rx.Component:
    return rx.fragment(
        rx.window_event_listener(
            on_key_down=VideoState.handle_global_key,
            on_key_up=VideoState.handle_global_key_up,
        ),
        rx.cond(
            VideoState.current_view == "player",
            player_view(),
            menu_view(),
        ),
        channel_number_dialog(),
    )


app = rx.App()
app.add_page(
    index,
    route="/",
    title="OpenPixel TV",
    on_load=VideoState.load_streams,
)
