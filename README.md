![Sports Score Lights](sports-score.jpg)

<table>
  <tr>
    <td align="center" width="140">
      <img src="logo.png" alt="Logo" width="120"/>
    </td>
    <td>
      <h1>Sports Score Lights for AppDaemon</h1>
      <p>Dynamic Team Colors & Score Flashing for AppDaemon</p>
    </td>
  </tr>
</table>

An AppDaemon app for Home Assistant that dynamically controls your smart lights based on live sports scores. It alternates your lights between your team's and opponent's colors during normal play, and triggers an exciting color-flashing sequence whenever a major scoring play (like a touchdown or field goal) happens!

## Features

* **Live Alternating Colors:** Cycles your lights between your favorite team and the opponent's colors smoothly during the game.
* **Score-Triggered Flashing:** Automatically flashes the lights back and forth when a big scoring play happens (field goals or touchdowns).
* **Smart Color Substitutions:** Automatically swaps out near-white or near-black team colors for bulb-friendly alternatives so teams with black or white uniforms still look vibrant.
* **State Persistence:** Saves the last-seen scores to a shared JSON file so Home Assistant or AppDaemon restarts do not cause the app to miss a score.
* **Condition & Sports Mode Checks:** Easily tie the app to a "Sports Mode" toggle or custom condition entity so it only runs when you want it to.

---

## Installation via HACS

1. Open **HACS** in your Home Assistant dashboard.
2. Click on **Automations** (or **Apps** depending on your HACS version).
3. Click the three dots in the top right corner and select **Custom repositories**.
4. Paste the URL of your GitHub repository.
5. Select **AppDaemon** as the category and click **Add**.
6. Find **Sports Score Lights** in your HACS list and click **Download**.

---

## Configuration (`apps.yaml`)

Add an entry to your AppDaemon `apps.yaml` file to configure your team sensor and light groups. Example:

```yaml
my_team_lights:
  module: sports_score_lights
  class: SportsScoreLights
  sensor: sensor.my_favorite_team_game_tracker
  lights_group1:
    - light.living_room_lamp_1
    - light.living_room_lamp_2
  lights_group2:
    - light.tv_backlight
  condition:
    entity: input_boolean.game_room_active
    state: "on"
