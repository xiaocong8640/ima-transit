#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云机 → 东家电脑 的文件转运（走 GitHub Release，国内下得动）

为什么用 GitHub Release：
  filebin/gofile 这类境外临时站，国内实测 11 KB/s；腾讯云 COS 上传要跨太平洋，
  实测 4 KB/s —— 瓶颈是跨境，不是站点。
  GitHub Release 两点都还行：云机（美国）上传 ~2 MB/s，东家本地下载通常
  200 KB~2 MB/s，且支持断点续传、私有仓库只有自己看得到。

用法：
  python3 transit.py <url 或 云机文件路径> [--name 文件名]

完成后输出一条 https://github.com/.../releases/download/... 链接，东家用浏览器点它。
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import box_mcp  # noqa: E402

REPO = "xiaocong8640/ima-transit"
DL = "/workspace/dl"
TOKEN = os.environ.get("GH_TOKEN", "")


def gh(method, path, data=None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(data).encode() if data else None)
    req.add_header("Authorization", "token " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "null")


def ensure_release():
    tag = "files-%s" % time.strftime("%Y%m%d")
    rels = gh("GET", "/repos/%s/releases" % REPO)
    for r in rels:
        if r["tag_name"] == tag:
            return r["id"], tag
    r = gh("POST", "/repos/%s/releases" % REPO, {"tag_name": tag, "name": "云机取回 " + tag[6:], "body": "自动创建"})
    return r["id"], tag


def box(cmd, timeout=300):
    return box_mcp.call("shell", {"command": cmd}, timeout=timeout)["text"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    if not TOKEN:
        sys.exit("需要环境变量 GH_TOKEN")

    name = a.name
    if a.src.startswith("http"):
        name = name or os.path.basename(a.src.split("?")[0]) or "download.bin"
        print("① 云机下载:", a.src)
        box("mkdir -p %s" % DL, 60)
        r = box("cd %s && curl -sL --retry 2 -m 3600 -o %s '%s' && stat -c %%s %s" % (DL, name, a.src, name), 900)
        print("   ", r.strip().splitlines()[0][:120])
        path = "%s/%s" % (DL, name)
    else:
        path = a.src
        name = name or os.path.basename(path)

    out = box("stat -c %s " + path, 90)
    m = re.search(r"\b(\d{3,})\b", out or "")
    size = int(m.group(1)) if m else 0
    if not size:
        sys.exit("云机上没有这个文件: " + path)
    print("   文件 %s  %.2f MB" % (name, size / 1048576))

    rid, tag = ensure_release()
    print("② 上传到 GitHub Release", tag)

    b64 = base64.b64encode(TOKEN.encode()).decode()
    cmd = ("echo %s | base64 -d > /tmp/.gh && chmod 600 /tmp/.gh && cd %s && "
           "nohup bash -c 'curl -s -X POST -H \"Authorization: token $(cat /tmp/.gh)\" "
           "-H \"Content-Type: application/octet-stream\" --data-binary @%s "
           "\"https://uploads.github.com/repos/%s/releases/%s/assets?name=%s\" > /tmp/ghup.json 2>&1; rm -f /tmp/.gh' "
           "> /dev/null 2>&1 & echo STARTED") % (b64, DL, name, REPO, rid, name)
    box(cmd, 120)

    url = None
    for i in range(60):
        time.sleep(6)
        assets = gh("GET", "/repos/%s/releases/%s/assets" % (REPO, rid))
        for aset in assets:
            if aset["name"] == name and aset["state"] == "uploaded":
                url = aset["browser_download_url"]
                break
        if url:
            break
        print("   上传中… %ds" % ((i + 1) * 6))
    if not url:
        sys.exit("上传超时，稍后用 gh release 页面确认")
    print("③ 直链（GitHub 原站）：")
    print(url)
    print("④ 国内加速镜像（点开即下，走国内节点）：")
    for p in ("https://gh-proxy.com/", "https://ghproxy.net/"):
        print(p + url)
    box("rm -f %s/%s" % (DL, name), 60)


if __name__ == "__main__":
    main()
