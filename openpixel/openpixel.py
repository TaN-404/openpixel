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

    # This guard prevents stale media events from making playback rapidly
    # alternate between pause and play while a stream is being replaced.
    _ignore_pause_until: float = 0.0

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

    # State for the "assigned channels" directory (Alt+L), a small,
    # search-and-arrow-key-navigable list built from `numbered_channels`
    # rather than the full IPTV catalogue.
    _assigned_catalog: list[dict[str, str]] = []
    assigned_streams: list[dict[str, str]] = []
    assigned_search_text: str = ""
    assigned_selected_index: int = 0

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
        self._play_smallest_numbered_channel()

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

    def update_assigned_search(self, value: str):
        """Filter the assigned-channels directory by number or name."""

        self.assigned_search_text = value
        self._refresh_assigned_channels(reset_selection=True)

    def _build_assigned_catalog(self) -> list[dict[str, str]]:
        catalog = [
            {
                "number": number,
                "title": str(channel.get("title", "")),
                "channel_id": str(channel.get("channel_id", "")),
                "url": str(channel.get("url", "")),
            }
            for number, channel in self.numbered_channels.items()
            if isinstance(channel, dict) and channel.get("url")
        ]
        catalog.sort(key=lambda channel: int(channel["number"]))
        return catalog

    def _refresh_assigned_channels(self, reset_selection: bool = False):
        self._assigned_catalog = self._build_assigned_catalog()
        search = self.assigned_search_text.strip().casefold()

        if search:
            matches = [
                channel
                for channel in self._assigned_catalog
                if search in channel["number"]
                or search in channel["title"].casefold()
            ]
        else:
            matches = self._assigned_catalog

        self.assigned_streams = matches

        if reset_selection or self.assigned_selected_index >= len(matches):
            self.assigned_selected_index = 0

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

    def select_channel(self, index: int):
        """Highlight a channel by clicking/hovering it in the grid."""

        self.selected_index = index

    def open_assigned_list(self):
        """Show the small directory of channels that have a number assigned."""

        self.current_view = "assigned"
        self.assigned_search_text = ""
        self._refresh_assigned_channels(reset_selection=True)

    def select_assigned_channel(self, index: int):
        """Highlight a row by clicking/hovering it in the assigned list."""

        self.assigned_selected_index = index

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
            return
        else:
            return

        # Arrow-key navigation moves `selected_index` on the backend, but the
        # browser has no reason to scroll the grid on its own. Ask the
        # newly-selected tile to bring itself into view once the DOM has
        # actually updated with the new selection.
        yield rx.call_script(
            """
            requestAnimationFrame(() => {
                const selected = document.querySelector(
                    '[data-channel-selected="true"]'
                );
                if (selected) {
                    selected.scrollIntoView({block: "nearest", behavior: "smooth"});
                }
            });
            """
        )

    def handle_assigned_key(self, key: str, modifiers: dict[str, bool]):
        if not self.assigned_streams:
            return

        if key == "ArrowDown":
            self.assigned_selected_index = (
                self.assigned_selected_index + 1
            ) % len(self.assigned_streams)
        elif key == "ArrowUp":
            self.assigned_selected_index = (
                self.assigned_selected_index - 1
            ) % len(self.assigned_streams)
        elif key == "Enter":
            selected_channel = self.assigned_streams[self.assigned_selected_index]
            self.play_stream(selected_channel["url"], selected_channel["title"])
            return
        else:
            return

        yield rx.call_script(
            """
            requestAnimationFrame(() => {
                const selected = document.querySelector(
                    '[data-assigned-selected="true"]'
                );
                if (selected) {
                    selected.scrollIntoView({block: "nearest", behavior: "smooth"});
                }
            });
            """
        )

    def handle_global_key(self, key: str, modifiers: dict[str, bool]):
        """Handle application-wide keyboard input."""

        ctrl_pressed = modifiers.get("ctrl_key", False)
        alt_pressed = modifiers.get("alt_key", False)
        meta_pressed = modifiers.get("meta_key", False)

        if alt_pressed and key.lower() == "m":
            self.channel_number_buffer = ""
            self.channel_number_message = ""
            self.number_entry_version += 1
            if self.current_view == "menu":
                self.close_menu()
            else:
                self.open_menu()
            return

        if alt_pressed and key.lower() == "l":
            self.channel_number_buffer = ""
            self.channel_number_message = ""
            self.number_entry_version += 1
            if self.current_view == "assigned":
                self.close_menu()
            else:
                self.open_assigned_list()
            return

        if key == "Escape":
            if self.show_number_dialog:
                self.close_number_dialog()
            elif self.current_view in ("menu", "assigned"):
                self.close_menu()
            else:
                self.channel_number_buffer = ""
                self.channel_number_message = ""
                self.number_entry_version += 1
            return

        if self.current_view != "player":
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

    def handle_playback_key(self, key: str, modifiers: dict[str, bool]):
        """Handle Space separately so repeated keydown events can be debounced."""

        has_modifier = (
            modifiers.get("ctrl_key", False)
            or modifiers.get("alt_key", False)
            or modifiers.get("meta_key", False)
        )

        if key in (" ", "Space", "Spacebar") and not has_modifier:
            self.toggle_playback()

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

    def _play_smallest_numbered_channel(self):
        """Auto-tune to the lowest assigned channel number on app start.

        Only runs when nothing is playing yet, so it acts purely as a
        startup default and never interrupts a channel the user already
        chose (e.g. if `load_streams` were ever triggered again later).
        """

        if self.stream_url or not self.numbered_channels:
            return

        numbers = [
            number
            for number, channel in self.numbered_channels.items()
            if isinstance(channel, dict) and channel.get("url") and number.isdigit()
        ]
        if not numbers:
            return

        smallest_number = min(numbers, key=int)
        channel = self.numbered_channels[smallest_number]

        self.stream_url = channel["url"]
        self.stream_title = str(channel.get("title", ""))
        self.actual_is_playing = False
        self.is_playing = True
        self._ignore_pause_until = time.monotonic() + PLAYER_TRANSITION_GRACE_SECONDS

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
            rx.vstack(
                rx.text(
                    "CHANNEL",
                    size="2",
                    weight="bold",
                    color="rgba(255,255,255,0.7)",
                ),
                rx.text(
                    VideoState.channel_number_buffer,
                    size="9",
                    weight="bold",
                    letter_spacing="6px",
                ),
                spacing="0",
                align_items="end",
            ),
            position="absolute",
            top="40px",
            right="40px",
            padding="14px 22px",
            background_color="rgba(0, 0, 0, 0.78)",
            color="white",
            border_radius="10px",
            border="1px solid rgba(255,255,255,0.15)",
            box_shadow="0 8px 30px rgba(0,0,0,0.45)",
            z_index="20",
            pointer_events="none",
        ),
        rx.cond(
            VideoState.channel_number_message != "",
            rx.box(
                rx.text(
                    VideoState.channel_number_message,
                    weight="bold",
                ),
                position="absolute",
                top="40px",
                right="40px",
                padding="14px 22px",
                background_color="rgba(180, 20, 20, 0.90)",
                color="white",
                border_radius="10px",
                z_index="20",
                pointer_events="none",
            ),
        ),
    )

