"""Baidu Netdisk uploads against a local fake of the open platform API."""
import hashlib
import json
import os
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from photoman import baidu
from photoman.importer import run_import


class FakeBaidu(BaseHTTPRequestHandler):
    """Implements just enough of openapi.baidu.com / pan.baidu.com / d.pcs.baidu.com."""
    state = None

    def log_message(self, *a):
        pass

    def _reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_any(b"")

    def do_POST(self):
        self.handle_any(self.rfile.read(int(self.headers.get("Content-Length", 0))))

    def handle_any(self, body):
        st = self.state
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        st["calls"].append((url.path, q.get("method") or q.get("grant_type")))
        if url.path.endswith("/device/code"):
            return self._reply({"device_code": "DEV", "user_code": "ABCD", "verification_url": "https://x/verify",
                                "qrcode_url": "https://x/qr", "expires_in": 300, "interval": 5})
        if url.path.endswith("/token"):
            if q["grant_type"] == "device_token":
                st["polls"] += 1
                if st["polls"] < 2:
                    return self._reply({"error": "authorization_pending"}, 400)
                return self._reply({"access_token": "TOKEN1", "refresh_token": "R1", "expires_in": 2592000})
            if q["grant_type"] == "refresh_token":
                return self._reply({"access_token": "TOKEN2", "refresh_token": "R2", "expires_in": 2592000})
        if q.get("access_token") not in ("TOKEN1", "TOKEN2"):
            return self._reply({"errno": -6})
        if url.path.endswith("/nas"):
            return self._reply({"errno": 0, "vip_type": 0})
        if url.path.endswith("/file") and q["method"] == "precreate":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            uid = f"U{len(st['uploads'])}"
            st["uploads"][uid] = {"path": form["path"], "chunks": {}}
            n = len(json.loads(form["block_list"]))
            return self._reply({"errno": 0, "return_type": 1, "uploadid": uid, "block_list": list(range(n))})
        if url.path.endswith("/superfile2"):
            boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
            part = body.split(b"--" + boundary)[1]
            data = part.split(b"\r\n\r\n", 1)[1][:-2]
            st["uploads"][q["uploadid"]]["chunks"][int(q["partseq"])] = data
            md5 = hashlib.md5(data).hexdigest()
            return self._reply({"md5": "0" * 32 if st.get("corrupt") else md5})
        if url.path.endswith("/file") and q["method"] == "create":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            up = st["uploads"][form["uploadid"]]
            chunks = [up["chunks"][i] for i in range(len(up["chunks"]))]
            assert [hashlib.md5(c).hexdigest() for c in chunks] == json.loads(form["block_list"])
            content = b"".join(chunks)
            st["files"][form["path"]] = content
            return self._reply({"errno": 0, "fs_id": 42, "path": form["path"], "size": len(content)})
        return self._reply({"errno": 31023, "errmsg": "unknown"}, 404)


@pytest.fixture
def server(monkeypatch, tmp_path):
    state = {"calls": [], "polls": 0, "uploads": {}, "files": {}}
    FakeBaidu.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeBaidu)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(baidu, "OAUTH", base + "/oauth/2.0")
    monkeypatch.setattr(baidu, "API", base + "/rest/2.0/xpan")
    monkeypatch.setattr(baidu, "PCS", base + "/rest/2.0/pcs")
    monkeypatch.setattr(baidu, "CRED_PATH", tmp_path / "home" / "baidu.json")
    monkeypatch.setattr(baidu, "CHUNK_BY_VIP", {0: 1024})   # small chunks so test files span several
    yield state
    httpd.shutdown()


@pytest.fixture
def client(server):
    creds = {"app_key": "AK", "secret_key": "SK", "app_name": "Photoman"}
    info = baidu.start_device_auth("AK")
    assert info["user_code"] == "ABCD"
    assert baidu.poll_device_auth(creds, info["device_code"]) is False     # user hasn't approved yet
    assert baidu.poll_device_auth(creds, info["device_code"]) is True
    return baidu.Baidu(baidu.load_credentials())


