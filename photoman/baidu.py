"""Baidu Netdisk (百度网盘) backup through the official open platform API (pan.baidu.com/union).

Setup: register an app on the open platform to get an AppKey and SecretKey, then authorize it once with the
device-code flow (the user enters a code on a Baidu page). Tokens are kept in ~/.photoman/baidu.json (mode 600)
and refreshed automatically.

Uploads go straight from the library drive to <cloud root>/<same relative path>, in chunks whose MD5s the
server confirms. Which files are uploaded is recorded in the library's index, so uploads resume where they left
off, on any day. Photoman can only upload: every request is checked against ALLOWED_CALLS and must target
the cloud root (config "cloud_root", default /apps/<app name>), so it can't delete, move or rename anything.
A re-exported edit replaces its older upload.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import config
from .importer import Index, ProgressCB, _library_files, edited_files, reconcile_library, wanted_in_cloud

OAUTH = "https://openapi.baidu.com/oauth/2.0"
API = "https://pan.baidu.com/rest/2.0/xpan"
PCS = "https://d.pcs.baidu.com/rest/2.0/pcs"
CRED_PATH = config.HOME_DIR / "baidu.json"
MB = 1024 * 1024
CHUNK_BY_VIP = {0: 4 * MB, 1: 16 * MB, 2: 32 * MB}   # regular / VIP / SVIP
REFRESH_BEFORE = 24 * 3600                          # refresh the 30-day token a day early


# The only Baidu calls Photoman may make: authorization, account info and uploading. Anything else
# (deleting, moving, renaming, listing other folders...) is refused before a request is sent.
ALLOWED_CALLS = {
    ("/oauth/2.0/device/code", None),
    ("/oauth/2.0/token", None),
    ("/rest/2.0/xpan/nas", "uinfo"),
    ("/rest/2.0/xpan/file", "precreate"),
    ("/rest/2.0/pcs/superfile2", "upload"),
    ("/rest/2.0/xpan/file", "create"),
}


class BaiduError(Exception):
    pass


class ForbiddenCall(BaiduError):
    """A call outside ALLOWED_CALLS, or a path outside the app's own upload folder."""


class BaiduAuthError(BaiduError):
    """The saved authorization no longer works; the user has to connect again."""


# ---------------------------------------------------------------- HTTP

def _multipart(fields: dict, files: dict):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, data in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{k}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _check_allowed(url: str, params: dict, data: dict) -> None:
    path = urllib.parse.urlparse(url).path
    endpoint = next((e for e, _ in ALLOWED_CALLS if path.endswith(e)), None)
    if (endpoint, (params or {}).get("method")) not in ALLOWED_CALLS:
        raise ForbiddenCall(f"Photoman only uploads to Baidu Netdisk; refusing {path} {params and params.get('method')}")
    target = (params or {}).get("path") or (data or {}).get("path")
    if target is not None:
        root = cloud_root()
        if not str(target).startswith(root + "/") or "/.." in str(target):
            raise ForbiddenCall(f"Refusing to write outside {root}/: {target}")


def cloud_root(creds: Optional[dict] = None) -> str:
    """The Baidu Netdisk folder Photoman uploads into: config "cloud_root" (e.g. /照片备份), or the app's own
    folder /apps/<app name> (shown as 我的应用数据/<app name>). Never the whole netdisk."""
    raw = (config.load_config().get("cloud_root") or "").strip()
    if raw and not raw.strip("/"):
        raise ForbiddenCall("The upload folder can't be the whole netdisk (/): use a folder such as /照片备份")
    root = raw.rstrip("/")
    if not root:
        app = (load_credentials() if creds is None else creds).get("app_name")
        if not app:
            raise ForbiddenCall("No upload folder: set up Baidu Netdisk first")
        root = f"/apps/{app}"
    if not root.startswith("/") or root == "/" or ".." in root.split("/"):
        raise ForbiddenCall(f"Invalid upload folder {root!r}: use a folder such as /照片备份")
    return root


def _http(url: str, params: dict = None, data: dict = None, files: dict = None, timeout: float = 120) -> dict:
    _check_allowed(url, params, data)
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"User-Agent": "pan.baidu.com"}
    body = None
    if files is not None:
        body, headers["Content-Type"] = _multipart(data or {}, files)
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read()
    except urllib.error.HTTPError as e:
        text = e.read()  # the OAuth endpoints answer errors with 400 + JSON
    except (urllib.error.URLError, OSError) as e:
        raise BaiduError(f"Network error: {e}") from e
    try:
        return json.loads(text)
    except ValueError:
        raise BaiduError(f"Unexpected response from Baidu: {text[:200]!r}")


