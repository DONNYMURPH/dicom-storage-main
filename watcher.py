#!/usr/bin/env python3
"""Watcher side of the DICOM dropbox.

For every share under /data/shares/<share>/:
  inbox/   people drop a study folder (or loose .dcm files) here
  failed/  studies that could not be sent end up here, with FAILED_REASON.txt.
           Move a folder back into inbox/ to retry it.
  upload_log.csv  one row per patient/study that went through (or failed):
           time, patient name, patient ID (E number), accession, study date, ...

Once an item in inbox/ has stopped changing for QUIET_SECONDS it is moved to
/data/.system/processing/<share>/ (invisible to users), sent with dcmsend to CTP,
then deleted on success or moved to failed/ on error.
A combined log of all shares is kept in /data/.system/logs/upload_log_all.csv
(visible to the admin on the admin-logs share).
"""
import csv
import datetime
import os
import shutil
import signal
import subprocess
import sys
import time

import pydicom
from pydicom.errors import InvalidDicomError

ROOT = os.environ.get("DROPBOX_ROOT", "/data")
PEER = os.environ.get("PEER") or sys.exit("set PEER (CTP hostname)")
PORT = os.environ.get("PORT") or sys.exit("set PORT (CTP DICOM port)")
CALLED_AE = os.environ.get("CALLED_AE", "CTP")
CALLING_AE = os.environ.get("CALLING_AE", "DROPBOX")
POLL = int(os.environ.get("POLL_INTERVAL", "15"))
QUIET = int(os.environ.get("QUIET_SECONDS", "120"))
SEND_TIMEOUT = int(os.environ.get("SEND_TIMEOUT_SECONDS", str(6 * 3600)))
HEARTBEAT = "/tmp/heartbeat"

SHARES_DIR = os.path.join(ROOT, "shares")
PROCESSING = os.path.join(ROOT, ".system", "processing")
LOGS_DIR = os.path.join(ROOT, ".system", "logs")
ALL_LOG = os.path.join(LOGS_DIR, "upload_log_all.csv")

JUNK = {"thumbs.db", "desktop.ini", ".ds_store", "failed_reason.txt"}
LOG_FIELDS = ["time", "share", "folder", "status", "patient_name", "patient_id",
              "accession_number", "study_date", "study_description", "modalities",
              "images", "detail"]
TAGS = ["PatientName", "PatientID", "AccessionNumber", "StudyDate", "StudyDescription",
        "Modality", "StudyInstanceUID"]
LOOSE = "(loose files)"

stopping = False


def now():
    return datetime.datetime.now().astimezone()


def log(msg):
    print(f"{now().isoformat(timespec='seconds')} [watcher] {msg}", flush=True)


def beat():
    with open(HEARTBEAT, "w") as f:
        f.write(str(int(time.time())))


def is_hidden(name):
    return name.startswith(".") or name.startswith("~$") or name.lower() in JUNK \
        or name.endswith(".part") or name.endswith(".tmp")


# --------------------------------------------------------------------------- detection
def files_under(path):
    if os.path.isfile(path):
        yield path
        return
    for d, dirs, files in os.walk(path):
        for f in files:
            yield os.path.join(d, f)


def signature(paths):
    """Snapshot of (path, size, mtime) for everything in the item. None if no real files."""
    sig, real = [], False
    for p in paths:
        for f in files_under(p):
            try:
                st = os.stat(f)
            except FileNotFoundError:
                continue
            sig.append((f, st.st_size, st.st_mtime_ns))
            real = real or not is_hidden(os.path.basename(f))
    return tuple(sorted(sig)) if real else None


def inbox_items(share):
    """Yield (key, [paths]) for each unit in the inbox: one per folder, plus one for all loose files."""
    inbox = os.path.join(SHARES_DIR, share, "inbox")
    try:
        entries = sorted(os.listdir(inbox))
    except FileNotFoundError:
        return
    loose = []
    for name in entries:
        if is_hidden(name):
            continue
        p = os.path.join(inbox, name)
        if os.path.isdir(p):
            yield name, [p]
        elif os.path.isfile(p):
            loose.append(p)
    if loose:
        yield LOOSE, loose