def player_top_overlay() -> rx.Component:
    return rx.hstack(
        rx.heading(
            "📺 OpenPixel TV",
            size="6",
            color="white",
            text_shadow="0 2px 8px black",
        ),

        rx.spacer(),

        rx.button(
            rx.icon("list-ordered"),
            "My Channels",
            rx.text.kbd("Alt"),
            "+",
            rx.text.kbd("L"),
            on_click=VideoState.open_assigned_list,
            variant="soft",
            color_scheme="gray",
        ),

        rx.button(
            rx.icon("menu"),
            "Channels",
            rx.text.kbd("Alt"),
            "+",
            rx.text.kbd("M"),
            on_click=VideoState.open_menu,
            variant="soft",
            color_scheme="gray",
        ),

        rx.button(
            rx.icon("maximize"),
            on_click=rx.call_script(
                """
                const player = document.getElementById("player-stage");
                if (player && !document.fullscreenElement) {
                    player.requestFullscreen();
                } else if (document.fullscreenElement) {
                    document.exitFullscreen();
                }
                """
            ),
            variant="soft",
            color_scheme="gray",
            aria_label="Toggle fullscreen",
        ),

        position="absolute",
        top="0",
        left="0",
        right="0",
        z_index="15",
        padding="24px 30px 55px",
        align_items="center",
        background=(
            "linear-gradient("
            "to bottom, "
            "rgba(0,0,0,0.85), "
            "rgba(0,0,0,0)"
            ")"
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

    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.icon(
                    "tv",
                    size=18,
                    color=rx.cond(is_selected, "var(--accent-11)", "var(--gray-9)"),
                    flex_shrink="0",
                ),
                rx.text(
                    stream["title"],
                    weight="bold",
                    size="3",
                    overflow="hidden",
                    text_overflow="ellipsis",
                    white_space="nowrap",
                ),
                spacing="2",
                align_items="center",
                width="100%",
            ),
            rx.hstack(
                rx.badge(stream["country"], variant="surface", color_scheme="gray"),
                rx.badge(stream["quality"], variant="surface", color_scheme="gray"),
                spacing="2",
                wrap="wrap",
            ),
            rx.text(
                stream["categories"],
                size="1",
                color_scheme="gray",
                overflow="hidden",
                text_overflow="ellipsis",
                white_space="nowrap",
                width="100%",
            ),
            rx.spacer(),
            rx.cond(
                stream["number"] != "",
                rx.badge(
                    rx.text("Channel ", stream["number"]),
                    color_scheme="blue",
                    variant="soft",
                ),
                rx.badge("Unassigned", color_scheme="gray", variant="soft"),
            ),
            rx.hstack(
                rx.button(
                    rx.icon("hash", size=15),
                    "Assign",
                    on_click=VideoState.open_number_dialog(
                        stream["channel_id"], stream["title"], stream["url"]
                    ),
                    variant="soft",
                    color_scheme="gray",
                    size="2",
                    flex="1",
                ),
                rx.button(
                    rx.icon("play", size=15),
                    "Play",
                    on_click=VideoState.play_stream(stream["url"], stream["title"]),
                    size="2",
                    flex="1",
                ),
                width="100%",
                spacing="2",
            ),
            align_items="start",
            spacing="3",
            height="100%",
            width="100%",
        ),
        # Marks the tile the keyboard cursor is currently on, so arrow-key
        # navigation can find it and scroll it into view (see
        # handle_menu_key), and so it can be styled as selected.
        custom_attrs={"data-channel-selected": rx.cond(is_selected, "true", "false")},
        on_click=VideoState.select_channel(index),
        background=rx.cond(is_selected, "var(--accent-a5)", "var(--gray-a4)"),
        border=rx.cond(
            is_selected,
            "2px solid var(--accent-9)",
            "2px solid var(--gray-a6)",
        ),
        box_shadow=rx.cond(is_selected, "0 0 0 4px var(--accent-a4)", "none"),
        border_radius="16px",
        padding="16px",
        cursor="pointer",
        transition="transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease",
        _hover={
            "border_color": "var(--accent-8)",
            "transform": "translateY(-3px)",
            "box_shadow": "0 10px 28px rgba(0,0,0,0.35)",
        },
        width="100%",
        height="100%",
    )



