#!/usr/bin/env python3
"""SMB side of the DICOM dropbox.

Reads two files (mounted from Kubernetes):
  SHARES_FILE  (ConfigMap) one share name per line, e.g. brain / prostate / umbrella
  USERS_FILE   (Secret)    username:password:share1,share2   ('*' = admin, every share)

and from them:
  * writes /etc/samba/smb.conf (SMB3 + encryption required, per-share valid users,
    full_audit access logging)
  * creates/updates/removes the Samba accounts
  * creates /data/shares/<share>/{inbox,failed}
  * runs smbd
  * receives smbd's syslog output and writes daily HIPAA audit logs to
    /data/.system/logs/audit/ (access-YYYY-MM-DD.log, smbd-YYYY-MM-DD.log)
  * re-checks the two files every CONFIG_CHECK_SECONDS and applies changes
    without a pod restart (Kubernetes updates mounted ConfigMaps/Secrets in ~1 min)
"""
import datetime
import grp
import hashlib
import os
import pwd
import re
import signal
import socket
import subprocess
import sys
import threading
import time

DATA = os.environ.get("DROPBOX_ROOT", "/data")
SHARES_FILE = os.environ.get("SHARES_FILE", "/config/shares.conf")
USERS_FILE = os.environ.get("USERS_FILE", "/secrets/users.conf")
ADMIN_LOG_SHARE = os.environ.get("ADMIN_LOG_SHARE", "admin-logs")
NETBIOS_NAME = os.environ.get("NETBIOS_NAME", "DICOMDROPBOX")
CHECK_SECONDS = int(os.environ.get("CONFIG_CHECK_SECONDS", "30"))
AUDIT_STDOUT = os.environ.get("AUDIT_STDOUT", "0") == "1"  # file-access lines contain file names (may be PHI)

SHARES_DIR = os.path.join(DATA, "shares")
SYSTEM_DIR = os.path.join(DATA, ".system")
LOGS_DIR = os.path.join(SYSTEM_DIR, "logs")
AUDIT_DIR = os.path.join(LOGS_DIR, "audit")
SMB_CONF = "/etc/samba/smb.conf"
SYSLOG_SOCKET = "/dev/log"

DROPBOX_USER = "dropbox"
DROPBOX_UID = DROPBOX_GID = 1000
LOCAL5 = 21  # syslog facility used for full_audit lines

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
RESERVED = {"root", "dropbox", "nobody", "global", "homes", "printers", "ipc$", ADMIN_LOG_SHARE}


def log(msg):
    print(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} [samba] {msg}", flush=True)


# --------------------------------------------------------------------------- audit log receiver
class AuditLogger(threading.Thread):
    """Tiny syslog receiver on /dev/log. smbd logs auth events and full_audit
    file-access events to syslog; we persist them as daily files on the PVC."""

    PRI_RE = re.compile(rb"^<(\d+)>")
    HEADER_RE = re.compile(r"^[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d ")

    def __init__(self):
        super().__init__(daemon=True)
        if os.path.exists(SYSLOG_SOCKET):
            os.unlink(SYSLOG_SOCKET)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(SYSLOG_SOCKET)
        os.chmod(SYSLOG_SOCKET, 0o666)
        self.lock = threading.Lock()

    def write(self, kind, line):
        now = datetime.datetime.now().astimezone()
        path = os.path.join(AUDIT_DIR, f"{kind}-{now:%Y-%m-%d}.log")
        with self.lock:
            new = not os.path.exists(path)
            with open(path, "a") as f:
                f.write(f"{now.isoformat(timespec='seconds')} {line}\n")
            if new:
                os.chown(path, 0, DROPBOX_GID)
                os.chmod(path, 0o640)

    def run(self):
        while True:
            data = self.sock.recv(65536)
            m = self.PRI_RE.match(data)
            facility = int(m.group(1)) >> 3 if m else 3
            text = data[m.end():] if m else data
            text = text.decode("utf-8", "replace").strip()
            text = self.HEADER_RE.sub("", text)  # drop syslog's own timestamp
            if not text:
                continue
            if facility == LOCAL5:
                # format: smbd_audit: user|client_ip|client_name|share|operation|ok/fail|args
                self.write("access", text)
                if AUDIT_STDOUT:
                    log("AUDIT " + text)
            else:
                self.write("smbd", text)
                log(text)


# --------------------------------------------------------------------------- config parsing
def read_lines(path):
    with open(path) as f:
        return f.read().splitlines()