# --------------------------------------------------------------------------- DICOM summary
def fmt_name(pn):
    """DICOM 'DOE^JOHN^M' -> 'DOE, JOHN M'"""
    parts = [p.strip() for p in str(pn or "").split("^")]
    family, given = parts[0], " ".join(p for p in parts[1:] if p)
    return f"{family}, {given}" if given else family


def fmt_date(d):
    d = str(d or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def summarize(path):
    """Return ({study_key: info}, dicom_count, other_count)."""
    studies, n_dcm, n_other = {}, 0, 0
    for f in files_under(path):
        try:
            ds = pydicom.dcmread(f, stop_before_pixels=True, specific_tags=TAGS)
        except (InvalidDicomError, OSError, ValueError, AttributeError, KeyError, TypeError):
            n_other += 1
            continue
        if "PatientID" not in ds and "StudyInstanceUID" not in ds:
            n_other += 1
            continue
        n_dcm += 1
        key = (str(ds.get("PatientID", "")), str(ds.get("StudyInstanceUID", "")))
        s = studies.setdefault(key, {
            "patient_name": fmt_name(ds.get("PatientName", "")),
            "patient_id": str(ds.get("PatientID", "")),
            "accession_number": str(ds.get("AccessionNumber", "")),
            "study_date": fmt_date(ds.get("StudyDate", "")),
            "study_description": str(ds.get("StudyDescription", "")),
            "modalities": set(), "images": 0})
        s["images"] += 1
        if ds.get("Modality"):
            s["modalities"].add(str(ds.Modality))
    return studies, n_dcm, n_other


def write_log(share, folder, status, studies, detail):
    rows = []
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    for s in (studies.values() or [{}]):
        rows.append({"time": ts, "share": share, "folder": folder, "status": status,
                     **{k: v for k, v in s.items() if k != "modalities"},
                     "modalities": " ".join(sorted(s.get("modalities", []))), "detail": detail})
    for path in (os.path.join(SHARES_DIR, share, "upload_log.csv"), ALL_LOG):
        try:
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, LOG_FIELDS)
                if new:
                    w.writeheader()
                w.writerows(rows)
        except OSError as e:
            log(f"could not write {path}: {e}")


# --------------------------------------------------------------------------- sending
def dcmsend(path, report):
    cmd = ["dcmsend", PEER, PORT, "-aet", CALLING_AE, "-aec", CALLED_AE,
           "--scan-directories", "--recurse", "--no-halt", "-ta", "60", "-td", "600",
           "--create-report-file", report, path]
    out_path = report + ".out"
    with open(out_path, "w") as out:
        p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        start = time.time()
        while p.poll() is None:  # keep the liveness heartbeat fresh during long sends
            beat()
            if time.time() - start > SEND_TIMEOUT:
                p.kill()
                p.wait()
                return False, f"dcmsend timed out after {SEND_TIMEOUT}s"
            time.sleep(5)
    with open(out_path) as f:
        output = f.read()
    rep = open(report).read() if os.path.exists(report) else ""
    os.remove(out_path)
    if os.path.exists(report):
        os.remove(report)
    ok, detail = parse_report(rep)
    if p.returncode != 0 or not ok:
        lines = output.strip().splitlines()
        errors = [l for l in lines if l.startswith(("E:", "F:"))]
        reason = errors[-1][2:].strip() if errors else f"dcmsend exit code {p.returncode}"
        tail = "\n".join(lines[-25:])
        return False, f"{detail}: {reason}\n\n--- dcmsend output ---\n{tail}\n\n{rep}".strip()
    return True, detail


def parse_report(rep):
    """dcmsend report -> (all_sent_ok, short text).
    Every image has a 'DIMSE Status  : 0x0000 (Success)' line; anything other than
    Success/Warning (or no status at all = never sent) counts as a failure."""
    total = accepted = 0
    for line in rep.splitlines():
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key == "number of sop instances":
            total = int(val.strip() or 0)
        elif key == "dimse status" and ("(Success)" in val or "(Warning)" in val):
            accepted += 1
    ok = total > 0 and accepted == total
    if total == 0:
        return False, "Nothing was delivered to CTP"
    return ok, f"{accepted} of {total} images accepted by CTP"


def unique_path(path):
    if not os.path.exists(path):
        return path
    return f"{path}_{now():%Y%m%d-%H%M%S}"