def player_view() -> rx.Component:
    """Fullscreen television/player view."""

    return rx.box(
        # Video or empty-player background.
        rx.cond(
            VideoState.stream_url != "",
            rx.video(
                src=VideoState.stream_url,
                key=VideoState.stream_url,
                controls=True,
                playing=VideoState.is_playing,
                on_play=VideoState.player_started,
                on_pause=VideoState.player_paused,
                id="tv-player",

                position="absolute",
                inset="0",
                width="100%",
                height="100%",
                object_fit="contain",
                background_color="black",
            ),
            rx.center(
                rx.vstack(
                    rx.icon(
                        "tv",
                        size=80,
                        color="var(--gray-8)",
                    ),
                    rx.text(
                        "Press Alt + M to select a channel",
                        color="white",
                        size="4",
                    ),
                    rx.text(
                        "Press Alt + L to jump to your assigned channels",
                        color="var(--gray-9)",
                        size="2",
                    ),
                    align_items="center",
                    spacing="4",
                ),
                position="absolute",
                inset="0",
                background_color="black",
            ),
        ),

        # Everything below is drawn over the video.
        player_top_overlay(),
        channel_number_overlay(),

        # Channel title above the native video controls.
        rx.cond(
            VideoState.stream_title != "",
            rx.box(
                rx.text(
                    VideoState.stream_title,
                    size="5",
                    weight="bold",
                    color="white",
                    text_shadow="0 2px 8px black",
                ),
                position="absolute",
                left="0",
                right="0",
                bottom="0",
                z_index="10",
                padding="70px 30px 70px",
                pointer_events="none",
                background=(
                    "linear-gradient("
                    "to top, "
                    "rgba(0,0,0,0.80), "
                    "rgba(0,0,0,0)"
                    ")"
                ),
            ),
        ),

        id="player-stage",

        # This makes the player occupy the complete browser viewport.
        position="relative",
        width="100vw",
        height="100dvh",
        overflow="hidden",
        background_color="black",
    )


