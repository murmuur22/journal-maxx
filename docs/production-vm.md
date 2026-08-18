# JOURNALMAX production VM

This runbook creates one production installation that consumes versioned GHCR images. Application containers are replaceable; configuration, SQLite state, and the SMB card archive remain host-owned.

## 1. Prepare the VM

Install Docker Engine with the Compose plugin and the operating system's CIFS/SMB client package. On Debian or Ubuntu the client package is `cifs-utils`. Do not install the SQLite state database on SMB.

Create a host identity matching the fixed container UID and persistent directories:

```bash
sudo groupadd --gid 10001 journalmax
sudo useradd --uid 10001 --gid journalmax --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin journalmax
sudo install -d -o journalmax -g journalmax -m 0700 /var/lib/journalmax/state
sudo install -d -o root -g root -m 0700 /mnt/journalmax-cards
sudo install -d -o root -g root -m 0750 /opt/journalmax
```

If UID or GID 10001 already exists, resolve that conflict before continuing. Do not change only the host ownership: the container deliberately runs as 10001.

## 2. Mount the SMB archive

Create a root-owned credentials file without putting its password in `/etc/fstab`:

```bash
sudo install -m 0600 /dev/null /root/.journalmax-smb
sudoedit /root/.journalmax-smb
```

Its contents are:

```ini
username=SMB_ACCOUNT
password=SMB_PASSWORD
domain=OPTIONAL_DOMAIN
```

Add the dedicated share to `/etc/fstab`, replacing `NAS` and `SHARE`:

```fstab
//NAS/SHARE /mnt/journalmax-cards cifs credentials=/root/.journalmax-smb,vers=3.1.1,seal,_netdev,nofail,x-systemd.automount,forceuid,forcegid,uid=10001,gid=10001,file_mode=0600,dir_mode=0700 0 0
```

Activate and verify it before starting Journalmax:

```bash
sudo systemctl daemon-reload
sudo mount /mnt/journalmax-cards
findmnt /mnt/journalmax-cards
sudo -u journalmax test -r /mnt/journalmax-cards
sudo -u journalmax test -w /mnt/journalmax-cards
```

The SMB account should be restricted to this share. Enable NAS snapshots and a separate encrypted backup; RAID alone is not a backup.

## 3. Install the release and updater

The host updater is intentionally separate from the web container. It runs as root, exposes only three fixed operations over a group-owned Unix socket, and is the only component allowed to control Docker or copy SQLite. Install a current GitHub CLI (`gh`) from GitHub's official package repository as well as Docker; `gh attestation verify --help` must succeed. The updater rejects an image that was not built from the requested version tag and attested by this repository's release workflow on a GitHub-hosted runner.

Download these assets from the selected GitHub Release:

- `compose.production.yaml`
- `journalmax-production.env.example`
- `journalmax_updater.py`
- `journalmax-updater.service`
- `journalmax-updater.tmpfiles.conf`
- `updater.json.example`

Store them as follows:

```bash
sudo install -d -o root -g root -m 0750 /opt/journalmax/updater
sudo install -o root -g root -m 0644 compose.production.yaml /opt/journalmax/compose.production.yaml
sudo install -o root -g root -m 0600 journalmax-production.env.example /opt/journalmax/.env.production
sudo install -o root -g root -m 0755 journalmax_updater.py /opt/journalmax/updater/journalmax_updater.py
sudo install -d -o root -g root -m 0755 /etc/journalmax
sudo install -o root -g root -m 0600 updater.json.example /etc/journalmax/updater.json
sudo install -o root -g root -m 0644 journalmax-updater.service /etc/systemd/system/journalmax-updater.service
sudo install -o root -g root -m 0644 journalmax-updater.tmpfiles.conf /etc/tmpfiles.d/journalmax-updater.conf
sudo install -o root -g root -m 0600 /dev/null /etc/journalmax/updater.env
UPDATER_GITHUB_TOKEN=$(gh auth token)
sudo sh -c 'printf "GH_TOKEN=%s\n" "$1" > /etc/journalmax/updater.env' sh "$UPDATER_GITHUB_TOKEN"
unset UPDATER_GITHUB_TOKEN
sudoedit /opt/journalmax/.env.production
sudoedit /etc/journalmax/updater.json
sudo systemd-tmpfiles --create /etc/tmpfiles.d/journalmax-updater.conf
sudo systemctl daemon-reload
sudo systemctl enable --now journalmax-updater.service
sudo systemctl status journalmax-updater.service
sudo test -S /run/journalmax-updater/updater.sock
```