# ---------------------------------------------------------------- credentials & authorization

def load_credentials() -> dict:
    try:
        return json.loads(CRED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_credentials(creds: dict) -> None:
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CRED_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    os.replace(tmp, CRED_PATH)


def _store_token(creds: dict, d: dict) -> None:
    creds["access_token"] = d["access_token"]
    creds["refresh_token"] = d.get("refresh_token", creds.get("refresh_token"))
    creds["expires_at"] = time.time() + int(d.get("expires_in", 30 * 24 * 3600))
    save_credentials(creds)


def start_device_auth(app_key: str) -> dict:
    """Step 1 of the device-code flow. Returns user_code, verification_url, qrcode_url, device_code,
    interval and expires_in; show the user_code and send the user to verification_url."""
    d = _http(OAUTH + "/device/code", {"response_type": "device_code", "client_id": app_key, "scope": "basic,netdisk"})
    if "device_code" not in d:
        raise BaiduError(d.get("error_description") or d.get("error") or f"Couldn't start authorization: {d}")
    return d


def poll_device_auth(creds: dict, device_code: str) -> bool:
    """Step 2: True once the user has authorized (tokens are saved), False while still waiting."""
    d = _http(OAUTH + "/token", {"grant_type": "device_token", "code": device_code,
                                 "client_id": creds["app_key"], "client_secret": creds["secret_key"]})
    if "access_token" in d:
        _store_token(creds, d)
        return True
    if d.get("error") in ("authorization_pending", "slow_down"):
        return False
    raise BaiduAuthError(d.get("error_description") or d.get("error") or str(d))


def is_set_up(creds: Optional[dict] = None) -> bool:
    creds = load_credentials() if creds is None else creds
    return bool(creds.get("access_token") and creds.get("app_name"))


# ---------------------------------------------------------------- client

class Baidu:
    def __init__(self, creds: Optional[dict] = None):
        self.creds = load_credentials() if creds is None else creds
        self._chunk = None

    def token(self) -> str:
        c = self.creds
        if not c.get("access_token"):
            raise BaiduAuthError("Baidu Netdisk isn't connected")
        if c.get("expires_at", 0) - time.time() < REFRESH_BEFORE:
            d = _http(OAUTH + "/token", {"grant_type": "refresh_token", "refresh_token": c.get("refresh_token", ""),
                                         "client_id": c["app_key"], "client_secret": c["secret_key"]})
            if "access_token" not in d:
                raise BaiduAuthError(d.get("error_description") or "Baidu authorization expired")
            _store_token(c, d)
        return c["access_token"]

    def _api(self, url: str, params: dict, **kw) -> dict:
        d = _http(url, {**params, "access_token": self.token()}, **kw)
        errno = d.get("errno", d.get("error_code", 0))
        if errno not in (0, None):
            if errno in (-6, 111, 110, 6):  # invalid / expired access token
                raise BaiduAuthError(f"Baidu authorization no longer valid (errno {errno})")
            raise BaiduError(f"Baidu error {errno}: {d.get('errmsg') or d.get('error_msg') or d}")
        return d

    def chunk_size(self) -> int:
        """Upload chunk size allowed for this account (4 MB regular, 16 MB VIP, 32 MB SVIP)."""
        if self._chunk is None:
            try:
                vip = int(self._api(API + "/nas", {"method": "uinfo"}).get("vip_type", 0))
            except BaiduAuthError:
                raise
            except BaiduError:
                vip = 0
            self._chunk = CHUNK_BY_VIP.get(vip, 4 * MB)
        return self._chunk

    @property
    def root(self) -> str:
        return cloud_root(self.creds)

    def remote_path(self, rel: str) -> str:
        return f"{self.root}/{rel}"

    def upload(self, local: Path, rel: str) -> dict:
        """Upload `local` to <cloud root>/<rel>, replacing any older upload there. Every chunk's MD5 is
        checked against what the server received, and the final size against the local file."""
        size = local.stat().st_size
        cs = self.chunk_size()
        md5s = []
        with open(local, "rb") as f:
            while True:
                chunk = f.read(cs)
                if not chunk and md5s:
                    break
                md5s.append(hashlib.md5(chunk).hexdigest())
                if len(chunk) < cs:
                    break
        path = self.remote_path(rel)
        block_list = json.dumps(md5s)
        pre = self._api(API + "/file", {"method": "precreate"},
                        data={"path": path, "size": size, "isdir": 0, "autoinit": 1, "rtype": 3,
                              "block_list": block_list})
        if pre.get("return_type") == 2:  # the server already has this exact file
            return pre.get("info") or {"path": path, "size": size}
        uploadid = pre["uploadid"]
        need = pre.get("block_list") or list(range(len(md5s)))
        with open(local, "rb") as f:
            for seq in need:
                f.seek(seq * cs)
                r = self._api(PCS + "/superfile2", {"method": "upload", "type": "tmpfile", "path": path,
                                                    "uploadid": uploadid, "partseq": seq},
                              files={"file": f.read(cs)})
                if r.get("md5") != md5s[seq]:
                    raise BaiduError(f"Chunk {seq} of {local.name} arrived corrupted")
        created = self._api(API + "/file", {"method": "create"},
                            data={"path": path, "size": size, "isdir": 0, "rtype": 3,
                                  "block_list": block_list, "uploadid": uploadid})
        if int(created.get("size", -1)) != size:
            raise BaiduError(f"{local.name}: Baidu stored {created.get('size')} bytes, expected {size}")
        return created


# ---------------------------------------------------------------- library uploads

@dataclass
class CloudResult:
    library_root: str
    uploaded: int = 0
    already: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)
    stopped: bool = False       # interrupted (drive disconnected, authorization lost, app quitting)

    def summary(self) -> str:
        s = f"{self.uploaded} uploaded, {self.already} already in Baidu Netdisk, {self.failed} failed"
        return s + (" (stopped early)" if self.stopped else "")


