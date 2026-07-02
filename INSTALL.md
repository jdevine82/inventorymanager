# Installing on a Proxmox Ubuntu LXC

These steps assume a fresh **Ubuntu** LXC container on Proxmox (Ubuntu 22.04/24.04
templates both work) and that you'll run the app as a systemd service so it
survives reboots.

## 1. Create the container

In the Proxmox web UI: **Create CT** → pick an Ubuntu template (download one
under **local → CT Templates** if none is present) → give it a hostname (e.g.
`inventory`), a few GB of disk, 512MB–1GB RAM, and a static IP (or a DHCP
reservation) on your LAN bridge. Unprivileged is fine.

Start the container, then open its console (or `pct enter <vmid>` from the
Proxmox host shell).

## 2. Base packages

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git
```

## 3. Create a service user and get the app onto the container

Running as a dedicated non-root user is safer than running the web server as
root:

```bash
useradd --system --create-home --shell /usr/sbin/nologin priceapp
```

Clone the repo (or `scp`/`rsync` the project directory over if you don't want
to use the git remote from the container):

```bash
cd /opt
git clone https://github.com/jdevine82/inventorymanager.git price-manager
chown -R priceapp:priceapp price-manager
cd price-manager
```

## 4. Python environment & dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 5. First run (manual test)

```bash
python app.py
```

This starts the server on `0.0.0.0:8000`. From another machine on your LAN,
browse to `http://<container-ip>:8000`. Confirm the page loads, then `Ctrl+C`
to stop it before setting up the service below.

The SQLite database (`prices.db`) is created automatically on first run in
the project directory.

Once you're done testing, hand the directory back to the service user:

```bash
chown -R priceapp:priceapp /opt/price-manager
```

## 6. Run as a systemd service (auto-start on boot)

Create `/etc/systemd/system/price-manager.service`:

```ini
[Unit]
Description=Border to Border Inventory Manager
After=network.target

[Service]
Type=simple
User=priceapp
Group=priceapp
WorkingDirectory=/opt/price-manager
ExecStart=/opt/price-manager/venv/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
systemctl daemon-reload
systemctl enable --now price-manager
systemctl status price-manager
```

Logs: `journalctl -u price-manager -f`

## 7. Firewall (if `ufw` is enabled in the container)

```bash
ufw allow 8000/tcp
```

## 8. Access

Open `http://<container-ip>:8000` from any browser on your network.

## 9. ServiceM8 API key

In the app, open **Settings** and paste your ServiceM8 API key (generate one
in ServiceM8 under **Settings → API Keys**). It's stored in `prices.db`,
which is git-ignored — it will not be pushed to the GitHub repo.

## Updating later

```bash
cd /opt/price-manager
git pull
chown -R priceapp:priceapp .
sudo -u priceapp venv/bin/pip install -r requirements.txt
systemctl restart price-manager
```

## Backing up

The entire app state lives in `prices.db`. The app's own **⬇ Backup DB**
toolbar button downloads it directly, or from the container:

```bash
cp /opt/price-manager/prices.db /opt/price-manager/backup/prices_$(date +%Y%m%d).db
```
