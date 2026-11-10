#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端两台机器统一入口（2026-11-10 东家要求给两台机器取名）

名字：
  小算 = instabox（机器名 xiaosuan，4核8G Linux，跑脚本/算力/自动化）
  小桌 = box by Simular（实例名 main，图形桌面，真人一样操作/问 AI 用）

用法：
  python3 cloud.py list                    # 两台状态
  python3 cloud.py 小算 "<shell 命令>"      # 在小算上跑（默认超时 120s，-t 改）
  python3 cloud.py 小桌 "<shell 命令>"      # 在小桌上跑
  python3 cloud.py all "<shell 命令>"       # 两台都跑
  python3 cloud.py 小桌 shot               # 小桌截图

别名：算/小算/xiaosuan/instabox → 小算；桌/小桌/box → 小桌
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IB_BASE = "https://api.instabox.sh"
IB_MACHINE = "m_agq4bgrkr52kxwnona6pxebk7e"

NAMES = {"小算", "小桌"}
ALIAS = {
    "小算": "小算", "算": "小算", "xiaosuan": "小算", "instabox": "小算", "x1": "小算",
    "小桌": "小桌", "桌": "小桌", "box": "小桌", "main": "小桌", "x2": "小桌",
}


# ---------- 小算 ----------
def ib_key():
    k = os.environ.get("INSTABOX_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(HERE, ".ib_key")
    if os.path.exists(p):
        return open(p).read().strip()
    sys.exit("缺 instabox key：写 .ib_key 或设 INSTABOX_API_KEY")


def ib_req(method, path, body=None, timeout=60):
    r = urllib.request.Request(IB_BASE + path, method=method,
                               data=json.dumps(body).encode() if body else None)
    r.add_header("X-API-Key", ib_key())
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "null")


def run_xiaosuan(cmd, timeout=120):
    d = ib_req("POST", "/v1/machines/%s/exec" % IB_MACHINE, {"cmd": cmd}, timeout=timeout + 30)
    out = d.get("stdout") or d.get("output") or ""
    err = d.get("stderr") or ""
    print(out, end="")
    if err:
        print("[stderr]", err, end="")
    if d.get("exitCode") not in (0, None):
        print("[exit %s]" % d.get("exitCode"))


def info_xiaosuan():
    d = ib_req("GET", "/v1/machines/%s" % IB_MACHINE, timeout=45)
    return "小算  name=%s  state=%s  %dvCPU/%dMiB" % (
        d.get("name"), d.get("state"), d.get("spec", {}).get("vcpus", 0),
        d.get("spec", {}).get("memMiB", 0))


# ---------- 小桌 ----------
def box_cli(*args):
    import subprocess
    p = subprocess.run([sys.executable, os.path.join(HERE, "box_mcp.py")] + list(args),
                       capture_output=True, text=True, timeout=600)
    return p.stdout + (("\n[stderr]" + p.stderr) if p.stderr else "")


def run_xiaozhuo(cmd):
    print(box_cli("sh", cmd), end="")


def info_xiaozhuo():
    out = box_cli("status")
    return "小桌  " + " ".join(out.split())[:300]


# ---------- 入口 ----------
def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] in ("list", "ls", "状态"):
        for f in (info_xiaosuan, info_xiaozhuo):
            try:
                print(f())
            except Exception as e:
                print("%s 查询失败: %s" % (f.__name__, str(e)[:100]))
        return
    target = ALIAS.get(a[0], a[0])
    rest = a[1:]
    timeout = 120
    if rest and rest[0] == "-t":
        timeout = int(rest[1]); rest = rest[2:]
    if target == "小算":
        run_xiaosuan(" ".join(rest), timeout)
    elif target == "小桌":
        if rest and rest[0] == "shot":
            print(box_cli("shot"), end="")
        else:
            run_xiaozhuo(" ".join(rest))
    elif target in ("all", "两台", "全体"):
        cmd = " ".join(rest)
        print("=== 小算 ===")
        try:
            run_xiaosuan(cmd, timeout)
        except Exception as e:
            print("[小算失败]", str(e)[:150])
        print("=== 小桌 ===")
        try:
            run_xiaozhuo(cmd)
        except Exception as e:
            print("[小桌失败]", str(e)[:150])
    else:
        sys.exit("不认识的名字：%s（只认 小算 / 小桌 / all）" % a[0])


if __name__ == "__main__":
    main()
