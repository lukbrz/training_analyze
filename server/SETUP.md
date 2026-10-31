# Training Analyze on a home server

This guide installs the web app on an Arch Linux server. You then open it from your laptop or phone over the WireGuard VPN on your MikroTik router.

**What you get:**

- **Access:** the web app at `http://<server>:8080`, behind a password.
- **Automatic updates of your data:**
  - It syncs with Garmin when you open it, if the last sync was over an hour ago.
  - Every night it syncs, reprocesses activities (only after an app update), and refreshes the calendar.
- **Isolation:** it runs as an unprivileged user in a rootless Podman container. No sudo is needed after setup, and it starts at boot.
- **Several athletes:** each gets their own container, password, Garmin login, data folder and port.
- **Laptop:** the laptop app keeps working on its own. It can also open the server instead (see [Daily use](#7-daily-use)).

Commands marked **(root)** need sudo once, during setup. Everything else runs as the service user `training`.

---

## 1. Prepare the server (root)

```bash
sudo pacman -Syu podman passt git
```

Podman runs containers without a daemon, and rootless (without root rights). `passt` gives rootless containers their network access.

Create the service user that runs the app. It gets no sudo rights:

```bash
sudo useradd -m -s /bin/bash training
sudo passwd training          # or put your SSH key in /home/training/.ssh/authorized_keys
```

Rootless containers need sub-UID/GID ranges for the user. Arch's `useradd` usually adds them; check, and add them if they're missing:

```bash
grep training /etc/subuid /etc/subgid \
  || sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 training
```

Let the user's services run without anyone logged in, and start at boot:

```bash
sudo loginctl enable-linger training
```

If the server runs a firewall (`systemctl is-active ufw`), open the app's ports to your LAN and to the WireGuard clients. Use your own subnets:

```bash
sudo ufw allow from 192.168.88.0/23 to any port 8080:8089 proto tcp   # LAN (88.x and 89.x)
sudo ufw allow from <wireguard-subnet> to any port 8080:8089 proto tcp
```

That's the last root step.

## 2. Log in as `training`

Use **SSH** (`ssh training@<server>`, or `ssh training@localhost` on the server) or `sudo machinectl shell training@`.

**Don't use `su training` / `su - training` / `sudo -u training`.** They keep the previous user's `XDG_RUNTIME_DIR` and start no systemd session for `training`. Podman then fails with `error opening namespace handles: Permission denied`, and `systemctl --user` doesn't work. If you already tried that way, log in properly and run `podman system migrate` once.

Check that rootless Podman works:

```bash
podman info --format '{{.Host.Security.Rootless}}'    # → true
```

## 3. Get the code

The repository is private, so give the server its own **read-only deploy key**:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/training_analyze_deploy
cat ~/.ssh/training_analyze_deploy.pub
```

On GitHub, go to the repository's **Settings → Deploy keys → Add deploy key**, paste the key, and leave "Allow write access" off. Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-training-analyze
    HostName github.com
    IdentityFile ~/.ssh/training_analyze_deploy
    IdentitiesOnly yes
EOF
git clone git@github-training-analyze:lukbrz/training_analyze.git ~/training_analyze
```

Alternatively, copy the repository from your laptop: `rsync -a --exclude data --exclude .venv ~/user/training_analyze/ training@<server>:training_analyze/`.

## 4. Build the image

```bash
cd ~/training_analyze
podman build -t training-analyze -f server/Containerfile .
```

The image (about 300 MB) contains the web app and the terminal app, but no desktop (Qt) app. Your data stays out of it: `.containerignore` lets only `src/` in.

## 5. Add an athlete

```bash
~/training_analyze/server/add-athlete.sh lukasz 8080
```

This does five things:

- creates the data folder `~/training-analyze/lukasz/`;
- creates a sign-in password and prints it once. It's stored in `~/.config/training-analyze/lukasz.env`, where you can change it;
- writes a Podman *quadlet* unit, `~/.config/containers/systemd/training-analyze-lukasz.container`;
- starts the service;
- makes it start at boot.

Check it:

```bash
systemctl --user status training-analyze-lukasz
curl http://127.0.0.1:8080/health          # → {"status":"ok","garmin":false}
```

**More athletes:** one command each, with their own port, password, Garmin account and data:

```bash
~/training_analyze/server/add-athlete.sh anna 8081
```

To listen on one address only (e.g. the server's LAN address), add it as the third argument: `add-athlete.sh lukasz 8080 192.168.88.10`.

## 6. Bring in your data, or log in fresh

**Fresh start:** open `http://<server>:8080`, sign in with the printed password, and log in to Garmin (password + MFA code) on the page that follows. Then press **Sync**, or **Sync full history** (`H`) for everything.

**Move your laptop data:** this keeps your interval marks, links, route names and settings, and usually the Garmin login too. Stop the service, copy, start it again:

```bash
# on the server
systemctl --user stop training-analyze-lukasz
# on the laptop, from the project folder
rsync -a --info=progress2 data workouts exports .garmin_session training@<server>:training-analyze/lukasz/
# on the server
systemctl --user start training-analyze-lukasz
```

If Garmin asks you to log in again in the browser, the copied login tokens didn't carry over. Logging in once fixes it.

The laptop then still has its own copy. Both sync from Garmin, but marks or links you make on one don't appear on the other. Treat the server as the main copy.

## 7. Daily use

**From a browser over the VPN:** `http://192.168.88.10:8080`, with your server's LAN address. The app keeps you signed in on that browser. "Copy table" works over plain http too.

**Desktop app from your laptop, as a window onto the server:**

```bash
./run_gui.sh --server http://192.168.88.10:8080
# or set it once: export TA_SERVER_URL=http://192.168.88.10:8080
```

You see the same data as on every other device; exports save to your Downloads folder. Without `--server`, the desktop app works on the laptop's own data as before.

**Terminal app on the server:**

```bash
ssh training@<server>
podman exec -it training-analyze-lukasz python src/app.py
```

**New workout plans:** on the Workouts page, drop the planning agent's `.zip` (or single `.json` files) into the upload box, then upload the week to Garmin (`U`) as usual.

**What runs by itself:**

- **When the app is opened:** if the last sync is over an hour old, it syncs in the background. The main page shows its progress and refreshes when it's done.
- **Every night at 03:00:**
  - sync;
  - reprocess, but only when an update changed how activities are read (set `TA_NIGHTLY_REPROCESS=always` to reprocess every night);
  - this week's calendar and load refresh.
- **One job at a time:** a sync, reprocess or export started while another runs is refused with a message, even across the web app and the terminal app.

## 8. Router: WireGuard on the MikroTik

The VPN ends on the router, so the server needs no VPN of its own. VPN clients just reach its LAN address. On RouterOS 7 (adjust the interface name, addresses and ports):

```text
# give the server a fixed address (find its lease first)
/ip dhcp-server lease print where host-name~"<server>"
/ip dhcp-server lease make-static numbers=<n>

# optional: a name for it
/ip dns static add name=training.lan address=192.168.88.10

# let VPN clients reach the app's ports (needed if your forward chain drops by default)
/ip firewall filter add chain=forward in-interface=wireguard1 dst-address=192.168.88.10 \
    protocol=tcp dst-port=8080-8089 action=accept comment="Training Analyze from VPN" \
    place-before=[find comment="defconf: drop all from WAN not DSTNATed"]
```

**Don't** add a port forward (dst-nat) from the internet to these ports. The app should only be reachable from your LAN and the VPN.

## 9. Updates

**Automatically, every night** (turn it on once):

```bash
~/training_analyze/server/enable-auto-update.sh          # at 02:30; or give a time: ... 01:45
```

At that time the server pulls the code. If there are new commits, it rebuilds the image and restarts every athlete's service, then checks that each one answers. If one doesn't, it goes back to the previous image, so a broken update can't leave the app down. The 03:00 nightly run then reprocesses activities if the update changed how they're read.

- See what the last run did: `journalctl --user -u training-analyze-update -n 30`
- Next run: `systemctl --user list-timers training-analyze-update.timer`
- Turn it off: `~/training_analyze/server/enable-auto-update.sh off`

**By hand**, any time:

```bash
~/training_analyze/server/update.sh            # only if the code changed
~/training_analyze/server/update.sh --force    # rebuild and restart anyway
```

## 10. Backups

All state is plain files: `~/training-analyze/` (one folder per athlete) and `~/.config/training-analyze/` (passwords). The `.garmin_session` folders hold Garmin login tokens, so keep backups private.

A simple nightly copy to another disk with a user timer:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/training-backup.service <<'EOF'
[Unit]
Description=Back up Training Analyze data

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'tar -czf /mnt/backup/training-analyze-$(date +%%Y%%m%%d).tar.gz -C %h training-analyze .config/training-analyze'
EOF
cat > ~/.config/systemd/user/training-backup.timer <<'EOF'
[Unit]
Description=Nightly Training Analyze backup

[Timer]
OnCalendar=*-*-* 05:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now training-backup.timer
```

`/mnt/backup` must be writable by `training`. For versioned, de-duplicated backups, use `restic` or `borg` on the same two folders instead. The `records_cache` and `tile_cache` folders inside `data/` can be left out; the app rebuilds them.

## 11. Settings

Each athlete's settings are in `~/.config/training-analyze/<name>.env`. Restart that athlete's service after editing (`systemctl --user restart training-analyze-<name>`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `TA_PASSWORD` | (generated) | Sign-in password; required, because the app won't listen on the network without one |
| `TA_NIGHTLY_AT` | `03:00` | Time of the nightly run (server's local time) |
| `TA_NIGHTLY_REPROCESS` | `when-changed` | `always` reprocesses every night (takes long with many activities) |
| `TA_SECRET` | (generated) | Signs the sign-in cookie; by default a random value kept in `data/.web_secret` |
| `TZ` | `Europe/Warsaw` | Time zone; dates in the calendar and exports follow it |
| `TA_LOG_LEVEL` | `INFO` | `DEBUG` also logs fast clicks and every step of a sync (see [What the log shows](#what-the-log-shows)) |

## 12. Troubleshooting

| Problem | What to check |
| --- | --- |
| Service won't start | `journalctl --user -u training-analyze-lukasz -e`; is the image built (`podman images`)? |
| "Unit not found" after add-athlete | `/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun` shows quadlet errors |
| Page doesn't open (browser waits forever) | `curl http://127.0.0.1:8080/health` on the server. If that works, the server's firewall drops the port: open 8080-8089/tcp for the LAN and VPN (`ufw allow`, `firewall-cmd --add-port`, or an `accept` line in `/etc/nftables.conf`). Over the VPN, also check the router's forward rule (§8) |
| `curl localhost:8080` gives "connection reset" | `localhost` tries IPv6 first and the container forwards it, but the app listens on IPv4; use `127.0.0.1` |
| "A sync is running — try again" | Another job (e.g. the nightly run) is busy; wait or watch the main page's status line |
| Garmin asks to log in again | Garmin login tokens expired or didn't move; log in on the page shown |
| Files in the data folder owned by a strange user | The unit needs `UserNS=keep-id:uid=1000,gid=1000` (add-athlete.sh writes it); rerun add-athlete.sh |
| Logs | `journalctl --user -u training-analyze-lukasz -f`, or the file `~/training-analyze/lukasz/data/logs/web.log` (see below) |
| Update failed | `journalctl --user -u training-analyze-update -n 50`; the app keeps running on the previous image |

### What the log shows

Each athlete's app logs to the journal and to `~/training-analyze/<name>/data/logs/web.log`, which keeps the last 5 × 2 MB. Every line has a time, a level and a topic:

| Topic | What | Example |
| --- | --- | --- |
| `ta` | Startup: version (git commit), data folder, number of activities | `Training Analyze 1a2b3c4 starting on 0.0.0.0:8080 (server mode)` |
| `ta.page` | Each page the server sent, and how long it took | `GET /activity/248… -> 200 in 125 ms` |
| `ta.page` | The browser's own timing of the same page, with the screen width (≤ 700 px is a phone) | `browser /activity/248… (412px wide): first byte 142 ms, page ready 554 ms, loaded 856 ms` |
| `ta.page` | Slow steps of an activity page | `activity 248… (running, 2:12:43): loop detection: 1.42 s` |
| `ta.ui` | Actions (keys, footer, ☰ menu) and clicks slower than 0.1 s | `Activities: Sync (s): 4.40 s` |
| `ta.job` | Sync, reprocess, export and nightly runs: start, end, duration, result | `background sync done in 4.40 s: Sync complete: 0 new activities` |
| `ta.stall` | The server was busy and **every open page froze** meanwhile | `event loop blocked for 1.20 s` |

To see every click and each sync step too, add `TA_LOG_LEVEL=DEBUG` to `~/.config/training-analyze/<name>.env` and restart the service. To find what's slow: `grep -E "stall| [0-9.]+ s" ~/training-analyze/lukasz/data/logs/web.log`.
