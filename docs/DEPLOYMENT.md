# SMARTfill Raspberry Pi Deployment

This guide installs SMARTfill Payroll on a Raspberry Pi or any Debian/Ubuntu Linux server.

## 1. Push From Mac

```bash
cd "/Users/smit/Desktop/Payroll WebApp"
git status
git add .
git commit -m "Initial SMARTfill Payroll app"
git push -u origin main
```

The repository is private, so GitHub authentication is required before pushing.

## 2. Give Raspberry Pi GitHub Access

Recommended method: SSH deploy key.

On the Pi:

```bash
ssh-keygen -t ed25519 -C "smartfill-rpi"
cat ~/.ssh/id_ed25519.pub
```

Copy the public key into GitHub:

```text
Repository -> Settings -> Deploy keys -> Add deploy key
```

Read-only access is enough for pulling updates.

## 3. Install On Raspberry Pi

```bash
cd /tmp
git clone git@github.com:smitz96/payroll.git
cd payroll
sudo APP_PORT=3000 REPO_URL=git@github.com:smitz96/payroll.git bash install.sh
```

The installer creates:

```text
/opt/smartfill-attendance
smartfill Linux user
smartfill-attendance systemd service
```

SMARTfill runs on:

```text
http://PI_IP_ADDRESS:3000
```

## 4. Service Commands

```bash
sudo systemctl status smartfill-attendance
sudo systemctl restart smartfill-attendance
journalctl -u smartfill-attendance -f
```

## 5. Future Updates

Push from Mac:

```bash
git add .
git commit -m "Describe change"
git push
```

Update on Pi using the app:

```text
Settings -> Server Update -> Git Pull Update
```

Or update from terminal:

```bash
cd /opt/smartfill-attendance
sudo -u smartfill git pull --ff-only
sudo -u smartfill venv/bin/python scripts/init_db.py
sudo systemctl restart smartfill-attendance
```

## 6. Backup

Back up these directories before upgrades or SD card changes:

```text
/opt/smartfill-attendance/data
/opt/smartfill-attendance/uploads
/opt/smartfill-attendance/output
```

The SQLite database is stored in:

```text
/opt/smartfill-attendance/data/attendance.db
```
