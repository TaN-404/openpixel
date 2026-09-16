import httpx
import reflex as rx
import asyncio
import json


# IPTV-org endpoint containing the available streams.
STREAMS_API = "https://iptv-org.github.io/api/streams.json"

FOCUS_VIDEO_SCRIPT = """
const root = document.getElementById("tv-player");

if (root) {
    const video = root.matches("video")
        ? root
        : root.querySelector("video");

    if (video) {
        video.tabIndex = 0;
        video.focus({ preventScroll: true });
    }
}
"""


CHANNELS_API = "https://iptv-org.github.io/api/channels.json"

MAX_SEARCH_RESULTS = 50

MAX_CHANNELS  = 50


class VideoState(rx.State):
    """Application state for the IPTV player."""

    _channel_catalog: list[dict[str, str]] = []

    channel_number_buffer: str = ""
    channel_number_message: str = ""

    # Used to invalidate older three-second timers.
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


    is_playing: bool = False

    selected_index: int = 0

    search_text: str = ""

    # Controls which interface is visible.
    current_view: str = "player"

    # Currently selected channel.
    stream_url: str = ""
    stream_title: str = ""

    # Channels downloaded from IPTV-org.
    streams: list[dict[str, str]] = []

    # Loading and error information.
    is_loading: bool = False
    error_message: str = ""

    def update_search(self, value: str):
        self.search_text = value
        self._refresh_visible_channels()

    def open_menu(self):
        self.current_view = "menu"
        self.selected_index = 0

    def close_menu(self):
        self.show_number_dialog = False
        self.current_view = "player"

    def load_streams(self):
        """Load and combine IPTV stream and channel information."""

        self.restore_numbered_channels()

        if self._channel_catalog:
            self._refresh_visible_channels()
            return

        self.is_loading = True
        self.error_message = ""
        yield

        try:
            with httpx.Client(
                timeout=30,
                follow_redirects=True,
            ) as client:
                streams_response = client.get(STREAMS_API)
                channels_response = client.get(CHANNELS_API)

            streams_response.raise_for_status()
            channels_response.raise_for_status()

            api_streams = streams_response.json()
            api_channels = channels_response.json()

            # Convert the channel list into a lookup dictionary.
            channels_by_id = {
                channel["id"]: channel
                for channel in api_channels
                if channel.get("id")
            }

            # Reverse the saved assignments:
            # channel ID -> assigned number
            number_by_channel_id = {
                channel["channel_id"]: number
                for number, channel
                in self.numbered_channels.items()
                if channel.get("channel_id")
            }

            complete_catalog = []
            seen_channel_ids = set()

            # This copy will receive updated stream URLs.
            updated_assignments = dict(
                self.numbered_channels
            )

            for stream in api_streams:
                channel_id = stream.get("channel")
                url = stream.get("url")

                if not channel_id:
                    continue

                # Prevent the same channel appearing multiple times.
                if channel_id in seen_channel_ids:
                    continue

                channel_information = channels_by_id.get(
                    channel_id
                )

                # Skip streams without matching channel metadata.
                if not channel_information:
                    continue

                # Hide adult channels from the default catalogue.
                if channel_information.get("is_nsfw", False):
                    continue

                is_browser_friendly = (
                    isinstance(url, str)
                    and url.startswith("https://")
                    and ".m3u8" in url.lower()
                    and not stream.get("referrer")
                    and not stream.get("user_agent")
                )

                if not is_browser_friendly:
                    continue

                title = (
                    channel_information.get("name")
                    or stream.get("title")
                    or channel_id
                )

                country = (
                    channel_information.get("country")
                    or "Unknown"
                )

                category_list = (
                    channel_information.get("categories")
                    or []
                )

                categories = (
                    ", ".join(category_list)
                    if category_list
                    else "Uncategorized"
                )

                quality = (
                    stream.get("quality")
                    or "Unknown quality"
                )

                saved_number = number_by_channel_id.get(
                    channel_id,
                    "",
                )

                channel_record = {
                    "channel_id": channel_id,
                    "title": title,
                    "url": url,
                    "quality": quality,
                    "country": country,
                    "categories": categories,
                    "number": saved_number,
                }

                complete_catalog.append(channel_record)
                seen_channel_ids.add(channel_id)

                # Update a saved assignment with the newest URL.
                if saved_number:
                    updated_assignments[saved_number] = {
                        "channel_id": channel_id,
                        "title": title,
                        "url": url,
                    }

                if len(complete_catalog) == MAX_CHANNELS:
                    break

            self.streams = complete_catalog

            # Save any refreshed titles and stream URLs.
            self.numbered_channels = updated_assignments
            self.numbered_channels_json = json.dumps(
                updated_assignments
            )

            if not self._channel_catalog:
                self.error_message = (
                    "No compatible IPTV channels were found."
                )

        except httpx.HTTPError as error:
            self.error_message = (
                f"Could not download IPTV data: {error}"
            )

        except ValueError:
            self.error_message = (
                "The IPTV API returned invalid JSON."
            )

        finally:
            self.is_loading = False

    def play_stream(self, url: str, title: str):
        self.stream_url = url
        self.stream_title = title
        self.current_view = "player"
        self.is_playing = True

    # @rx.var
    # def complete_catalog(self) -> list[dict[str, str]]:
    #     """Search channel names, countries and categories."""

    #     search = self.search_text.strip().lower()

    #     if not search:
    #         return self.streams

    #     matching_streams = []

    #     for stream in self.streams:
    #         searchable_text = " ".join(
    #             [
    #                 stream["title"],
    #                 stream["country"],
    #                 stream["categories"],
    #                 stream["quality"],
    #             ]
    #         ).lower()

    #         if search in searchable_text:
    #             matching_streams.append(stream)

    #     return matching_streams
    
    def handle_menu_key(
        self,
        key: str,
        modifiers: dict[str, bool],
    ):
        channels = self.streams

        if not channels:
            return

        if key == "ArrowDown":
            self.selected_index = (
                self.selected_index + 1
            ) % len(channels)

        elif key == "ArrowUp":
            self.selected_index = (
                self.selected_index - 1
            ) % len(channels)

        elif key == "Enter":
            selected_channel = channels[self.selected_index]

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


    def handle_global_key(
        self,
        key: str,
        modifiers: dict[str, bool],
    ):
        """Handle application-wide keyboard input."""

        ctrl_pressed = modifiers.get("ctrl_key", False)
        alt_pressed = modifiers.get("alt_key", False)
        meta_pressed = modifiers.get("meta_key", False)

        # Ctrl+M toggles the menu.
        if ctrl_pressed and key.lower() == "m":
            self.channel_number_buffer = ""
            self.channel_number_message = ""
            self.number_entry_version += 1

            if self.current_view == "player":
                self.open_menu()
            else:
                self.close_menu()

            return

        # Escape closes the dialog, menu or number overlay.
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

        # Channel-number and playback shortcuts only work
        # in the player view.
        if self.current_view != "player":
            return

        # Space toggles playback.
        if key in (" ", "Space", "Spacebar"):
            self.toggle_playback()
            return

        # Ignore Ctrl+number, Alt+number and similar shortcuts.
        has_modifier = (
            ctrl_pressed
            or alt_pressed
            or meta_pressed
        )

        # Add numeric keys to the buffer.
        if key.isdigit() and not has_modifier:
            if len(self.channel_number_buffer) >= 3:
                return

            self.channel_number_buffer += key
            self.channel_number_message = ""

            self.number_entry_version += 1
            current_version = self.number_entry_version

            return VideoState.tune_after_delay(
                current_version
            )

        # Remove the most recently entered digit.
        if key == "Backspace":
            if not self.channel_number_buffer:
                return

            self.channel_number_buffer = (
                self.channel_number_buffer[:-1]
            )

            self.channel_number_message = ""
            self.number_entry_version += 1

            if self.channel_number_buffer:
                current_version = self.number_entry_version

                return VideoState.tune_after_delay(
                    current_version
                )

            return

        # Enter switches immediately.
        if key == "Enter":
            if not self.channel_number_buffer:
                return

            # Invalidates any timer that is still sleeping.
            self.number_entry_version += 1
            self._tune_to_channel_number()

    def toggle_playback(self):
        """Pause or resume the current channel."""

        if self.current_view != "player":
            return

        if not self.stream_url:
            return

        self.is_playing = not self.is_playing


    def player_started(self):
        """Synchronize state when native controls start playback."""

        self.is_playing = True


    def player_paused(self):
        """Synchronize state when native controls pause playback."""

        self.is_playing = False



    def open_number_dialog(
        self,
        channel_id: str,
        title: str,
        url: str,
    ):
        """Open the number dialog for a channel."""

        self.pending_channel = {
            "channel_id": channel_id,
            "title": title,
            "url": url,
        }

        self.channel_number_input = ""
        self.assignment_error = ""

        # Show its existing number when changing an assignment.
        for number, channel in self.numbered_channels.items():
            if channel["channel_id"] == channel_id:
                self.channel_number_input = number
                break

        self.show_number_dialog = True


    def close_number_dialog(self):
        self.show_number_dialog = False
        self.channel_number_input = ""
        self.assignment_error = ""
        self.pending_channel = {}


    def update_channel_number(self, value: str):
        """Accept only three numeric characters."""

        digits_only = "".join(
            character
            for character in value
            if character.isdigit()
        )

        self.channel_number_input = digits_only[:3]
        self.assignment_error = ""


    def save_channel_number(self):
        """Save the selected channel-number assignment."""

        entered_number = self.channel_number_input.strip()

        if not entered_number:
            self.assignment_error = "Enter a channel number."
            return

        channel_number = str(int(entered_number))

        if channel_number == "0":
            self.assignment_error = (
                "Channel numbers must be between 1 and 999."
            )
            return

        existing_channel = self.numbered_channels.get(
            channel_number
        )

        if (
            existing_channel
            and existing_channel["channel_id"]
            != self.pending_channel["channel_id"]
        ):
            self.assignment_error = (
                f"Channel {channel_number} is already assigned to "
                f"{existing_channel['title']}."
            )
            return

        updated_assignments = dict(self.numbered_channels)

        # Remove the channel's previous number, if it had one.
        for old_number, channel in list(
            updated_assignments.items()
        ):
            if (
                channel["channel_id"]
                == self.pending_channel["channel_id"]
            ):
                del updated_assignments[old_number]

        updated_assignments[channel_number] = dict(
            self.pending_channel
        )

        self.numbered_channels = updated_assignments
        self.numbered_channels_json = json.dumps(
            updated_assignments
        )

        # Update the number shown in the menu.
        updated_catalog = []

        for stream in self._channel_catalog:
            updated_stream = dict(stream)

            if (
                updated_stream["channel_id"]
                == self.pending_channel["channel_id"]
            ):
                updated_stream["number"] = channel_number

            updated_catalog.append(updated_stream)

        self._channel_catalog = updated_catalog
        self.close_number_dialog()


    def handle_assignment_key(self, key: str):
        if key == "Enter":
            self.save_channel_number()


    def _tune_to_channel_number(self):
        """Switch to the channel in the number buffer."""

        if not self.channel_number_buffer:
            return

        # Convert 001 into 1, for example.
        channel_number = str(
            int(self.channel_number_buffer)
        )

        assigned_channel = self.numbered_channels.get(
            channel_number
        )

        if not assigned_channel:
            self.channel_number_message = (
                f"Channel {channel_number} is not assigned."
            )
            self.channel_number_buffer = ""
            return

        self.stream_url = assigned_channel["url"]
        self.stream_title = assigned_channel["title"]
        self.is_playing = True

        self.channel_number_buffer = ""
        self.channel_number_message = ""

    def restore_numbered_channels(self):
        """Restore assignments from browser local storage."""

        try:
            stored_data = json.loads(
                self.numbered_channels_json or "{}"
            )

            if isinstance(stored_data, dict):
                self.numbered_channels = stored_data
            else:
                self.numbered_channels = {}

        except (json.JSONDecodeError, TypeError):
            self.numbered_channels = {}
            self.numbered_channels_json = "{}"

    
    def _refresh_visible_channels(self):
        """Search the full catalogue and expose only 50 results."""

        search = self.search_text.strip().lower()

        if not search:
            matches = self._channel_catalog
        else:
            matches = []

            for channel in self._channel_catalog:
                searchable_text = " ".join(
                    [
                        channel["title"],
                        channel["country"],
                        channel["categories"],
                        channel["quality"],
                    ]
                ).lower()

                if search in searchable_text:
                    matches.append(channel)

        self.streams = matches[:MAX_SEARCH_RESULTS]
        self.selected_index = 0


    @rx.event(background=True)
    async def tune_after_delay(self, timer_version: int):
        """Tune after three seconds if no newer key was pressed."""

        await asyncio.sleep(3)

        async with self:
            if timer_version != self.number_entry_version:
                return

            if not self.channel_number_buffer:
                return

            self._tune_to_channel_number()


