# Training Analyze

A personal training analysis tool for Garmin users. It downloads your activities from Garmin Connect and lets you dig into laps and intervals: split each interval into phases, make histograms, compare reps, check HR/power zones and see a map of the track. It also shows your Garmin calendar with training load (TSS, CTL, ATL, TSB), and uploads structured workouts, one at a time or a whole week, to Garmin Connect.

It comes with three front ends. They share the same screens and keys:

| Front end | Start with | What it is |
| --- | --- | --- |
| Terminal (TUI) | `run.sh` / `run.bat` | Keyboard-driven app in the terminal (the main one) |
| Desktop (GUI) | `run_gui.sh` / `run_gui.bat` | Native window (Qt) with charts and a slippy map |
| Web | `run_web.sh` / `run_web.bat` | Opens in your browser, only reachable from your own computer |

> This tool uses the **unofficial** Garmin Connect API (the [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library). It is not affiliated with Garmin. If Garmin changes its API, things can break until the library is updated.

## Requirements

- **Python 3.12 or newer**
- A **Garmin Connect** account
- An internet connection. You need it to sync, to get map tiles from OpenStreetMap and to upload workouts. Activities you have already synced work offline.

## Installation

Clone or download this repository, then follow the steps for your system. The installer creates a private Python environment in `.venv/` inside the project folder and installs everything into it. Nothing is installed system-wide.

### macOS

1. Install Python 3.12+. Use [Homebrew](https://brew.sh) (`brew install python@3.12`) or the installer from [python.org](https://www.python.org/downloads/macos/).
2. Open **Terminal** in the project folder and run:

   ```bash
   ./install.sh
   ```

   If you get "permission denied" (this can happen when you downloaded a zip), run `bash install.sh` instead, and use `bash run.sh` and so on below.
3. Start the app:

   ```bash
   ./run.sh        # terminal app
   ./run_gui.sh    # desktop app
   ./run_web.sh    # web app in your browser
   ```

### Linux

1. Install Python 3.12+ with the venv module. On Ubuntu/Debian:

   ```bash
   sudo apt install python3 python3-venv
   ```

   On older distributions that ship Python below 3.12, install a newer one, e.g. with `pyenv` or the deadsnakes PPA.
2. Optional extras:
   - **Desktop app on Ubuntu/Debian:** Qt needs `sudo apt install libxcb-cursor0`.
   - **Copy key (`y`) in the terminal app:** install a clipboard tool. Use `wl-clipboard` on Wayland, or `xclip`/`xsel` on X11.
3. In the project folder:

   ```bash
   ./install.sh
   ./run.sh        # terminal app
   ./run_gui.sh    # desktop app
   ./run_web.sh    # web app in your browser
   ```

### Windows

1. Install Python 3.12+ from [python.org](https://www.python.org/downloads/windows/). Keep the **"py launcher"** option ticked.
2. Double-click `install.bat`, or run it from a terminal in the project folder:

   ```bat
   install.bat
   ```

3. Start the app by double-clicking one of these, or run it from a terminal:
   - `run.bat`: terminal app. Use **Windows Terminal**; the old console window draws the charts badly.
   - `run_gui.bat`: desktop app
   - `run_web.bat`: web app in your browser

### Updating

Pull or download the new version, then run `install.sh` / `install.bat` again to update the dependencies.

## First start: logging in

On the first start you will see a login screen. Enter your Garmin Connect email and password. If your account uses two-factor authentication, the app then asks for the code Garmin sends you.

Your password is **not** stored. After login the app saves Garmin session tokens in `.garmin_session/`, and you stay logged in until they expire. To log out, delete that folder. **Treat it like a password: don't share it or commit it.**

Then press **`s` (Sync)** to download your activities. The first sync downloads every activity file, so it can take a while.

## Using it

Every screen lists its keys in the footer at the bottom. In the desktop and web apps you can also click them. `Esc` goes back and `q` quits the terminal app.

### Activities (main screen)

| Key | Action |
| --- | --- |
| `s` | Sync new activities from Garmin Connect |
| `r` | Reprocess: re-read the downloaded files offline after updating this app (keeps your interval marks) |
| `o` | Routes: activities grouped by the route you ran or rode |
| `w` | Workouts: upload planned workouts to Garmin Connect |
| `c` | Calendar & training load for the week |
| `p` | Settings (see below) |
| `H` | Sync your whole Garmin history, not just the last year (asks first; run it again to carry on if Garmin stops it part-way) |
| `y` | Copy the table to the clipboard (tab-separated, pastes into a spreadsheet) |
| `Enter` | Open the selected activity |

Use the period drop-down to show only the last 7 or 14 days or 4 weeks (the app remembers your choice), and the sport drop-down to show only running, cycling, swimming and so on. Sync also imports the sets of strength sessions (exercise, reps, weight).

### Settings (`p`)

Stored in `data/settings.json`:
- **Activity list shows:** all activities, or only the last 7 / 14 days / 4 weeks (same as the main screen's period drop-down).
- **Hidden sports:** e.g. strength. Hidden sports aren't listed among activities or as calendar rows, but their training load still counts.
- **Archive past workouts:** moves week plans and workout files whose dates have all passed into `workouts/archive/`.
- **Show archived workouts:** lists the archive on the Workouts screen again.
- **Find physical laps automatically:** turn off to skip loop detection when an activity opens (`p` there still finds them).

### Activity laps

The laps list has three sections:

- **INTERVALS:** every lap you marked, from any section, in time order, with its phase splits. Laps from a split are prefixed `S` and physical laps `P`.
- **LAPS:** your watch's own laps, or a whole-activity split once you type one (e.g. `1km`) and press Split. Reset brings back the watch laps.
- **PHYSICAL:** laps of a repeated loop (track or park laps), found from GPS automatically when the activity opens. The loops are numbered; the way to the loop and the way back show as `in` and `out`.

Marks on the watch's laps are saved. Marks on a split or on physical laps last until you leave the activity.

| Key | Action |
| --- | --- |
| `m` | Mark or unmark the selected lap as an interval |
| `p` | Detect physical laps again, using the loop radius typed in the split box (e.g. `100`) |
| `i` | Split intervals into phases (shown as 2.1, 2.2, …) |
| `f` | Show interval laps only |
| `h` | Phase time/pace/power histograms |
| `c` | Compare intervals |
| `z` | HR and power zones |
| `g` | Full-size map (desktop and web apps) |
| `t` | Strength sets: exercise, reps, weight, volume and rest of each set (strength sessions) |
| `y` | Copy the table |

The split length is typed as a plain number for **metres** (`300`) or with a time unit for **time** (`60s`, `1m`, `1:30`). The **Total** column shows the running time after each lap. It restarts in each section. "Interval only" (`f`) hides the other two sections.

A multisport activity (e.g. a triathlon) shows one section per leg. You can split pool swims the same way as runs and rides. If an activity varies in intensity (a structured workout), the app joins splits that your watch's Auto Lap cut in the middle of a planned interval.

### Calendar & training load

The calendar shows one week of your Garmin Connect calendar: done, planned and missed sessions, with TSS for each.

- **TSS:** estimated from your thresholds in Garmin (FTP, threshold pace, lactate threshold HR), rounded to 5.
- **CTL (fitness):** 42-day average of daily TSS.
- **ATL (fatigue):** 7-day average of daily TSS.
- **TSB (form):** yesterday's CTL minus ATL.
- **Ramp rate:** CTL gained over the week; above about 5–8 is aggressive.
- **ACWR:** ATL ÷ CTL; 0.8–1.3 is the sweet spot, above 1.5 injury risk rises.
- **Monotony:** average daily TSS ÷ its spread; above 2 means too little variation between hard and easy days.
- **Strain:** weekly TSS × monotony.

Each run or ride also shows its **efficiency factor** (power or speed per heartbeat) and **aerobic decoupling** (how much that drops from the first half to the second; under 5% on a long steady session means good aerobic endurance).

| Key | Action |
| --- | --- |
| `[` / `]` | Previous / next week |
| `t` | This week |
| `r` | Refresh from Garmin |
| `m` | Move the selected planned workout to another day of its week, in Garmin Connect (your watch gets it on its next sync) |
| `x` | Cancel the selected planned workout: removes it from the Garmin Connect calendar but keeps it in your workout library (asks first) |
| `l` | Link a planned workout with the activity that carried it out, e.g. Saturday's run done on Sunday, or an activity recorded without the workout on the watch. On a linked row, `l` unlinks them |
| `e` | Export this week so far (Monday to today), or the last 1, 4, 12 or 24 full weeks (Monday to Sunday, ending last Sunday), to one JSON file in `exports/` for another AI agent to analyse |

A planned workout counts as done when an activity of the same sport is on the same day. Links override this. They are stored only in this app (`data/plan_links.json`), because Garmin has no such link.

**Export (`e`):** the file is meant for an AI coach that reviews your training and gives feedback. If you're logged in, the app first downloads whatever the period is missing from Garmin (older calendar months, planned workouts, strength sets, your weight/resting HR/VO2max/threshold history). It reads top-down:
- **Guide:** a few lines telling the model what the file is and how to read it.
- **Athlete:** gender, age, height, HR and power zones, current thresholds and fitness.
- **Trend:** one row per week (TSS planned/done, hours, km per sport, intensity split, CTL/ATL/TSB, ACWR, monotony, resting HR, weight).
- **Each week's summary:** sessions done/missed/extra, volume per sport, TSS against plan, time in HR zones and the easy/moderate/hard split, load metrics, that week's weight/resting HR/VO2max/thresholds and W/kg, key sessions, and **flags**. Flags are rule-based hints, such as a high ACWR, a missed session, reps off target, an easy session done too hard, resting HR above its 4-week baseline, or high decoupling.
- **Each session:**
  - **execution:** the plan against what was done, step by step when the workout was followed on the watch. Each step shows its target, the actual pace, grade-adjusted pace, HR, power and NP, a verdict (on target / faster / slower, by how much) and HR recovery in rests. Strength sessions get planned against done sets × reps @ kg per exercise.
  - **planned:** the workout as written.
  - **activity:** what was recorded, with intensity factor, efficiency factor, decoupling, exact time in HR and power zones, and cadence against 166 spm.
  - **Laps and splits:** the activity's laps (its structure, with your interval marks), each split again every 300 m (runs) or 1 km (rides). Laps and splits include min/avg/max HR, power, NP, cadence and, on runs, grade-adjusted pace, ground contact time, vertical oscillation and ratio, and step length.

Values are in seconds, metres, m/s, W and bpm, with paces also as text. The file is compact JSON: about 75 KB for a week, 1 MB for 24 weeks. After this update, press `r` (Reprocess) once on the main screen so rides recorded on two devices also get cadence and running dynamics.

### Workouts

Put workout files in the `workouts/` folder. Each file describes one structured workout (running, cycling or pool swimming) as JSON, following [`src/workout_schema.json`](src/workout_schema.json). See `workouts/example_6x600.json` and `workouts/example_swim.json` for examples.

| Key | Action |
| --- | --- |
| `u` | Upload the selected workout (and schedule it, if it has a `schedule_date`) |
| `U` | Upload the whole week of the selected archive (asks first) |
| `x` / `Enter` | Expand or collapse a week |
| `r` | Rescan the folder |

Past plans move to `workouts/archive/` automatically (see Settings). Nothing is deleted, and the app still knows they were uploaded.

**Planning with an AI assistant:** give an AI assistant (ChatGPT, Claude, …) the contents of `src/workout_schema.json` and ask it for workouts that follow it. A whole week can come as one `.zip` with one `.json` per workout. Drop the zip into `workouts/`; the app unpacks it and shows it as a collapsible week.

### Pauses, virtual rides and route leaderboards

- **Pauses:** a stop of 10 s or more (no movement, power or cadence) or a gap in the recording counts as a pause. Computed laps, splits and loops, efficiency factor, decoupling and the export use moving time. The activity shows its moving time and where it paused.
- **A ride recorded twice** (e.g. on your Edge and in TrainingPeaks Virtual) shows each device's laps in its own LAPS section, and you can mark intervals in either. TPV laps at the route's lap line, so for a TPV ride the PHYSICAL section is the route's loops: lead-in, loop 1…N and the way out. The route comes from Garmin's title ("TrainingPeaks Virtual - Island Coasts"), and Sync names the route after it. Loop lengths come from the [TPV hub](https://tpvirtualhub.com/map) route list (fetched at most monthly) or, for other worlds, from the repeated lap distance.
- **Leaderboard (`b`)** on the Routes screen or in an activity: your own loops of that route (or whole passes, for routes ridden or run once through), fastest first by moving time, with pace/speed, power, W/kg on the day and HR. `o` switches to the best loop per ride.

### Right-click menus

In the desktop and web apps, and in the terminal app if it gets mouse clicks, right-click a row in the activity list, laps, calendar or workouts list for its actions: move or cancel a planned workout, link a plan with an activity, mark an interval, open the map, upload a week, and so on. Each entry shows its key. Entries that don't apply to that row are greyed out (desktop/web) or left out (terminal). In the calendar, double-click or Enter on a row opens its activity.

## Options

```bash
./run.sh --theme nord              # terminal app with another Textual theme (e.g. dracula, tokyo-night)
./run_web.sh --port 8080           # web app on another port (default 8000)
./run_web.sh --no-browser          # don't open a browser tab automatically
```

The web app listens on `127.0.0.1` only, so other computers on your network can't reach it. It has access to your Garmin session, so keep it that way.

## Where your data lives

All data stays in the project folder, and git ignores it:

| Folder | Contents |
| --- | --- |
| `.garmin_session/` | Garmin login tokens (private) |
| `data/` | Downloaded activity files, your lap marks and route names, caches (calendar, map tiles, …) |
| `workouts/` | Your workout plans (only the `example_*` files are part of the repo); past ones in `workouts/archive/` |
| `exports/` | Training exports (`e` in the calendar) |

To start over, delete `data/`; the next sync downloads everything again. Deleting `data/` also loses your interval marks and route names.

## Troubleshooting

- **"Python 3.12 or newer is required"**: install a newer Python (see above) and run the installer again. If `.venv/` was created with an older Python, delete it first.
- **".venv not found"**: run `install.sh` / `install.bat` first.
- **Login fails or keeps asking for login**: delete `.garmin_session/` and log in again. Garmin sometimes rate-limits logins; if it does, wait a while before retrying.
- **The first sync is slow**: it downloads and parses every activity file once. Later syncs only fetch new activities.
- **Desktop app on Linux: "Could not load the Qt platform plugin xcb"**: install `libxcb-cursor0` (see the Linux section).
- **Copy (`y`) does nothing in the terminal app**: on Linux, install `wl-clipboard`, `xclip` or `xsel`. Otherwise use a terminal that supports clipboard access (OSC 52), such as Windows Terminal, iTerm2, kitty or WezTerm.
- **Charts look broken in the terminal**: use a terminal with Unicode and true-colour support, and make the window wider. The track outline next to the laps table needs at least 150 columns.

## Credits

- Garmin Connect access: [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
- Activity files: [fitparse](https://github.com/dtcooper/python-fitparse)
- Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors. Tiles are cached locally and fetched sparingly, as the OSM tile policy requires.
- Built with [Textual](https://textual.textualize.io), [PySide6](https://doc.qt.io/qtforpython-6/) and [NiceGUI](https://nicegui.io)