def _file(path: Path, content: bytes, when=datetime(2026, 9, 5, 10), age=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    t = time.time() - age if age else when.timestamp()
    os.utime(path, (t, t))


def test_device_auth_saves_private_credentials(client, tmp_path):
    creds = baidu.load_credentials()
    assert creds["access_token"] == "TOKEN1" and creds["refresh_token"] == "R1"
    assert oct(baidu.CRED_PATH.stat().st_mode & 0o777) == "0o600"
    assert baidu.is_set_up(creds)


def test_upload_in_verified_chunks(client, server, tmp_path):
    f = tmp_path / "big.jpg"
    content = os.urandom(3500)                  # 4 chunks of 1024
    f.write_bytes(content)
    info = client.upload(f, "2026/2026-09-05_Rome/big.jpg")
    assert info["size"] == 3500
    assert server["files"]["/apps/Photoman/2026/2026-09-05_Rome/big.jpg"] == content
    assert len(next(iter(server["uploads"].values()))["chunks"]) == 4


def test_corrupted_chunk_is_an_error(client, server, tmp_path):
    server["corrupt"] = True
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 100)
    with pytest.raises(baidu.BaiduError, match="corrupted"):
        client.upload(f, "a.jpg")


def test_expiring_token_is_refreshed(client, server):
    client.creds["expires_at"] = time.time() + 60
    assert client.token() == "TOKEN2"
    assert baidu.load_credentials()["refresh_token"] == "R2"


def test_upload_library_sends_jpegs_and_edits_and_resumes(client, server, tmp_path):
    card = tmp_path / "NIKON" / "DCIM" / "100"
    for name, data in (("DSC_0001.NEF", b"raw1" * 500), ("DSC_0001.JPG", b"jpg1" * 400),
                       ("DSC_0002.JPG", b"jpg2" * 10), ("DSC_0003.MOV", b"video")):
        _file(card / name, data)
    lib = tmp_path / "PhotoA"
    lib.mkdir()
    res = run_import(tmp_path / "NIKON", lib, "Rome", [".nef", ".jpg", ".mov"])
    edited = Path(res.edited_folder)
    _file(edited / "DSC_0001-edit.jpg", b"edit v1", age=3600)
    _file(edited / "Web" / "small.jpg", b"web", age=3600)

    assert baidu.pending_cloud(lib, "jpeg") == 4       # 2 JPEG originals + 2 edits (no RAW, no video)
    r = baidu.upload_library(lib, client, "jpeg")
    assert (r.uploaded, r.failed) == (4, 0)
    names = sorted(Path(p).name for p in server["files"])
    assert names == ["DSC_0001-edit.jpg", "DSC_0001.JPG", "DSC_0002.JPG", "small.jpg"]
    assert "/apps/Photoman/2026/2026-09-05_Rome_Edited/Web/small.jpg" in server["files"]

    # nothing left to do, even with a new client (state lives in the library's index)
    assert baidu.pending_cloud(lib, "jpeg") == 0
    assert baidu.upload_library(lib, baidu.Baidu(baidu.load_credentials()), "jpeg").already == 4

    # a re-exported edit is uploaded again
    _file(edited / "DSC_0001-edit.jpg", b"edit v2 longer", age=1800)
    r = baidu.upload_library(lib, client, "jpeg")
    assert r.uploaded == 1
    assert server["files"]["/apps/Photoman/2026/2026-09-05_Rome_Edited/DSC_0001-edit.jpg"] == b"edit v2 longer"

    # switching the policy to "all" picks up the RAW and the video
    assert baidu.pending_cloud(lib, "all") == 2


def test_lost_authorization_stops_uploads(client, server, tmp_path):
    lib = tmp_path / "PhotoA"
    _file(tmp_path / "NIKON" / "DCIM" / "100" / "A.JPG", b"a")
    lib.mkdir()
    run_import(tmp_path / "NIKON", lib, "", [".jpg"])
    client.creds["access_token"] = "REVOKED"
    client.creds["expires_at"] = time.time() + 10 * 86400
    with pytest.raises(baidu.BaiduAuthError):
        baidu.upload_library(lib, client, "jpeg")
    assert baidu.pending_cloud(lib, "jpeg") == 1
