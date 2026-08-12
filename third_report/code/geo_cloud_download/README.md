# GEO Cloud Download

Run with the `pytorch` conda environment:

```powershell
conda run -n pytorch python code\geo_cloud_download\geo_cloud_downloader.py --root E:\GEO_Cloud_2024 inventory
conda run -n pytorch python code\geo_cloud_download\geo_cloud_downloader.py --root E:\GEO_Cloud_2024 download-test-day --date 2024-03-12
```

EUMETSAT credentials must be provided only as process environment variables:

```powershell
$env:EUMETSAT_CONSUMER_KEY = "<consumer key>"
$env:EUMETSAT_CONSUMER_SECRET = "<consumer secret>"
conda run -n pytorch python code\geo_cloud_download\geo_cloud_downloader.py --root E:\GEO_Cloud_2024 first-round
Remove-Item Env:\EUMETSAT_CONSUMER_KEY
Remove-Item Env:\EUMETSAT_CONSUMER_SECRET
```

Outputs are written under `E:\GEO_Cloud_2024\manifests`, `logs`, and the platform/product/day/hour data folders.

## Meteosat API smoke test

The Meteosat path uses EUMETSAT Data Store credentials and does not persist
secrets. In a PowerShell session:

```powershell
$env:EUMETSAT_CONSUMER_KEY = "<consumer key>"
$env:EUMETSAT_CONSUMER_SECRET = "<consumer secret>"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File code\geo_cloud_download\run_meteosat_api_smoke.ps1
Remove-Item Env:\EUMETSAT_CONSUMER_KEY
Remove-Item Env:\EUMETSAT_CONSUMER_SECRET
```

The smoke test writes:

- `E:\GEO_Cloud_2024\manifests\meteosat_collection_options.json`
- `E:\GEO_Cloud_2024\manifests\meteosat_smoke_2024-03-12_0000.json`

## Local-to-server transfer batches

Use `geo_ring_cloud_transfer_batch.ps1` to isolate a UTC date range in its own
local staging directory.  The runner can restrict downloads to GOES and/or
Meteosat, keeps `.part` downloads non-final, validates source files, and then
creates a SHA-256 transfer manifest for manual Xftp/SFTP upload.

Inventory uses parallel daily listings and an exact semantic cache. All provider
traffic is forced to `direct_only`; proxy environment variables are removed by
both the runner and Python downloader.

S3 objects use bounded 4 MiB Range requests and resume from an existing `.part`
size. Download concurrency is selectable from 1 to 16; EUMETSAT downloads are
capped at 8 concurrent workers and use the authenticated Data Store rather than
AWS. With `-AdaptiveDownload`, both providers begin with a conservative worker
count and use measured completed-byte throughput plus error rate to probe upward
or back off. The selected `-DownloadWorkers` value becomes the ceiling.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\code\geo_cloud_download\geo_ring_cloud_transfer_batch.ps1 `
  -BatchRoot "<local staging directory>" `
  -ServerRoot "/data04/1/dhr" `
  -StartDate 2024-04-02 `
  -EndDate 2024-04-30 `
  -Platforms "GOES-16,GOES-18" `
  -InventoryWorkers 8 `
  -DownloadWorkers 12 `
  -AdaptiveDownload `
  -DownloadInitialWorkers 4 `
  -DownloadMinWorkers 2 `
  -S3RangeMiB 4
```

The completed batch contains a `transfer` directory with a JSON/CSV file list,
SHA-256 values, server destination paths, a Chinese upload plan, and a status
file.  Upload the data and the standalone
`geo_ring_cloud_transfer_batch.py` verifier with Xftp.  On the server, run:

```bash
python3 geo_ring_cloud_transfer_batch.py verify \
  --manifest geo_ring_cloud_transfer_20240401_20240403_manifest.json \
  --report server_verification.json \
  --location server
```

The transfer tool intentionally has no delete command.  Local raw files may be
removed only after the server report is `PASS` and the user explicitly confirms
the deletion.

## Chinese transfer dashboard

Start the local-only dashboard for one batch:

```powershell
python .\code\geo_cloud_download\geo_ring_cloud_transfer_dashboard.py `
  --batch-root "<local batch directory>" `
  --port 8765
```