def pagination_controls() -> rx.Component:
    return rx.hstack(
        rx.button(
            rx.icon("chevron-left", size=16),
            "Previous",
            on_click=VideoState.previous_page,
            disabled=~VideoState.has_previous_page,
            variant="soft",
            color_scheme="gray",
            size="2",
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
            size="2",
            color_scheme="gray",
        ),
        rx.spacer(),
        rx.button(
            "Next",
            rx.icon("chevron-right", size=16),
            on_click=VideoState.next_page,
            disabled=~VideoState.has_next_page,
            variant="soft",
            color_scheme="gray",
            size="2",
        ),
        width="100%",
        align_items="center",
    )


def menu_view() -> rx.Component:
    return rx.box(
        # Header and search stay put; only the channel grid below scrolls.
        # Keeping the app inside its own flex/overflow container (instead of
        # relying on page-level scroll) is also what makes the arrow-key
        # scrollIntoView behaviour in handle_menu_key reliable.
        rx.box(
            rx.vstack(
                rx.hstack(
                    rx.heading("📺 Channels", size="7"),
                    rx.spacer(),
                    rx.button(
                        rx.icon("arrow-left"),
                        "Back to TV",
                        rx.text.kbd("Esc"),
                        on_click=VideoState.close_menu,
                        variant="soft",
                        color_scheme="gray",
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
                    radius="large",
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
            ),
            width="100%",
            max_width="1400px",
            margin="0 auto",
            padding="28px 32px 20px",
        ),
        rx.box(
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
                    padding="60px",
                ),
                rx.cond(
                    VideoState.streams.length() > 0,
                    rx.vstack(
                        rx.box(
                            pagination_controls(),
                            width="100%",
                            max_width="1400px",
                            margin="0 auto",
                        ),
                        rx.grid(
                            rx.foreach(VideoState.streams, channel_button),
                            columns={"initial": "1", "sm": "2", "lg": "3"},
                            spacing="4",
                            width="100%",
                            max_width="1400px",
                            margin="0 auto",
                        ),
                        rx.box(
                            pagination_controls(),
                            width="100%",
                            max_width="1400px",
                            margin="0 auto",
                        ),
                        width="100%",
                        spacing="4",
                        align_items="center",
                    ),
                    rx.center(
                        rx.text("No matching channels found.", color_scheme="gray"),
                        width="100%",
                        padding="60px",
                    ),
                ),
            ),
            id="channel-scroll-area",
            width="100%",
            flex="1 1 auto",
            overflow_y="auto",
            padding="4px 32px 40px",
        ),
        # Fixed + translucent + blurred so it reads as a channel-guide
        # overlay (Tata Sky / DTH style) sitting on top of the still-playing
        # video, rather than a separate opaque page.
        position="fixed",
        inset="0",
        z_index="30",
        display="flex",
        flex_direction="column",
        background="rgba(6, 9, 16, 0.80)",
        backdrop_filter="blur(22px) saturate(140%)",
    )


def assigned_channel_row(
    channel: rx.Var[dict[str, str]],
    index: rx.Var[int],
) -> rx.Component:
    is_selected = index == VideoState.assigned_selected_index

    return rx.box(
        rx.hstack(
            rx.box(
                rx.text(
                    channel["number"],
                    size="6",
                    weight="bold",
                    color=rx.cond(
                        is_selected, "var(--accent-11)", "var(--gray-11)"
                    ),
                ),
                min_width="56px",
                text_align="center",
            ),
            rx.box(width="1px", height="32px", background="var(--gray-a6)"),
            rx.vstack(
                rx.text(
                    channel["title"],
                    weight="bold",
                    size="3",
                    overflow="hidden",
                    text_overflow="ellipsis",
                    white_space="nowrap",
                    width="100%",
                ),
                rx.text(
                    channel["channel_id"],
                    size="1",
                    color_scheme="gray",
                ),
                align_items="start",
                spacing="0",
                min_width="0",
                flex="1",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("play", size=15),
                "Play",
                on_click=VideoState.play_stream(channel["url"], channel["title"]),
                size="2",
                flex_shrink="0",
            ),
            width="100%",
            align_items="center",
            spacing="4",
        ),
        # Marks the row the keyboard cursor is on, so arrow-key navigation
        # (handle_assigned_key) can scroll it into view.
        custom_attrs={"data-assigned-selected": rx.cond(is_selected, "true", "false")},
        on_click=VideoState.select_assigned_channel(index),
        background=rx.cond(is_selected, "var(--accent-a5)", "var(--gray-a4)"),
        border=rx.cond(
            is_selected,
            "2px solid var(--accent-9)",
            "2px solid var(--gray-a6)",
        ),
        border_radius="14px",
        padding="12px 18px",
        cursor="pointer",
        transition="border-color 0.15s ease, background 0.15s ease",
        _hover={"border_color": "var(--accent-8)"},
        width="100%",
    )


