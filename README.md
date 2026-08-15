# NULL//DIARY

A private, self-hosted diary-card service with separate patient, therapist, and content-blind administrator experiences. Submitted cards are ordinary Markdown files on a configurable data volume; operational metadata lives in a local SQLite volume.

## Deployment choice

The application itself runs in Docker. Proxmox recommends running application containers such as Docker inside a QEMU VM for stronger isolation and easier migration. A Debian VM is therefore the recommended long-term deployment. An unprivileged Debian LXC with nesting enabled is convenient for a private lab test and is documented below, but it provides a weaker isolation boundary because it shares the Proxmox host kernel.

References: [Proxmox VE container documentation](https://pve.proxmox.com/pve-docs/pct.1.html) and [Docker's Debian installation guide](https://docs.docker.com/engine/install/debian/).

## Quick Proxmox LXC test installation

These steps create a LAN-only test instance. Do not publish it on the internet until HTTPS, route filtering, backups, and account recovery have been tested.

### 1. Create the LXC

Download a current Debian 12 or Debian 13 template in Proxmox, then create an **unprivileged** container with approximately:

- 2 CPU cores
- 2 GiB RAM
- 16 GiB local root disk
- A static DHCP lease or static LAN address
- Start at boot enabled
- Proxmox firewall enabled

In the container's **Options → Features**, enable `nesting` and `keyctl`. From the Proxmox host shell, the equivalent command is:

```bash
pct set <CTID> -features nesting=1,keyctl=1
pct start <CTID>
```

Replace `<CTID>` with the numeric container ID. If Docker storage or networking behaves unexpectedly inside LXC, use a Debian QEMU VM instead; that is Proxmox's supported recommendation for Docker workloads.

### 2. Install Docker inside Debian

Open the container console or SSH into it as root. Install Docker Engine and the Compose plugin from Docker's official Debian repository:

```bash
apt update
apt install -y ca-certificates curl git openssl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker run --rm hello-world
docker compose version
```

Docker warns that published container ports can bypass some guest firewall tools. Enforce the test network boundary with the Proxmox firewall as well as any Debian rules.

### 3. Choose card storage

For a first smoke test without the NAS:

```bash
install -d -m 0750 /srv/journal-maxx/cards
```

For the NAS, the preferred LXC arrangement is to mount SMB3 on the **Proxmox host**, then bind-mount that directory into the LXC. This avoids giving the LXC extra mount privileges.

On the Proxmox host, create a dedicated NAS account limited to one diary directory, then create a root-only credentials file:

```bash
apt update
apt install -y cifs-utils
install -d -m 0750 /mnt/bindmounts/journal-maxx
install -m 0600 /dev/null /root/.smb-journal-maxx
nano /root/.smb-journal-maxx
```

The credentials file contains:

```ini
username=DIARY_NAS_USER
password=DIARY_NAS_PASSWORD
domain=WORKGROUP
```

Add an SMB3 mount to the Proxmox host's `/etc/fstab`, adjusting the server and share. The `uid=100000,gid=100000` values match root inside a default unprivileged LXC; verify or adjust them if the container uses custom ID mappings.

```fstab
//NAS_ADDRESS/DIARY_SHARE /mnt/bindmounts/journal-maxx cifs credentials=/root/.smb-journal-maxx,vers=3.1.1,seal,uid=100000,gid=100000,file_mode=0660,dir_mode=0770,nofail,_netdev,x-systemd.automount 0 0
```

Mount and verify it on the Proxmox host:

```bash
systemctl daemon-reload
mount /mnt/bindmounts/journal-maxx
findmnt /mnt/bindmounts/journal-maxx
```

Stop the LXC and add the bind mount:

```bash
pct stop <CTID>
pct set <CTID> -mp0 /mnt/bindmounts/journal-maxx,mp=/mnt/journal-maxx
pct start <CTID>
pct exec <CTID> -- test -w /mnt/journal-maxx
```

Bind-mounted data is not included in normal LXC `vzdump` backups. Back up and snapshot the NAS data separately. Keep SQLite on the LXC's local disk, never on SMB.

### 4. Clone and configure NULL//DIARY

Inside the Debian LXC:

```bash
git clone https://github.com/murmuur22/journal-maxx.git /opt/journal-maxx
cd /opt/journal-maxx
cp .env.example .env
chmod 0600 .env
nano .env
```

For a temporary LAN-only test, use settings similar to these:

```dotenv
DIARY_SECRET_KEY=PASTE_A_LONG_RANDOM_VALUE_HERE
DIARY_ALLOWED_HOSTS=192.168.1.50,localhost,127.0.0.1
DIARY_CSRF_TRUSTED_ORIGINS=
DIARY_TIME_ZONE=America/New_York
DIARY_BEHIND_HTTPS_PROXY=0
DIARY_STATE_ROOT=/var/lib/diary-state
DIARY_CARD_ROOT=/data/cards
DIARY_HOST_CARD_PATH=/srv/journal-maxx/cards
DIARY_BIND_ADDRESS=0.0.0.0
```

Replace `192.168.1.50` with the LXC's address and choose your actual IANA time zone. Generate the secret without putting it in shell history:

```bash
openssl rand -base64 48
```

If you mounted the NAS, use this instead:

```dotenv
DIARY_HOST_CARD_PATH=/mnt/journal-maxx
```

`DIARY_BIND_ADDRESS=0.0.0.0` makes port 8800 reachable from the LAN. Limit access to trusted LAN/VPN sources with the Proxmox firewall. In the eventual reverse-proxy deployment, change it back to `127.0.0.1` and set `DIARY_BEHIND_HTTPS_PROXY=1`.

### 5. Build and start

```bash
cd /opt/journal-maxx
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 diary
```

Confirm the health endpoint from the LXC and another LAN machine:

```bash
curl --fail http://127.0.0.1:8800/health/live
curl --fail http://192.168.1.50:8800/health/live
```

Both should return `{"status": "ok"}`.

### 6. Bootstrap accounts

Create the content-blind administrator:

```bash
docker compose exec diary python manage.py bootstrap_diary --username operator
```

The command prints an initial password, authenticator key, and one-use recovery codes. Save them immediately in a password manager; command output is the only time the recovery codes are shown.

Open `http://192.168.1.50:8800/control/`, sign in as `operator`, and create:

1. One patient invitation.
2. One therapist/reviewer invitation.

Open each generated link in the appropriate browser profile. The therapist must enroll the displayed TOTP key in an authenticator app before account activation.

## Test checklist

Before adding real diary content:

1. Sign in as the patient and save a draft. Refresh and confirm it remains present.
2. Select several emotions, assign intensities, write a short test response, attach a small text or image file, and submit.
3. Confirm submission creates a date-prefixed folder and readable Markdown:

   ```bash
   find "$(grep '^DIARY_HOST_CARD_PATH=' .env | cut -d= -f2-)" -maxdepth 3 -type f -print
   ```

4. Confirm the patient cannot edit the submitted original but can append an addendum.
5. Sign in as the therapist and verify the card, trends, attachment download, tags, collections, star, read state, and private note.
6. Sign in as the administrator and confirm diary text and filenames are not visible.
7. Quarantine the test card using its UUID, verify that patient and therapist access disappears, then restore it.
8. Temporarily stop or unmount the NAS and confirm submission is blocked instead of reporting a false success.
9. Run the automated application suite:

   ```bash
   docker compose exec diary python manage.py check
   docker compose exec diary python manage.py test -v 2
   ```

10. Review logs for secrets or diary content:

    ```bash
    docker compose logs diary
    ```

## HTTPS and split access

The app intentionally serves HTTP. Terminate HTTPS at your existing reverse proxy. Publish only `/login/`, `/logout/`, `/invite/`, `/review/`, `/static/`, and `/health/live` on the therapist hostname. Keep `/journal/`, `/control/`, and `/api/` behind the VPN. Proxy filtering is defense in depth; application role checks still protect every route.

When the proxy is ready:

```dotenv
DIARY_BIND_ADDRESS=127.0.0.1
DIARY_BEHIND_HTTPS_PROXY=1
DIARY_ALLOWED_HOSTS=review.example.com,diary.vpn.example.com
DIARY_CSRF_TRUSTED_ORIGINS=https://review.example.com,https://diary.vpn.example.com
```

Recreate the container after changing `.env`:

```bash
docker compose up -d --force-recreate
```

## Routine operations

### Upgrade

```bash
cd /opt/journal-maxx
git pull --ff-only
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 diary
```

### Metadata API

Create a read-only token:

```bash
docker compose exec diary python manage.py create_api_token USERNAME
```

Supply it as `Authorization: Bearer TOKEN` to `/api/v1/cards`, `/api/v1/cards/{uuid}`, or `/api/v1/ready`. The API never returns prompts, answers, emotions, notes, filenames, Markdown, or attachment bytes.

### Quarantine purge

Run this daily from a scheduler after documenting backup retention:

```bash
cd /opt/journal-maxx
docker compose exec -T diary python manage.py purge_quarantine
```

### Backups

- `/var/lib/diary-state` inside the named Docker volume contains SQLite and operational metadata. Back it up using a SQLite-consistent method or a stopped-container volume snapshot.
- `/data/cards` is the NAS-backed Markdown and attachment collection. Protect it with encrypted storage, SMB3 encryption, least-privilege permissions, snapshots, and tested restores.
- Back up the `.env` secret securely. Without the same `DIARY_SECRET_KEY`, encrypted TOTP secrets cannot be decrypted after restore.
- Perform a restore test before trusting the deployment with real information.

## Stop or remove the test

Stop the application while retaining data:

```bash
cd /opt/journal-maxx
docker compose down
```

Do not add `--volumes` unless you intentionally want to delete the local SQLite state. NAS card files are outside the Docker volume and must be managed separately.

## Security boundary

This software is not a certification of HIPAA compliance and is not an emergency or continuously monitored service. The operator remains responsible for reverse-proxy TLS, network policy, encrypted storage, backups, host patching, access review, incident response, and determining any legal or professional obligations.