Open `http://127.0.0.1:8765`.  The page shows the seven transfer gates,
per-platform download progress, active `.part` files, disk space, Xftp
confirmation, server verification, and cleanup approval. It can also create a
new direct-only download batch with selectable platforms, worker counts, and
local destination drive. Its multi-task center keeps download, upload, and
server-verification progress visible for older batches after a new batch is
created. Disk-gate failures show the required, available, and shortfall GiB
values without truncating the original error. The confirmation actions write
audit markers only and never delete raw files.  The complete
Chinese procedure is in `geo_ring_cloud_data_transfer_operation_guide_cn.md`.

The same dashboard can start or resume a conservative automatic SFTP upload.
New batches enable continuous download-and-upload by default: a finalized file
must have unchanged size and modification time across two scans before one SFTP
worker uploads it. Files ending in `.part` are never selected. An existing
running download can be attached with the dashboard's continuous-upload button.
After download completion, the normal transfer manifest is reconciled and every
remote file receives the full SHA-256 verification.
The SSH key passphrase must be unlocked in the user's `ssh-agent`; it is never
stored by the dashboard or uploader:

```powershell
ssh-add "$env:USERPROFILE\.ssh\id_ed25519_node05"
python .\code\geo_cloud_download\geo_ring_cloud_transfer_dashboard.py `
  --batch-root "<local batch directory>" `
  --ssh-target "dhr@210.45.127.28" `
  --identity-file "$env:USERPROFILE\.ssh\id_ed25519_node05" `
  --auto-upload-root "/data04/1/dhr/geo_ring_cloud_auto_upload" `
  --port 8765
```

Remote payloads are transferred to `.part`, resumed after interruption, and
renamed only after SFTP completion. The uploader then runs the standalone
SHA-256 verifier on the server and retrieves its report. No local deletion is
implemented. Download and upload can run simultaneously on a full-duplex
network interface, but they can still contend for local disk I/O, router or
campus-link capacity, CPU hashing, and TCP acknowledgement traffic; the
continuous uploader uses one SFTP stream while downloading. Once the download
manifest is ready, upload automatically ramps through 2, 3, then at most 4
parallel SFTP streams. The dashboard shows current/maximum download and upload
worker counts together with the latest tuning reason.

On Windows, the small dashboard status file is retried when temporarily locked
by another local process; a status-write problem never terminates raw-data
transfer. The dashboard separately checks the uploader PID: a dead uploader is
shown as `STOPPED` and can be safely resumed, while a live PID with no heartbeat
for 15 minutes is shown as `STALLED` to prevent accidental duplicate uploads.

On Windows an independent notification monitor observes task transitions even
when the dashboard page is closed. It persists only state snapshots, a bounded
outbox, retry timestamps, and delivery evidence under
`_geo_ring_cloud_control/notifications`. SMTP settings may come from process
environment variables, or from the dashboard's local-only setup form. In the
latter mode the app password is encrypted by Windows DPAPI for the current user
and stored outside the Git workspace under `%LOCALAPPDATA%\GeoRingCloud`; the
state/outbox never contains credentials. The first observation establishes a quiet baseline,
so old completed tasks do not generate a notification storm. New terminal
transitions are deduplicated and failed email delivery is retried with bounded
backoff. The dashboard shows monitor health and queue counts and provides a
`Send test email` action once SMTP is configured:

```powershell
$env:GEO_RING_NOTIFY_SMTP_HOST = "<smtp host>"
$env:GEO_RING_NOTIFY_SMTP_PORT = "587"
$env:GEO_RING_NOTIFY_SMTP_USER = "<smtp user>"
$env:GEO_RING_NOTIFY_SMTP_PASSWORD = "<smtp app password>"
$env:GEO_RING_NOTIFY_EMAIL_FROM = "<sender address>"
$env:GEO_RING_NOTIFY_EMAIL_TO = "<recipient address>"
$env:GEO_RING_NOTIFY_SMTP_STARTTLS = "1"
```

For implicit TLS on port 465, set `GEO_RING_NOTIFY_SMTP_SSL=1`. Use an SMTP app
password rather than the mailbox login password. Environment variables override
the DPAPI configuration when explicitly present. The monitor state never stores
the password, sender address, recipient address, or SMTP hostname. The monitor
is a separate hidden process and dynamically reloads the current-user DPAPI
configuration without interrupting download or upload processes.
