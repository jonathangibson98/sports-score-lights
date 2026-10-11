import appdaemon.plugins.hass.hassapi as hass
import json
import os


SPORTS_MODE_ENTITY = "select.sync_lights_mode"
SPORTS_MODE_STATE = "Sports"

ALTERNATE_INTERVAL = 30      # seconds between team swaps while idle
FLASH_INTERVAL = 0.8         # seconds between group color-swaps during a score flash
FLASH_COUNT = 3              # how many times group1/group2 trade colors per score flash

# One shared JSON file holds last-seen scores for EVERY team tracker app,
# keyed by app name. No HA entities/helpers needed no matter how many
# team_lights_* apps you run.
SCORE_STATE_FILE = "/homeassistant/appdaemon/team_lights_score_state.json"


class TeamScoreLights(hass.Hass):

    def initialize(self):
        # ---------------------------------------------------------
        # CONFIG
        # ---------------------------------------------------------
        self.sensor = self.args["sensor"]
        # Two light groups, defined in apps.yaml as lights_group1 / lights_group2.
        # Group 1 always shows the "primary" side of whatever's being displayed;
        # Group 2 always shows the "secondary" side.
        self.lights_group1 = self.args.get("lights_group1", [])
        self.lights_group2 = self.args.get("lights_group2", [])

        condition = self.args.get("condition", {})
        self.cond_entity = condition.get("entity")
        self.cond_state = condition.get("state")

        # ---------------------------------------------------------
        # INTERNAL STATE
        # ---------------------------------------------------------
        self.last_team_score = None
        self.last_opponent_score = None

        self.team_name = None
        self.opponent_name = None
        self.team_colors = []
        self.opponent_colors = []

        # which team is currently showing in the alternate cycle: 0 = team, 1 = opponent
        self._alternate_team_index = 0

        # handle for the recurring 30s alternate timer, so we can cancel/restart it
        self._alternate_timer_handle = None

        # handles for any in-progress flash sequence, so a new score can cancel an old flash
        self._flash_timer_handles = []

        self.started = False

        # pull last-seen scores from the shared JSON state file
        # BEFORE we do anything else, so a restart doesn't blind us to a
        # score that happened right before/during the restart.
        self._load_persisted_scores()

        # ---------------------------------------------------------
        # LISTENERS
        # ---------------------------------------------------------
        self.listen_state(
            self._on_sensor_change,
            self.sensor,
            attribute="all"
        )

        if self.cond_entity:
            self.listen_state(
                self._on_condition_change,
                self.cond_entity
            )

        self.listen_state(
            self._on_sports_mode_change,
            SPORTS_MODE_ENTITY
        )

        self.run_in(self._startup_check, 2)

    # =========================================================
    # PERSISTENCE
    # =========================================================

    def _read_state_file(self):
        """Read the whole shared JSON file. Returns {} if it doesn't exist
        yet or is corrupt/empty — never crashes the app over this."""
        if not os.path.exists(SCORE_STATE_FILE):
            return {}
        try:
            with open(SCORE_STATE_FILE, "r") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"{self.name}: Could not read score state file: {e}", level="WARNING")
            return {}

    def _write_state_file(self, all_state):
        try:
            with open(SCORE_STATE_FILE, "w") as f:
                json.dump(all_state, f)
        except OSError as e:
            self.log(f"{self.name}: Could not write score state file: {e}", level="ERROR")

    def _load_persisted_scores(self):
        """Read this app's last-seen scores out of the shared JSON file so
        a restart doesn't reset our baseline to whatever the score happens
        to be right now."""
        all_state = self._read_state_file()
        my_state = all_state.get(self.name, {})
        self.last_team_score = my_state.get("last_team_score")
        self.last_opponent_score = my_state.get("last_opponent_score")

    def _persist_scores(self):
        """Write this app's current last-seen scores back into the shared
        JSON file (under this app's own key, so 20 trackers can share one
        file without stomping each other) so the next restart picks up
        where we left off."""
        all_state = self._read_state_file()
        all_state[self.name] = {
            "last_team_score": self.last_team_score,
            "last_opponent_score": self.last_opponent_score,
        }
        self._write_state_file(all_state)

    # =========================================================
    # CONDITION CHECKS
    # =========================================================

    def _condition_met(self):
        if not self.cond_entity:
            return True
        current_state = self.get_state(self.cond_entity)
        return str(current_state) == str(self.cond_state)

    def _sports_mode_met(self):
        current_state = self.get_state(SPORTS_MODE_ENTITY)
        return str(current_state) == SPORTS_MODE_STATE

    def _all_conditions_met(self):
        return self._condition_met() and self._sports_mode_met()

    # =========================================================
    # STARTUP
    # =========================================================

    def _startup_check(self, kwargs):
        self.started = True

        if self._all_conditions_met():
            self._refresh_team_data(seed_scores=(self.last_team_score is None))
            self._start_alternating()
        else:
            self._stop_alternating()

    # =========================================================
    # STATE LISTENERS
    # =========================================================

    def _on_sensor_change(self, entity, attribute, old, new, kwargs):
        if not self.started:
            return

        if not self._all_conditions_met():
            self._stop_alternating()
            return

        self._handle_sensor_update()

    def _on_condition_change(self, entity, attribute, old, new, kwargs):
        if not self.started:
            return

        if self._all_conditions_met():
            # only seed scores here if we don't already have a persisted
            # baseline, otherwise flipping Sports mode off/on mid-game
            # would re-baseline and swallow a score the same way a
            # restart used to.
            self._refresh_team_data(seed_scores=(self.last_team_score is None))
            self._start_alternating()
        else:
            self._stop_alternating()

    def _on_sports_mode_change(self, entity, attribute, old, new, kwargs):
        if not self.started:
            return

        if self._all_conditions_met():
            self._refresh_team_data(seed_scores=(self.last_team_score is None))
            self._start_alternating()
        else:
            self._stop_alternating()

    # =========================================================
    # SENSOR DATA
    # =========================================================

    def _get_sensor_data(self):
        data = self.get_state(self.sensor, attribute="all")

        if not data:
            self.log(f"{self.sensor}: No sensor data available.", level="WARNING")
            return None

        return data.get("attributes", {})

    def _refresh_team_data(self, seed_scores=False):
        """Pull the latest team/opponent names + colors from the sensor.
        Only overwrites last_team_score/last_opponent_score if seed_scores
        is True AND we don't already have a persisted value — otherwise we
        keep whatever baseline we loaded from the helpers so we don't miss
        a score that happened around a restart/condition flip."""
        attributes = self._get_sensor_data()

        if attributes is None:
            return False

        self.team_name = attributes.get("team_name", self.name)
        self.opponent_name = attributes.get("opponent_name", "Opponent")
        self.team_colors = self._usable_colors(attributes.get("team_colors", []) or [])
        self.opponent_colors = self._usable_colors(attributes.get("opponent_colors", []) or [])

        if seed_scores:
            team_score = self._get_score(attributes, "team_score")
            opponent_score = self._get_score(attributes, "opponent_score")
            self.last_team_score = team_score
            self.last_opponent_score = opponent_score
            self._persist_scores()

        return True

    def _handle_sensor_update(self):
        """Called whenever the sensor changes. Figures out if it was a score change
        and triggers a flash if so, otherwise just refreshes cached team data."""

        attributes = self._get_sensor_data()
        if attributes is None:
            return

        team_score = self._get_score(attributes, "team_score")
        opponent_score = self._get_score(attributes, "opponent_score")

        if team_score is None or opponent_score is None:
            return

        # Cache latest names/colors regardless (team names rarely change mid-game, but just in case)
        self.team_name = attributes.get("team_name", self.name)
        self.opponent_name = attributes.get("opponent_name", "Opponent")
        self.team_colors = self._usable_colors(attributes.get("team_colors", []) or [])
        self.opponent_colors = self._usable_colors(attributes.get("opponent_colors", []) or [])

        # Only flash on scoring plays worth celebrating: field goal (+3)
        # or any touchdown variant (+6 TD, +7 TD/PAT, +8 TD/2pt — all
        # covered by "6 or more" since you can't reach 7/8 without the
        # touchdown's 6 first). A lone extra point (+1) arrives as its
        # own separate update after the TD update and is skipped here.
        def _is_flashable(delta):
            return delta == 3 or delta >= 6

        team_delta = None
        if self.last_team_score is not None:
            team_delta = team_score - self.last_team_score

        opponent_delta = None
        if self.last_opponent_score is not None:
            opponent_delta = opponent_score - self.last_opponent_score

        team_scored = team_delta is not None and _is_flashable(team_delta)
        opponent_scored = opponent_delta is not None and _is_flashable(opponent_delta)

        self.last_team_score = team_score
        self.last_opponent_score = opponent_score
        self._persist_scores()

        if not self._all_conditions_met():
            return

        # Make sure the alternate loop is running (covers case where app
        # started mid-game or timer somehow died)
        if self._alternate_timer_handle is None:
            self._start_alternating()

        if team_scored:
            self.log(f"{self.name}: {self.team_name} scored (+{team_delta})!")
            self._flash(self.team_colors)
        elif opponent_scored:
            self.log(f"{self.name}: {self.opponent_name} scored (+{opponent_delta})!")
            self._flash(self.opponent_colors)

    def _get_score(self, attributes, key):
        value = attributes.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    # Substitutes for near-white / near-black team colors — real bulb-friendly
    # colors that read as "white-ish" or "black-ish" without going full
    # blinding white or fully dark. Used to REPLACE, never delete, so a team
    # whose actual colors include black/white (Carolina, Alabama, etc.)
    # still gets two distinct colors instead of collapsing to one.
    WHITE_SUBSTITUTE = "#cfe8ff"   # soft ice blue — reads as "white" on an RGB bulb
    BLACK_SUBSTITUTE = "#3d2b00"   # deep amber/gold — reads as "black" without going dark

    def _is_near_white(self, rgb):
        WHITE_THRESHOLD = 235   # all channels above this = "basically white"
        return all(c >= WHITE_THRESHOLD for c in rgb)

    def _is_near_black(self, rgb):
        BLACK_THRESHOLD = 20    # all channels below this = "basically black"
        return all(c <= BLACK_THRESHOLD for c in rgb)

    def _usable_colors(self, colors):
        """Swap out any near-white or near-black color for a bulb-friendly
        substitute, keeping the same number of colors and the same order —
        so a team like South Carolina (garnet + black) or Alabama (crimson +
        white) still ends up with two visually distinct colors instead of
        one getting silently dropped."""
        if not colors:
            return []

        result = []
        for color in colors:
            rgb = self._color_to_rgb(color)
            if self._is_near_white(rgb):
                result.append(self.WHITE_SUBSTITUTE)
            elif self._is_near_black(rgb):
                result.append(self.BLACK_SUBSTITUTE)
            else:
                result.append(color)
        return result

    # =========================================================
    # ALTERNATING LOOP (steady state, every 30s)
    # Group 1 = current team's primary color, Group 2 = that same team's
    # secondary color, held simultaneously. Every 30s both groups swap to
    # the other team (still primary/secondary within that team).
    # =========================================================

    def _start_alternating(self):
        """(Re)start the recurring 30s alternate-team timer."""
        self._stop_alternating()

        if not self._all_conditions_met():
            return

        self._alternate_team_index = 0
        # Show immediately, then flip every ALTERNATE_INTERVAL seconds
        self._apply_alternate_step({})
        self._alternate_timer_handle = self.run_every(
            self._apply_alternate_step,
            "now+{}".format(ALTERNATE_INTERVAL),
            ALTERNATE_INTERVAL
        )

    def _stop_alternating(self):
        if self._alternate_timer_handle is not None:
            try:
                self.cancel_timer(self._alternate_timer_handle)
            except Exception:
                pass
            self._alternate_timer_handle = None

    def _apply_alternate_step(self, kwargs):
        if not self._all_conditions_met():
            self._stop_alternating()
            return

        colors = self.team_colors if self._alternate_team_index == 0 else self.opponent_colors

        primary = colors[0] if len(colors) > 0 else None
        secondary = colors[1] if len(colors) > 1 else primary

        if primary is not None:
            self._set_group_color(self.lights_group1, primary)
        if secondary is not None:
            self._set_group_color(self.lights_group2, secondary)

        self._alternate_team_index = 1 - self._alternate_team_index

    # =========================================================
    # SCORE FLASH (interrupts the alternate loop briefly)
    # Group 1 starts on the scoring team's primary, Group 2 on its
    # secondary. Every FLASH_INTERVAL seconds the two groups trade colors.
    # FLASH_COUNT trades total, then resume normal alternating.
    # =========================================================

    def _flash(self, colors):
        """Group1/Group2 trade the scoring team's primary/secondary colors
        back and forth FLASH_COUNT times, then resume the alternate loop."""

        self._cancel_flash()

        if not colors:
            self.log(
                f"{self.name}: _flash called with no colors — skipping flash "
                f"(check team_colors/opponent_colors on {self.sensor})",
                level="WARNING"
            )
            return

        primary = colors[0]
        secondary = colors[1] if len(colors) > 1 else colors[0]

        # Pause the steady alternate loop while we flash
        self._stop_alternating()

        total_steps = FLASH_COUNT * 2  # each trade = group1<->group2 swap
        step_index = [0]

        def run_step(kwargs):
            if not self._all_conditions_met():
                self._flash_timer_handles.clear()
                return

            i = step_index[0]

            if i >= total_steps:
                # Flash done -> resume normal alternating
                self._start_alternating()
                return

            # Even step: group1=primary, group2=secondary.
            # Odd step: group1=secondary, group2=primary (the "trade").
            if i % 2 == 0:
                self._set_group_color(self.lights_group1, primary)
                self._set_group_color(self.lights_group2, secondary)
            else:
                self._set_group_color(self.lights_group1, secondary)
                self._set_group_color(self.lights_group2, primary)

            step_index[0] += 1

            handle = self.run_in(run_step, FLASH_INTERVAL)
            self._flash_timer_handles.append(handle)

        handle = self.run_in(run_step, 0)
        self._flash_timer_handles.append(handle)

    def _cancel_flash(self):
        for handle in self._flash_timer_handles:
            try:
                self.cancel_timer(handle)
            except Exception:
                pass
        self._flash_timer_handles = []

    # =========================================================
    # LIGHT CONTROL
    # =========================================================

    def _set_group_color(self, lights, color):
        """Always sets an actual RGB color on a specific group of lights —
        never turns them off, so there's no white/blackout flash between
        swaps."""

        if not self._all_conditions_met():
            return

        for light in lights:
            try:
                rgb = self._color_to_rgb(color)
                self.call_service(
                    "light/turn_on",
                    entity_id=light,
                    rgb_color=rgb,
                    transition=0  # force instant change, no per-bulb fade drift
                )
                self.log(f"{self.name}: set {light} -> {color} (rgb={rgb})", level="DEBUG")
            except Exception as e:
                self.log(f"{self.name}: Error controlling {light}: {e}", level="ERROR")

    def _color_to_rgb(self, color):
        if isinstance(color, (list, tuple)):
            if len(color) >= 3:
                return [int(color[0]), int(color[1]), int(color[2])]

        if isinstance(color, str):
            color = color.strip().lstrip("#")
            if len(color) == 6:
                try:
                    return [
                        int(color[0:2], 16),
                        int(color[2:4], 16),
                        int(color[4:6], 16)
                    ]
                except ValueError:
                    pass

        return [255, 255, 255]
