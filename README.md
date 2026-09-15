# JOURNALMAX

A private, self-hosted diary-card service with separate patient, therapist, and content-blind administrator experiences. Submitted cards are ordinary Markdown files on a configurable data volume; operational metadata lives in a local SQLite volume.

## Deploy

Production consumes versioned images from GHCR while its configuration, SQLite state, and SMB card archive remain on the VM. Follow the complete [production VM runbook](docs/production-vm.md).

Development continues to use the local build in `compose.yaml`. Publishing a semantic version tag runs the test suite, builds and attests a container image, and creates a GitHub Release. Ordinary pushes to `main` do not update production.

The production admin **Updates** workspace talks to a narrow root-owned host broker over a Unix socket. The broker verifies the release attestation, backs up SQLite, recreates only the application container, health-checks it, and automatically rolls back a failed candidate. The web container never receives the Docker socket.

The app intentionally serves HTTP. Terminate HTTPS at your existing reverse proxy. Publish only `/login/`, `/logout/`, `/invite/`, `/review/`, `/static/`, and `/health/live` on the therapist hostname. Keep `/journal/`, `/control/`, and `/api/` behind the VPN. Proxy filtering is defense in depth; the app also checks roles for every route.

## Storage and recovery

- `/var/lib/diary-state`: local SQLite state; back up with a SQLite-consistent snapshot.
- `/data/cards`: NAS-backed Markdown and attachments; protect it with encrypted storage, encrypted SMB transport, least-privilege permissions, snapshots, and tested restores.
- `.journalmax-volume.json` identifies a recognized archive. When marker enforcement is enabled, Journalmax fails closed if the mount disappears, the marker is invalid, or the configured volume ID does not match.
- Card folders are date-prefixed and immutable after submission. Addenda are separate files.
- Maintenance deletion moves a complete folder to `/data/cards/.quarantine` for seven days. The admin can restore it from the control plane. A scheduler/management command for automatic final purge should be enabled only after backup retention is documented.
- Run `python manage.py purge_quarantine` daily from the host scheduler to complete expired maintenance deletions.
- Direct editing of card files is unsupported because it bypasses immutability and auditing.

## Metadata API

Create a read-only token with `python manage.py create_api_token USERNAME`. Supply it as `Authorization: Bearer TOKEN` to `/api/v1/cards`, `/api/v1/cards/{uuid}`, or `/api/v1/ready`. The API never returns prompts, answers, emotions, notes, filenames, Markdown, or attachment bytes.

## Security boundary

This software is not a certification of HIPAA compliance and is not an emergency or continuously monitored service. The operator remains responsible for reverse-proxy TLS, network policy, encrypted storage, backups, host patching, access review, incident response, and determining any legal or professional obligations.

## Daily recording

The patient page accepts quick notes and attachments before **7 pm**, then opens
the existing emotions/reflection form until **midnight**. These times use
`DIARY_TIME_ZONE` (UTC by default). Administrators can change the shared opening
time in **Maintenance → Evening reflection**. **Save for tomorrow** schedules a
next-day change; **Apply now** changes today’s window immediately and replaces
any change scheduled for tomorrow. Refresh the patient page after applying.

Earlier notes and uploads are hidden during the full form and return after
**Lock + Submit**. Submitted cards still accept addenda and attachments. Each
text quick note is saved as a separate timestamped Markdown file. The total
attachment allowance includes early uploads.

Patient and assigned-therapist archives include every day since the patient
joined: **Complete** for submitted cards, **Incomplete** for saved notes, files,
or unfinished form drafts, and **Missing** when nothing was saved. Today remains
**In progress** until submission or midnight. At midnight, unsubmitted days close
permanently and their saved content becomes readable by assigned therapists.
Save Draft stores responses in the server database; unsaved browser text is not
an archived entry. Existing imported cards remain accessible.

Apply database migrations before starting an updated preview:

```bash
PYTHONPATH=.deps python3 manage.py migrate
PYTHONPATH=.deps DIARY_DEBUG=1 python3 manage.py runserver 127.0.0.1:8000 --noreload
```

Validate recording and existing behavior with `PYTHONPATH=.deps python3 manage.py
test` and browser-state handling with `node --test tests/recording.test.cjs`.
