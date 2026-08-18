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

## 3. Install the release configuration

Download `compose.production.yaml` and `.env.production.example` from the selected GitHub Release, or copy them once from the development checkout. Store them in `/opt/journalmax`:

```bash
cd /opt/journalmax
sudo cp .env.production.example .env.production
sudo chmod 0600 .env.production
sudoedit .env.production
```

Set at least:

- `JOURNALMAX_RELEASE` to the exact released version, without the leading `v`.
- `DIARY_SECRET_KEY` to a stable random value, for example output from `openssl rand -base64 48`.
- The real allowed host, HTTPS origin, and timezone.
- `DIARY_HOST_CARD_PATH=/mnt/journalmax-cards`.

Never regenerate `DIARY_SECRET_KEY` during an update.

If the repository or GHCR package is private, authenticate Docker once with a GitHub token limited to package read access:

```bash
docker login ghcr.io
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

## 6. Manual update procedure

The admin-driven updater will automate this sequence. Until then, use it directly on the VM:

1. Read the GitHub Release notes and set `JOURNALMAX_RELEASE` to the exact new version.
2. Pull the candidate image while the existing container remains online.
3. Stop Journalmax and make a consistent state backup.
4. Start the new release and wait for `/health/ready`.
5. Keep the backup and previous image until the release is accepted.

Example backup and update:

```bash
cd /opt/journalmax
sudo docker compose --env-file .env.production -f compose.production.yaml pull
sudo docker compose --env-file .env.production -f compose.production.yaml stop diary
sudo install -d -o root -g root -m 0700 /var/backups/journalmax
sudo cp -a /var/lib/journalmax/state/diary.sqlite3 \
  /var/backups/journalmax/diary.sqlite3.pre-update
sudo docker compose --env-file .env.production -f compose.production.yaml up -d
curl --retry 20 --retry-delay 3 --retry-all-errors --fail \
  http://127.0.0.1:8800/health/ready
```

If readiness fails, stop the service, restore the pre-update SQLite file, set `JOURNALMAX_RELEASE` back to the previous version, and run `up -d` again. Do not restore the database while the container is running.

## 7. Publish a release from development

After the development work is reviewed, committed, and pushed, create a semantic version tag:

```bash
git tag -a v0.1.0 -m "JOURNALMAX v0.1.0"
git push origin main
git push origin v0.1.0
```

The release workflow tests the tagged commit, publishes the container and provenance attestation, then creates the GitHub Release. A normal push to `main` never updates production.