def read_config():
    """Return (shares, users) where users = {name: (password, set_of_shares, is_admin)}."""
    shares = []
    for raw in read_lines(SHARES_FILE):
        name = raw.split("#", 1)[0].strip().lower()
        if not name:
            continue
        if not NAME_RE.match(name) or name in RESERVED:
            log(f"WARNING: ignoring invalid share name {name!r} (use lowercase letters, digits, - or _)")
            continue
        if name not in shares:
            shares.append(name)

    users = {}
    for n, raw in enumerate(read_lines(USERS_FILE), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        name, _, rest = raw.partition(":")
        password, _, share_field = rest.rpartition(":")  # password may itself contain ':'
        name = name.strip().lower()
        share_field = share_field.strip()
        if not NAME_RE.match(name) or name in RESERVED:
            log(f"WARNING: users file line {n}: invalid username {name!r}, skipped")
            continue
        if len(password) < 12:
            log(f"WARNING: users file line {n}: password for {name} shorter than 12 characters, skipped")
            continue
        if share_field == "*":
            users[name] = (password, set(shares), True)
            continue
        wanted = {s.strip().lower() for s in share_field.split(",") if s.strip()}
        unknown = wanted - set(shares)
        if unknown:
            log(f"WARNING: users file line {n}: {name} lists unknown share(s) {sorted(unknown)} (not in shares.conf)")
        users[name] = (password, wanted & set(shares), False)
    return shares, users


def config_hash():
    h = hashlib.sha256()
    for p in (SHARES_FILE, USERS_FILE):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            h.update(b"<missing>")
    return h.hexdigest()


# --------------------------------------------------------------------------- smb.conf
GLOBAL = f"""[global]
   server role = standalone server
   workgroup = WORKGROUP
   netbios name = {NETBIOS_NAME}
   server string = DICOM dropbox
   security = user
   passdb backend = tdbsam
   idmap config * : backend = tdb
   idmap config * : range = 3000-7999
   map to guest = never
   restrict anonymous = 2
   usershare max shares = 0

   # ---- encryption in transit (HIPAA): SMB3 only, encryption mandatory
   server min protocol = SMB3_00
   server smb encrypt = required
   server signing = mandatory
   ntlm auth = ntlmv2-only

   disable netbios = yes
   smb ports = 445
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

   # users only see the shares they are allowed into
   access based share enum = yes

   # ---- audit logging (HIPAA): logins + every file open/rename/delete, with user + IP
   logging = syslog@3
   log level = 1 auth_audit:3
   vfs objects = full_audit
   full_audit:prefix = %U|%I|%m|%S
   full_audit:success = connect disconnect openat renameat unlinkat mkdirat
   full_audit:failure = connect openat renameat unlinkat mkdirat
   full_audit:facility = LOCAL5
   full_audit:priority = NOTICE

   # mac / windows junk files never land on the share
   veto files = /._*/.DS_Store/Thumbs.db/desktop.ini/.TemporaryItems/
   delete veto files = yes
"""

SHARE_TEMPLATE = """
[{name}]
   path = {path}
   comment = {comment}
   valid users = {users}
   read only = {read_only}
   browseable = yes
   guest ok = no
   force user = {force_user}
   force group = {force_user}
   create mask = 0660
   directory mask = 2770
   force directory mode = 2770
"""


def build_smb_conf(shares, users):
    admins = sorted(u for u, (_, _, adm) in users.items() if adm)
    out = [GLOBAL]
    for s in shares:
        allowed = sorted(set(admins) | {u for u, (_, sh, _) in users.items() if s in sh})
        block = SHARE_TEMPLATE.format(
            name=s, path=os.path.join(SHARES_DIR, s), comment=f"DICOM dropbox - {s}",
            users=" ".join(allowed) or "nobody", read_only="no", force_user=DROPBOX_USER)
        if not allowed:
            block += "   available = no\n"
        out.append(block)
    if admins:
        out.append(SHARE_TEMPLATE.format(
            name=ADMIN_LOG_SHARE, path=LOGS_DIR, comment="Audit + upload logs (admin only)",
            users=" ".join(admins), read_only="yes", force_user=DROPBOX_USER))
    return "".join(out)


# --------------------------------------------------------------------------- filesystem
def ensure_dir(path, mode, uid=DROPBOX_UID, gid=DROPBOX_GID):
    os.makedirs(path, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def ensure_dirs(shares):
    ensure_dir(SHARES_DIR, 0o2750)
    ensure_dir(SYSTEM_DIR, 0o2750)
    ensure_dir(LOGS_DIR, 0o2750)
    ensure_dir(AUDIT_DIR, 0o2750, uid=0)  # only root (samba) writes, admin share can read
    ensure_dir(os.path.join(SYSTEM_DIR, "processing"), 0o2770)
    for s in shares:
        for sub in ("", "inbox", "failed"):
            ensure_dir(os.path.join(SHARES_DIR, s, sub), 0o2770)


# --------------------------------------------------------------------------- accounts
def run(cmd, stdin=None):
    return subprocess.run(cmd, input=stdin, text=True, capture_output=True)


def samba_users():
    r = run(["pdbedit", "-L"])
    return {line.split(":", 1)[0] for line in r.stdout.splitlines() if ":" in line}


def sync_users(users):
    for name, (password, _, _) in users.items():
        try:
            pwd.getpwnam(name)
        except KeyError:
            run(["useradd", "-M", "-N", "-g", DROPBOX_USER, "-d", "/nonexistent",
                 "-s", "/usr/sbin/nologin", name])
        r = run(["smbpasswd", "-s", "-a", name], stdin=f"{password}\n{password}\n")
        if r.returncode != 0:
            log(f"ERROR setting samba password for {name}: {r.stderr.strip()}")
    for name in samba_users() - set(users):
        run(["smbpasswd", "-x", name])
        run(["userdel", name])
        log(f"removed user {name}")


# --------------------------------------------------------------------------- main
class Server:
    def __init__(self):
        self.smbd = None
        self.applied = None  # (hash, access_pairs, passwords)
        self.stopping = False

    def apply(self):
        h = config_hash()
        shares, users = read_config()
        ensure_dirs(shares)
        conf = build_smb_conf(shares, users)
        with open(SMB_CONF, "w") as f:
            f.write(conf)
        r = run(["testparm", "-s", SMB_CONF])
        if r.returncode != 0:
            log("ERROR: generated smb.conf failed testparm:\n" + r.stderr)
        sync_users(users)
        pairs = {(u, s) for u, (_, sh, _) in users.items() for s in sh}
        pws = {u: p for u, (p, _, _) in users.items()}
        prev = self.applied
        self.applied = (h, pairs, pws)
        summary = ", ".join(
            f"{s}=[{' '.join(sorted(u for u, (_, sh, _) in users.items() if s in sh))}]" for s in shares)
        log(f"config applied: shares {summary}; admins {sorted(u for u, v in users.items() if v[2])}")
        # Access removed or password changed -> restart smbd so existing sessions are cut off.
        revoked = prev is not None and (
            bool(prev[1] - pairs) or any(pws.get(u) != p for u, p in prev[2].items()))
        return revoked

    def start_smbd(self):
        # own session: when smbd stops it signals its whole process group, which must not include us
        self.smbd = subprocess.Popen(["smbd", "--foreground", "--no-process-group"], start_new_session=True)
        log(f"smbd started (pid {self.smbd.pid})")

    def stop_smbd(self):
        if self.smbd and self.smbd.poll() is None:
            self.smbd.terminate()
            try:
                self.smbd.wait(20)
            except subprocess.TimeoutExpired:
                self.smbd.kill()

    def on_term(self, *_):
        self.stopping = True

    def main(self):
        signal.signal(signal.SIGTERM, self.on_term)
        signal.signal(signal.SIGINT, self.on_term)
        os.makedirs(AUDIT_DIR, exist_ok=True)
        AuditLogger().start()
        self.apply()
        self.start_smbd()
        last_check = time.time()
        while not self.stopping:
            time.sleep(1)
            if self.smbd.poll() is not None:
                log(f"smbd exited with code {self.smbd.returncode}; exiting so Kubernetes restarts the pod")
                sys.exit(1)
            if time.time() - last_check < CHECK_SECONDS:
                continue
            last_check = time.time()
            if config_hash() == self.applied[0]:
                continue
            log("shares/users config changed, applying")
            try:
                revoked = self.apply()
            except Exception as e:  # keep serving with the old config
                log(f"ERROR applying new config: {e}")
                continue
            if revoked:
                log("access was removed or a password changed: restarting smbd to drop old sessions")
                self.stop_smbd()
                self.start_smbd()
            else:
                run(["smbcontrol", "smbd", "reload-config"])
        log("shutting down")
        self.stop_smbd()


if __name__ == "__main__":
    Server().main()
