# NULL//DIARY

A private, self-hosted diary-card service with separate patient, therapist, and content-blind administrator experiences. Submitted cards are ordinary Markdown files on a configurable data volume; operational metadata lives in a local SQLite volume.

## Deploy

1. Mount the encrypted SMB3 NAS share on the Docker host. Do not place SQLite on SMB.
2. Copy `.env.example` to `.env`, generate a long random secret, and set `DIARY_HOST_CARD_PATH` to a dedicated directory on that host mount.
3. Start the container: `docker compose up -d --build`.
4. Bootstrap the administrator:
   `docker compose exec diary python manage.py bootstrap_diary --username operator`
5. Sign into `/control/` with the administrator account and generate separate single-use invitation links for the patient and therapist. The therapist enrolls TOTP while accepting the invitation and receives one-time recovery codes.

The app intentionally serves HTTP. Terminate HTTPS at your existing reverse proxy. Publish only `/login/`, `/logout/`, `/invite/`, `/review/`, `/static/`, and `/health/live` on the therapist hostname. Keep `/journal/`, `/control/`, and `/api/` behind the VPN. Proxy filtering is defense in depth; the app also checks roles for every route.

## Storage and recovery

- `/var/lib/diary-state`: local SQLite state; back up with a SQLite-consistent snapshot.
- `/data/cards`: NAS-backed Markdown and attachments; protect it with encrypted storage, encrypted SMB transport, least-privilege permissions, snapshots, and tested restores.
- Card folders are date-prefixed and immutable after submission. Addenda are separate files.
- Maintenance deletion moves a complete folder to `/data/cards/.quarantine` for seven days. The admin can restore it from the control plane. A scheduler/management command for automatic final purge should be enabled only after backup retention is documented.
- Run `python manage.py purge_quarantine` daily from the host scheduler to complete expired maintenance deletions.
- Direct editing of card files is unsupported because it bypasses immutability and auditing.

## Metadata API

Create a read-only token with `python manage.py create_api_token USERNAME`. Supply it as `Authorization: Bearer TOKEN` to `/api/v1/cards`, `/api/v1/cards/{uuid}`, or `/api/v1/ready`. The API never returns prompts, answers, emotions, notes, filenames, Markdown, or attachment bytes.

## Security boundary

This software is not a certification of HIPAA compliance and is not an emergency or continuously monitored service. The operator remains responsible for reverse-proxy TLS, network policy, encrypted storage, backups, host patching, access review, incident response, and determining any legal or professional obligations.