def cloud_candidates(library_root: Path, policy: str) -> list:
    """(rel, stat) of every file that belongs in the cloud: originals matching `policy`, plus all edits."""
    library_root = Path(library_root)
    out = []
    if (library_root / ".photoman" / "index.sqlite").exists():
        index = Index(library_root, readonly=True)
        try:
            for _sha, rel, exists in _library_files(index, library_root):
                if exists and wanted_in_cloud(rel, policy):
                    out.append((rel, (library_root / rel).stat()))
        finally:
            index.close()
    out += [(str(p.relative_to(library_root)), st) for p, st in edited_files(library_root)]
    return out


def _uploaded(index: Index, rel: str, st, root: str) -> bool:
    rec = index.cloud_of(rel, root)
    return bool(rec) and rec[0] == st.st_size and abs(rec[1] - st.st_mtime) <= 2


def pending_cloud(library_root: Path, policy: str) -> int:
    candidates = cloud_candidates(library_root, policy)
    if not candidates and not (Path(library_root) / ".photoman" / "index.sqlite").exists():
        return 0
    root = cloud_root()
    index = Index(Path(library_root), readonly=True)
    try:
        n = sum(1 for rel, st in candidates if not _uploaded(index, rel, st, root))
        # originals not at their recorded path (probably moved) and never uploaded: an upload run finds them
        n += sum(1 for _sha, rel, exists in _library_files(index, Path(library_root))
                 if not exists and wanted_in_cloud(rel, policy) and not index.cloud_of(rel, root))
        return n
    finally:
        index.close()


def cloud_counts(library_root: Path, policy: str) -> tuple:
    """(already uploaded, still waiting) for one destination: the whole picture, not just one upload round."""
    waiting = pending_cloud(library_root, policy)
    candidates = cloud_candidates(library_root, policy)
    if not candidates:
        return 0, waiting
    root = cloud_root()
    index = Index(Path(library_root), readonly=True)
    try:
        return sum(1 for rel, st in candidates if _uploaded(index, rel, st, root)), waiting
    finally:
        index.close()


def upload_library(library_root: Path, client: Baidu, policy: str, progress: Optional[ProgressCB] = None,
                   should_stop: Optional[Callable[[], bool]] = None) -> CloudResult:
    library_root = Path(library_root)
    res = CloudResult(library_root=str(library_root))
    reconcile_library(library_root)  # originals moved or renamed since import keep their upload record
    candidates = cloud_candidates(library_root, policy)
    if not candidates:
        return res
    root = client.root
    index = Index(library_root)
    try:
        todo = []
        for rel, st in candidates:
            if _uploaded(index, rel, st, root):
                res.already += 1
            else:
                todo.append((rel, st))
        for i, (rel, st) in enumerate(todo, 1):
            if (should_stop and should_stop()) or not library_root.is_dir():
                res.stopped = True
                break
            if progress:
                progress(i, len(todo), Path(rel).name)
            try:
                info = client.upload(library_root / rel, rel)
                index.mark_cloud(rel, root, st.st_size, st.st_mtime, str(info.get("fs_id", "")))
                index.commit()
                res.uploaded += 1
            except BaiduAuthError:
                res.stopped = True
                raise
            except (BaiduError, OSError) as e:
                res.failed += 1
                if len(res.errors) < 5:
                    res.errors.append(f"{Path(rel).name}: {e}")
    finally:
        index.close()
    return res