def process(share, key, paths):
    folder = key if key != LOOSE else f"loose-files_{now():%Y%m%d-%H%M%S}"
    work_parent = os.path.join(PROCESSING, share)
    os.makedirs(work_parent, exist_ok=True)
    work = unique_path(os.path.join(work_parent, folder))
    try:
        if key == LOOSE:
            os.makedirs(work)
            for p in paths:
                os.rename(p, os.path.join(work, os.path.basename(p)))
        else:
            os.rename(paths[0], work)
    except OSError as e:
        log(f"{share}: could not claim {key!r}: {e}")
        return

    for f in list(files_under(work)):
        if is_hidden(os.path.basename(f)):
            os.remove(f)

    studies, n_dcm, n_other = summarize(work)
    log(f"{share}: sending {folder!r} ({n_dcm} DICOM files, {len(studies)} studies, {n_other} non-DICOM)")
    if n_dcm == 0:
        ok, detail = False, "No DICOM files found in this folder."
    else:
        ok, detail = dcmsend(work, os.path.join(work_parent, f".report-{os.getpid()}.txt"))
    if ok and n_other:
        detail += f"; {n_other} non-DICOM file(s) ignored"

    if ok:
        shutil.rmtree(work, ignore_errors=True)
        log(f"{share}: SENT {folder!r}: {detail}")
        write_log(share, folder, "SENT", studies, detail)
    else:
        dest = unique_path(os.path.join(SHARES_DIR, share, "failed", folder))
        with open(os.path.join(work, "FAILED_REASON.txt"), "w") as f:
            f.write(f"Failed at {now():%Y-%m-%d %H:%M:%S %Z}\n"
                    f"To retry: move this folder back into inbox/ "
                    f"(this file is ignored).\n\n{detail}\n")
        os.rename(work, dest)
        short = detail.splitlines()[0] if detail else ""
        log(f"{share}: FAILED {folder!r} -> failed/{os.path.basename(dest)}: {short}")
        write_log(share, folder, "FAILED", studies, short)


def recover():
    """Anything left in processing/ (pod died mid-send) goes back into that share's inbox."""
    if not os.path.isdir(PROCESSING):
        return
    for share in os.listdir(PROCESSING):
        for name in os.listdir(os.path.join(PROCESSING, share)):
            if name.startswith("."):
                os.remove(os.path.join(PROCESSING, share, name))
                continue
            dest = unique_path(os.path.join(SHARES_DIR, share, "inbox", name))
            os.rename(os.path.join(PROCESSING, share, name), dest)
            log(f"{share}: re-queued interrupted {name!r}")


def on_term(*_):
    global stopping
    stopping = True
    log("shutdown requested; finishing current item")


def main():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    os.umask(0o007)
    os.makedirs(PROCESSING, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    recover()
    log(f"watching {SHARES_DIR}/*/inbox -> {CALLED_AE}@{PEER}:{PORT} "
        f"(poll {POLL}s, item must be unchanged for {QUIET}s)")
    seen = {}  # (share, key) -> (signature, first_time_seen_unchanged)
    while not stopping:
        beat()
        current = set()
        shares = sorted(d for d in os.listdir(SHARES_DIR)
                        if os.path.isdir(os.path.join(SHARES_DIR, d))) if os.path.isdir(SHARES_DIR) else []
        for share in shares:
            for sub in ("inbox", "failed"):
                os.makedirs(os.path.join(SHARES_DIR, share, sub), exist_ok=True)
            for key, paths in inbox_items(share):
                if stopping:
                    break
                k = (share, key)
                current.add(k)
                sig = signature(paths)
                if sig is None:
                    continue
                prev = seen.get(k)
                if prev is None or prev[0] != sig:
                    if prev is None:
                        log(f"{share}: new upload {key!r}, waiting for it to finish copying")
                    seen[k] = (sig, time.time())
                    continue
                if time.time() - prev[1] < QUIET:
                    continue
                seen.pop(k, None)
                current.discard(k)
                process(share, key, paths)
        for k in list(seen):
            if k not in current:
                del seen[k]
        for _ in range(POLL):
            if stopping:
                break
            time.sleep(1)
    log("exiting")


if __name__ == "__main__":
    main()