Set at least:

- `JOURNALMAX_RELEASE` to the exact released version, without the leading `v`.
- `DIARY_SECRET_KEY` to a stable random value, for example output from `openssl rand -base64 48`.
- The real allowed host, HTTPS origin, and timezone.
- `DIARY_HOST_CARD_PATH=/mnt/journalmax-cards`.

Never regenerate `DIARY_SECRET_KEY` during an update.

The default updater configuration expects the files above in `/opt/journalmax`, the web service on `127.0.0.1:8800`, and the container group to use GID 10001. If those deliberate defaults differ, edit `/etc/journalmax/updater.json` before starting the service. Do not point the updater at a general-purpose Compose project.

Authenticate root's GitHub CLI and container client once. Use a dedicated GitHub token limited to reading this public repository and package; the updater never needs write access:

```bash
sudo gh auth login
sudo docker login ghcr.io
```

## 4. Start and identify the storage volume

Use the production environment file for both Compose interpolation and the container environment:

```bash
cd /opt/journalmax
sudo docker compose --env-file .env.production -f compose.production.yaml pull
sudo docker compose --env-file .env.production -f compose.production.yaml up -d
```

The first container will report unhealthy because the uninitialized SMB directory intentionally fails the readiness check. Initialize it once:

```bash
sudo docker compose --env-file .env.production -f compose.production.yaml exec diary \
  python manage.py init_card_volume --confirm-path /data/cards
```

Copy the printed volume ID into `DIARY_CARD_VOLUME_ID` in `.env.production`, then recreate and verify:

```bash
sudoedit /opt/journalmax/.env.production
sudo docker compose --env-file .env.production -f compose.production.yaml up -d --force-recreate
curl --fail http://127.0.0.1:8800/health/ready
```

For an existing inspected Journalmax archive, add `--adopt-existing` to the initialization command. Never use that flag on an unfamiliar directory.

## 5. Bootstrap access

Create the first administrator once:

```bash
sudo docker compose --env-file .env.production -f compose.production.yaml exec diary \
  python manage.py bootstrap_diary --username operator
```

Store the printed passphrase, authenticator key, and recovery codes securely. Put an HTTPS reverse proxy in front of `127.0.0.1:8800` before allowing real users to sign in.

## 6. Update from the control plane

Sign in as an administrator and open **Updates**. **Check for updates** reads the latest stable GitHub Release. If a newer semantic version exists, review its notes and choose **Install update**. A fresh authenticator or recovery code is required to authorize the operation.

The host service then:

1. Reads the release manifest itself; the browser cannot choose an image or digest.
2. Pulls the candidate by immutable digest and verifies its GitHub provenance attestation while the current service stays online.
3. Stops Journalmax, copies SQLite into `/var/lib/journalmax/updater/backups`, and atomically selects the new release.
4. Recreates only the `diary` service and waits for `/health/ready`.
5. If readiness fails, stops the candidate, restores the prior environment value and SQLite backup, restarts the previous release, and reports `ROLLED BACK`.

The SMB card archive is never copied or rewritten by the updater. Its path remains an external bind mount. The production environment file is preserved except for `JOURNALMAX_RELEASE`.

Updater diagnostics are available without exposing diary contents:

```bash
sudo systemctl status journalmax-updater.service
sudo journalctl -u journalmax-updater.service
sudo tail -n 100 /var/lib/journalmax/updater/updater.log
```

### Emergency manual recovery

If both the candidate and automatic rollback fail, leave the service off, inspect the updater log, restore the newest known-good SQLite file from `/var/lib/journalmax/updater/backups`, put the previous `JOURNALMAX_RELEASE` in `/opt/journalmax/.env.production`, then recreate `diary`. Never restore SQLite while the container is running.

Manual recreate commands:

```bash
cd /opt/journalmax
sudo docker compose --env-file .env.production -f compose.production.yaml stop diary
sudoedit /opt/journalmax/.env.production
sudo docker compose --env-file .env.production -f compose.production.yaml up -d --force-recreate diary
curl --retry 20 --retry-delay 3 --retry-all-errors --fail \
  http://127.0.0.1:8800/health/ready
```

## 7. Publish a release from development

After the development work is reviewed, committed, and pushed, create a semantic version tag:

```bash
git tag -a v0.2.1 -m "JOURNALMAX v0.2.1"
git push origin main
git push origin v0.2.1
```

The release workflow tests the tagged commit, publishes the container and provenance attestation, then creates the GitHub Release. A normal push to `main` never updates production.
