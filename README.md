# DICOM dropbox

Windows/Mac network shares (SMB) where people drop DICOM studies. Each study is sent with
`dcmsend` to CTP, which forwards it to XNAT.

```
 user PC ──SMB3 (encrypted)──► samba container ──► /data (PVC) ◄── watcher container ──DICOM──► CTP ──► XNAT
```

One image (`ghcr.io/donnymurph/dicom-storage-main`) runs both containers in one pod:
`samba` (file server) and `watcher` (sender).

## What users see

Each project has its own share, e.g. `\\<server>\brain` (Mac: `smb://<server>/brain`):

| path | what it is |
|---|---|
| `inbox/` | Drop a study folder (or loose `.dcm` files) here. It is sent once nothing in it has changed for 2 minutes, then removed. |
| `failed/` | Anything that could not be sent, with `FAILED_REASON.txt`. To retry, move the folder back into `inbox/`. |
| `upload_log.csv` | One row per study: time, status (SENT/FAILED), **patient name, patient ID (E number)**, accession, study date, description, modality, image count. |

People only see and open the shares they are assigned to. Admin users (marked `*`)
can open every share, plus a read-only `admin-logs` share.

## Admin tasks (all in Rancher, no restart needed, applies in ~1 min)

* **New share:** Storage → ConfigMaps → `dicom-dropbox-shares` → Edit → add a line (e.g. `cardiac`).
* **New user / change access / change password:** Storage → Secrets → `dicom-dropbox-users` → Edit `users.conf`:
  ```
  username:password:share1,share2      # password >= 12 chars
  adminname:password:*                 # * = admin, all shares
  ```
  Removing a user or changing a password immediately disconnects their open sessions.

## Logs (HIPAA)

All logs are on the volume under `/data/.system/logs/`, which the admin sees as the `admin-logs` share:

| file | contents |
|---|---|
| `audit/access-YYYY-MM-DD.log` | Every connect, disconnect, file open, rename, delete and mkdir: `user\|client IP\|client name\|share\|action\|ok/fail\|path` |
| `audit/smbd-YYYY-MM-DD.log` | Logins (successful and failed, with user and IP) and server events |
| `upload_log_all.csv` | All shares' upload logs combined. This is the authoritative copy: users can edit the per-share copy. |

Logs are never deleted automatically. Back up `admin-logs` periodically.
HIPAA documentation retention is 6 years.

## Security settings

* SMB3 only, **encryption required** (AES-GCM). SMB1, guest and anonymous access are off, and so is NTLMv1.
* Each share has an explicit `valid users` list, and users can't see shares they can't open.
* Files on disk are owned by a service account (uid 1000). The real uploader is recorded in the access log.
* Samba runs without `privileged`, with a minimal capability set. The watcher runs as non-root with no capabilities.
* **Not handled by this image:**
  * Encryption at rest depends on the storage class or node disks (e.g. a Longhorn encrypted StorageClass, or LUKS).
  * The watcher→CTP DICOM traffic stays inside the cluster network and is not TLS.

## Files

| file | purpose |
|---|---|
| `dockerfile`, `entrypoint.sh` | the image; `entrypoint.sh samba` or `entrypoint.sh watcher` |
| `samba_server.py` | builds `smb.conf` and Samba users from the ConfigMap/Secret, runs smbd, writes audit logs, live-reloads config |
| `watcher.py` | scans every `shares/*/inbox`, sends to CTP, writes `upload_log.csv`, moves failures to `failed/` |

The Kubernetes manifests (`k8s/`) and the users Secret are kept **out of git** on purpose:
they contain cluster details and passwords.

Volume layout:

```
/data/shares/<share>/{inbox,failed,upload_log.csv}
/data/.system/processing/<share>/  (being sent; re-queued to inbox automatically if the pod restarts mid-send)
/data/.system/logs/                (audit + combined upload logs)
```