def assigned_view() -> rx.Component:
    """A small directory of only the channels the user has numbered."""

    return rx.box(
        rx.box(
            rx.vstack(
                rx.hstack(
                    rx.heading("🔢 My Channels", size="7"),
                    rx.spacer(),
                    rx.button(
                        rx.icon("arrow-left"),
                        "Back to TV",
                        rx.text.kbd("Esc"),
                        on_click=VideoState.close_menu,
                        variant="soft",
                        color_scheme="gray",
                    ),
                    width="100%",
                    align_items="center",
                ),
                rx.input(
                    placeholder="Search by number or name...",
                    value=VideoState.assigned_search_text,
                    on_change=VideoState.update_assigned_search,
                    on_key_down=VideoState.handle_assigned_key,
                    auto_focus=True,
                    width="100%",
                    size="3",
                    radius="large",
                ),
                width="100%",
                spacing="4",
            ),
            width="100%",
            max_width="900px",
            margin="0 auto",
            padding="28px 32px 20px",
        ),
        rx.box(
            rx.cond(
                VideoState.assigned_streams.length() > 0,
                rx.vstack(
                    rx.foreach(VideoState.assigned_streams, assigned_channel_row),
                    width="100%",
                    max_width="900px",
                    margin="0 auto",
                    spacing="3",
                    padding_bottom="40px",
                ),
                rx.center(
                    rx.vstack(
                        rx.icon("list", size=48, color="var(--gray-8)"),
                        rx.text(
                            rx.cond(
                                VideoState.assigned_search_text != "",
                                "No assigned channels match your search.",
                                "No channels assigned yet. Open the channel "
                                "menu (Alt+M) and use Assign to give a "
                                "channel a number.",
                            ),
                            color_scheme="gray",
                            text_align="center",
                        ),
                        align_items="center",
                        spacing="3",
                    ),
                    width="100%",
                    padding="60px",
                ),
            ),
            id="assigned-scroll-area",
            width="100%",
            flex="1 1 auto",
            overflow_y="auto",
            padding="4px 32px 40px",
        ),
        position="fixed",
        inset="0",
        z_index="30",
        display="flex",
        flex_direction="column",
        background="rgba(6, 9, 16, 0.80)",
        backdrop_filter="blur(22px) saturate(140%)",
    )


def index() -> rx.Component:
    return rx.fragment(
        # The native <video controls> element has its own built-in keyboard
        # shortcuts (Space toggles play/pause, arrows seek, etc.). Once a
        # click gives it focus, pressing Space would trigger that native
        # toggle *and* the app's own handle_playback_key below, flipping
        # playback twice in a row. Immediately blurring the video whenever it
        # gains focus keeps this app's state as the single source of truth
        # without disabling the native control bar itself (clicks on the
        # play/seek/volume controls still work — only keyboard focus is
        # removed).
        rx.script(
            """
            document.addEventListener("focusin", (event) => {
                if (event.target && event.target.tagName === "VIDEO") {
                    event.target.blur();
                }
            });
            """
        ),
        rx.window_event_listener(
            on_key_down=VideoState.handle_global_key,
        ),
        rx.window_event_listener(
            # A held key produces many keydown events. Debouncing this separate
            # listener turns that burst into one playback toggle without
            # delaying number-key entry handled by the listener above.
            on_key_down=VideoState.handle_playback_key.debounce(200),
        ),
        # player_view is now always mounted (instead of being swapped out by
        # a top-level rx.cond) so the <video> element never unmounts. The
        # channel menu and assigned-channels list are drawn as translucent
        # overlays on top of it, so the current channel keeps playing behind
        # the guide instead of stopping while you browse.
        player_view(),
        rx.cond(
            VideoState.current_view == "menu",
            menu_view(),
        ),
        rx.cond(
            VideoState.current_view == "assigned",
            assigned_view(),
        ),
        channel_number_dialog(),
    )


app = rx.App(
    theme=rx.theme(
        appearance="dark",
        accent_color="blue",
        gray_color="slate",
        radius="large",
    ),
)
app.add_page(
    index,
    route="/",
    title="OpenPixel TV",
    on_load=VideoState.load_streams,
)