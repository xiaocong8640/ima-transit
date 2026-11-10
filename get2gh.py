#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云机侧：下载 URL → 上传 GitHub Release → 打印本地可拉直链
在云机上运行（带宽好），产物在 GitHub，本地用 IDM 拉满速。

用法：
  python3 get2gh.py <url|磁力> [--name 文件名] [--dir /data/dl]
输出最后一行：LOCAL_URL=<gh-proxy 直链>
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime

REPO = os.environ.get("GH_REPO", "xiaocong8640/ima-transit")
PROXY = "https://gh-proxy.com/"
TOKEN_FILES = ["/data/.gh_token", os.path.expanduser("~/.gh_token")]


def token():
    t = os.environ.get("GH_TOKEN", "").strip()
    if t:
        return t
    for p in TOKEN_FILES:
        if os.path.exists(p):
            return open(p).read().strip()
    sys.exit("缺 GitHub token")


def gh(method, path, body=None, raw=None, ctype="application/json"):
    url = "https://api.github.com" + path
    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "token " + token())
    r.add_header("Content-Type", ctype)
    r.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read().decode() or "null")


def ensure_release(tag):
    try:
        return gh("GET", "/repos/%s/releases/tags/%s" % (REPO, tag))
    except Exception:
        return gh("POST", "/repos/%s/releases" % REPO, {
            "tag_name": tag, "name": tag, "body": "auto files %s" % tag})


def upload(release, path):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    q = urllib.parse.quote(name)
    for a in release.get("assets", []):
        if a.get("name") == name:
            print("已存在同名资产，删除旧的:", name)
            gh("DELETE", "/repos/%s/releases/assets/%d" % (REPO, a["id"]))
    up = "https://uploads.github.com/repos/%s/releases/%d/assets?name=%s" % (
        REPO, release["id"], q)
    r = urllib.request.Request(up, data=open(path, "rb"), method="POST")
    r.add_header("Authorization", "token " + token())
    r.add_header("Content-Type", "application/octet-stream")
    r.add_header("Content-Length", str(size))
    with urllib.request.urlopen(r, timeout=3600) as resp:
        return json.loads(resp.read().decode())


def download(url, outdir, name=None):
    os.makedirs(outdir, exist_ok=True)
    if url.startswith("magnet:"):
        subprocess.run(["aria2c", "-d", outdir, "--seed-time=0", url], check=True)
        files = sorted(os.listdir(outdir), key=lambda f: -os.path.getsize(os.path.join(outdir, f)))
        return os.path.join(outdir, files[0])
    name = name or os.path.basename(urllib.parse.urlparse(url).path) or "file.bin"
    out = os.path.join(outdir, name)
    # 优先 aria2c 多线程，失败退回 curl
    try:
        subprocess.run(["aria2c", "-x", "16", "-s", "16", "-k", "1M", "-d", outdir, "-o", name, url],
                       check=True)
    except Exception:
        subprocess.run(["curl", "-L", "--retry", "3", "-o", out, url], check=True)
    return out


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    url = a[0]
    name = None
    outdir = "/data/dl"
    i = 1
    while i < len(a):
        if a[i] == "--name":
            name = a[i + 1]; i += 2
        elif a[i] == "--dir":
            outdir = a[i + 1]; i += 2
        else:
            i += 1
    print("[1/3] 下载:", url)
    path = download(url, outdir, name)
    size = os.path.getsize(path)
    print("[2/3] 完成 %s (%.1f MB)" % (path, size / 1048576))
    tag = "files-" + datetime.now().strftime("%Y%m%d")
    rel = ensure_release(tag)
    print("[3/3] 上传 Release:", tag)
    asset = upload(rel, path)
    local_url = PROXY + asset["browser_download_url"]
    print("RAW_URL=" + asset["browser_download_url"])
    print("LOCAL_URL=" + local_url)


if __name__ == "__main__":
    main()