def channel_number_overlay() -> rx.Component:
    return rx.cond(
        VideoState.channel_number_buffer != "",

        rx.box(
            rx.text(
                VideoState.channel_number_buffer,
                size="8",
                weight="bold",
            ),
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
                rx.text(
                    VideoState.channel_number_message,
                    weight="bold",
                ),
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

                rx.button(
                    "Save Number",
                    on_click=VideoState.save_channel_number,
                ),

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
            rx.cond(
                is_selected,
                rx.icon("chevron-right"),
                rx.box(width="24px"),
            ),

            rx.vstack(
                rx.text(
                    stream["title"],
                    weight="bold",
                ),
                rx.hstack(
                    rx.hstack(
                        rx.text(
                            stream["country"],
                            size="1",
                            color_scheme="gray",
                        ),

                        rx.text(
                            "•",
                            size="1",
                            color_scheme="gray",
                        ),

                        rx.text(
                            stream["categories"],
                            size="1",
                            color_scheme="gray",
                        ),

                        rx.text(
                            "•",
                            size="1",
                            color_scheme="gray",
                        ),

                        rx.text(
                            stream["quality"],
                            size="1",
                            color_scheme="gray",
                        ),

                        spacing="2",
                        wrap="wrap",
                    ),

                    rx.cond(
                        stream["number"] != "",
                        rx.badge(
                            rx.text(
                                "Channel ",
                                stream["number"],
                            ),
                            color_scheme="blue",
                        ),
                        rx.badge(
                            "No number",
                            color_scheme="gray",
                        ),
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
                    stream["channel_id"],
                    stream["title"],
                    stream["url"],
                ),
                variant="soft",
            ),

            rx.button(
                rx.icon("play"),
                "Play",
                on_click=VideoState.play_stream(
                    stream["url"],
                    stream["title"],
                ),
            ),

            width="100%",
            align_items="center",
        ),

        background_color=rx.cond(
            is_selected,
            "var(--accent-5)",
            "var(--gray-2)",
        ),

        border=rx.cond(
            is_selected,
            "2px solid var(--accent-9)",
            "2px solid transparent",
        ),

        width="100%",
        padding="10px",
    )



def player_view() -> rx.Component:
    """Default television/player view."""

    return rx.center(
        rx.vstack(
            channel_number_overlay(),
            rx.hstack(
                rx.heading(
                    "📺 OpenPixel TV",
                    size="7",
                ),
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
                        controls=True,

                        playing=VideoState.is_playing,

                        on_play=VideoState.player_started,
                        on_pause=VideoState.player_paused,

                        id="tv-player",
                        width="100%",
                        height="auto",
                        aspect_ratio="16 / 9",
                    ),
                    rx.text(
                        VideoState.stream_title,
                        weight="bold",
                        size="4",
                    ),
                    width="100%",
                    align_items="center",
                    spacing="3",
                ),
                rx.center(
                    rx.vstack(
                        rx.icon(
                            "tv",
                            size=60,
                            color="var(--gray-8)",
                        ),
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
            max_width="900px",
            spacing="4",
        ),
        width="100vw",
        min_height="100vh",
        padding="20px",
        background_color="var(--gray-2)",
    )


def menu_view() -> rx.Component:
    """Channel selection menu."""

    return rx.container(
        rx.vstack(
            # Header
            rx.hstack(
                rx.heading(
                    "Channels",
                    size="7",
                ),

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

            # Search bar
            rx.input(
                placeholder="Search channels...",
                value=VideoState.search_text,
                on_change=VideoState.update_search,
                on_key_down=VideoState.handle_menu_key,
                auto_focus=True,
                width="100%",
                size="3",
            ),

            # Loading state or channel results
            rx.cond(
                VideoState.is_loading,

                rx.center(
                    rx.vstack(
                        rx.spinner(size="3"),
                        rx.text("Loading IPTV channels..."),
                        align_items="center",
                        spacing="3",
                    ),
                    width="100%",
                    padding="40px",
                ),

                rx.cond(
                    VideoState.streams.length() > 0,

                    # The channel list is rendered only here.
                    rx.vstack(
                        rx.foreach(
                            VideoState.streams,
                            channel_button,
                        ),
                        width="100%",
                        spacing="2",
                    ),

                    # Empty search result
                    rx.center(
                        rx.text(
                            "No matching channels found.",
                            color_scheme="gray",
                        ),
                        width="100%",
                        padding="40px",
                    ),
                ),
            ),

            # API error
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

        max_width="900px",
    )

def index() -> rx.Component:
    return rx.fragment(
        rx.window_event_listener(
            on_key_down=VideoState.handle_global_key,
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