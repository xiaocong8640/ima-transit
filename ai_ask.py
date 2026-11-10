#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_ask.py —— 用云机上的真实浏览器问各家 AI，抓回完整回答

为什么是浏览器不是 API：网页版不花钱，API 按 token 计费。

用法：
  python3 ai_ask.py list                         列出支持的平台
  python3 ai_ask.py kimi "你的问题"               问 Kimi
  python3 ai_ask.py doubao "问题" -o /data/a.json  指定输出
  python3 ai_ask.py deepseek @/data/prompt.txt    从文件读问题
  python3 ai_ask.py all @/data/prompt.txt         依次问全部平台

平台：kimi / doubao / zhipu / deepseek / yuanbao

关键设计（都是踩坑换来的）：
  1. profile 用【副本】：/data/prof-copy。东家的图形浏览器占着原目录，
     两个实例抢同一 profile 会互相干扰。副本才能继承登录态。
     → 东家新登录了平台时，跑一次 --sync 重新复制。
  2. 输入必须用 CDP 的 Input.insertText：直接改 textarea.value 再派发
     input 事件，React 不认，发送按钮还是禁用状态。
  3. 发送：有的平台点按钮，有的只能 Enter；DeepSeek 的 Enter 要带 text="\\r"。
  4. 轮询判完成：正文长度连续 8 次不变且 >2000 字符，才算答完。
     只看"长度不变"会被"正在思考…"的静态文字骗过去。

依赖：chromium（云机自带）、python3-websocket-client
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

try:
    import websocket
except ImportError:
    sys.exit("缺 websocket-client：pip3 install --break-system-packages websocket-client")

# ---------------------------------------------------------------- 平台配置

BASE_PROFILE = os.environ.get("AI_ASK_BASE", "/data/home/.config/chromium")   # 东家图形浏览器用的目录
COPY_PROFILE = os.environ.get("AI_ASK_COPY", "/data/prof-copy")               # 我们用的副本
PORT = int(os.environ.get("AI_ASK_PORT", "9222"))
CHROME = os.environ.get("AI_ASK_CHROME", "chromium")                          # box 上是 google-chrome

PLATFORMS = {
    "kimi": {
        "url": "https://www.kimi.com",
        "input": 'div.chat-input-editor[contenteditable="true"]',
        "send": "enter",          # 没有稳定的发送按钮，Enter 最可靠
        "new_chat": None,
    },
    "doubao": {
        "url": "https://www.doubao.com/chat/",
        "input": 'div[contenteditable="true"]',
        "send": "send-div",       # 豆包的发送键是 div.send-button-container，不是 button
        "new_chat": "新对话",
    },
    "zhipu": {
        "url": "https://chatglm.cn",
        "input": "textarea",
        "send": "send-div",
        "new_chat": "新对话",
    },
    "deepseek": {
        "url": "https://chat.deepseek.com",
        "input": "textarea",
        "send": "enter_cr",       # 必须带 text="\r" 才触发发送
        "new_chat": "New chat",
    },
    "yuanbao": {
        "url": "https://yuanbao.tencent.com/chat",
        "input": "div.ql-editor",
        "send": "send-div",
        "new_chat": None,
    },
    "chatgpt": {
        "url": "https://chatgpt.com/",
        "input": "#prompt-textarea, div[contenteditable=\"true\"]",
        "send": "send-button",    # button[data-testid="send-button"]
        "new_chat": None,
    },
}

# ---------------------------------------------------------------- 浏览器控制


class Browser:
    def __init__(self, profile, width=1440, height=1150):
        self.profile = profile
        self.width = width
        self.height = height
        self.ws = None
        self._id = 0

    def launch(self, headless=True):
        self.kill()
        time.sleep(2)
        args = [CHROME, "--no-sandbox", "--disable-dev-shm-usage",
                "--enable-unsafe-swiftshader", "--disable-gpu-sandbox", "--disable-http2",
                "--hide-scrollbars", "--window-size=%d,%d" % (self.width, self.height),
                "--user-data-dir=" + self.profile,
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
                "--disable-session-crashed-bubble", "--no-first-run", "--password-store=basic",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding"]
        if headless:
            args.insert(1, "--headless=new")
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(30):
            time.sleep(1)
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=3)
                return True
            except Exception:
                pass
        return False

    @staticmethod
    def kill():
        os.system("pkill -f 'remote-debugging-port=%d' 2>/dev/null" % PORT)

    def attach(self, url):
        """优先复用已经打开且域名匹配的标签（用户的标签通常已渲染好），
        其次新建标签。跨境站点新开标签常常只渲染出侧边栏。"""
        host = url.split("//")[-1].split("/")[0].replace("www.", "")
        tab = None
        try:
            tabs = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % PORT, timeout=10).read().decode())
            for t in tabs:
                if t.get("type") != "page":
                    continue
                u = (t.get("url") or "").replace("www.", "")
                if host and host in u:
                    tab = t
                    break
        except Exception:
            pass
        if tab is not None:
            self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=3600)
            self.send("Page.enable")
            return tab
        # 没有对应标签就新开
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/json/new?%s" % (PORT, url), method="PUT")
            tab = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=3600)
            self.send("Page.enable")
            return tab
        except Exception:
            tabs = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % PORT, timeout=10).read().decode())
            pages = [t for t in tabs if t.get("type") == "page"]
            if not pages:
                raise RuntimeError("没有可用标签页")
            tab = pages[0]
            self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=3600)
            self.send("Page.enable")
            self.send("Page.navigate", url=url)
            return tab

    def send(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr):
        try:
            r = self.send("Runtime.evaluate", returnByValue=True, expression=expr)
            return r.get("result", {}).get("value")
        except Exception:
            return None

    def key(self, k, code, vk, text=""):
        for t in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", type=t, key=k, code=code,
                      windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk,
                      text=text if t == "keyDown" else "")

    def shot(self, path):
        try:
            d = self.send("Page.captureScreenshot", format="png")["data"]
            open(path, "wb").write(base64.b64decode(d))
            return path
        except Exception:
            return None


# ---------------------------------------------------------------- 主流程


def ask(plat, prompt, out_path=None, timeout=300, headless=True, sync=False, attach_only=False):
    cfg = PLATFORMS[plat]
    # 1. 同步 profile 副本
    if sync or not os.path.isdir(COPY_PROFILE):
        print("[1/5] 同步 profile 副本…", flush=True)
        os.system("rm -rf %s && cp -a %s %s" % (COPY_PROFILE, BASE_PROFILE, COPY_PROFILE))
    os.system("rm -f %s/SingletonLock %s/SingletonSocket 2>/dev/null" % (COPY_PROFILE, COPY_PROFILE))

    b = Browser(COPY_PROFILE)
    if attach_only:
        print("[2/5] 连接已有浏览器（不新开）…", flush=True)
        import urllib.request as _u
        _u.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=5)
    else:
        print("[2/5] 启动浏览器…", flush=True)
        if not b.launch(headless):
            raise RuntimeError("chromium 启动失败")

    print("[3/5] 打开 %s" % cfg["url"], flush=True)
    b.attach(cfg["url"])
    # 等输入框出现（最多 40 秒），比固定等待可靠
    sel = json.dumps(cfg["input"])
    for _ in range(20):
        time.sleep(2)
        if b.js("!!document.querySelector(%s)" % sel):
            break
    time.sleep(2)

    # 关弹窗
    b.key("Escape", "Escape", 27)
    time.sleep(1)

    # 新对话
    if cfg["new_chat"]:
        b.js("""(()=>{const t=%s;const els=[...document.querySelectorAll('button,div[role=button],a,span,div')]
          .filter(e=>e.offsetWidth>0&&e.offsetHeight>0&&e.children.length<=2&&(e.innerText||e.textContent||'').trim()===t);
          if(els.length){els[0].click();return 1}return 0})()""" % json.dumps(cfg["new_chat"]))
        time.sleep(3)

    # 输入
    ok = b.js("""(()=>{const e=document.querySelector(%s);
      if(!e)return 'no-input'; e.focus();
      if(e.tagName==='TEXTAREA'){e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));}
      else{document.execCommand('selectAll');document.execCommand('delete');}
      return e.tagName})()""" % json.dumps(cfg["input"]))
    if ok == "no-input":
        diag = b.js("(()=>{const c=document.body.cloneNode(true);c.querySelectorAll('script,style,noscript').forEach(e=>e.remove());return c.textContent.slice(0,300)})()") or ""
        raise RuntimeError("找不到输入框（可能未登录或被拦）: %s | 页面: %s" % (cfg["input"], diag.replace("\n", " ")))
    b.send("Input.insertText", text=prompt)
    time.sleep(2)
    print("[4/5] 已输入 %d 字，发送…" % len(prompt), flush=True)

    # 发送（带重试验证：发送成功的标志是输入框被清空）
    key_frag = json.dumps(prompt[:12])
    sel_js = json.dumps(cfg["input"])
    sent = False
    for attempt in range(1, 4):
        print("  发送尝试 %d/3" % attempt, flush=True)
        b.js("""(()=>{const e=document.querySelector(%s); if(!e) return 0;
          e.click(); e.focus();
          if(e.tagName==='TEXTAREA'){e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));}
          else{document.execCommand('selectAll');document.execCommand('delete');}
          return 1})()""" % sel_js)
        b.send("Input.insertText", text=prompt)
        time.sleep(1.5)
        # 点发送：按平台选择
        if cfg["send"] == "send-div":
            r = b.js("""(()=>{const els=[...document.querySelectorAll(
                'div[class*="send"],div[class*="Send"],button[class*="send"],button[class*="Send"],button[aria-label="Send"],button[aria-label="发送"]')]
              .filter(e=>e.offsetWidth>0&&e.offsetHeight>0);
              if(!els.length) return 'none'; els[els.length-1].click(); return 'clicked'})()""")
            if r == "none":
                b.key("Enter", "Enter", 13)
        elif cfg["send"] == "send-button":
            r = b.js("""(()=>{const t=document.querySelector('button[data-testid="send-button"]')||
              document.querySelector('button[aria-label*="Send"]')||
              document.querySelector('button[aria-label*="发送"]');
              if(t){t.click();return 'btn'}return 'none'})()""")
            if r == "none":
                b.key("Enter", "Enter", 13)
        elif cfg["send"] == "enter_cr":
            b.key("Enter", "Enter", 13, "\r")
        else:
            b.key("Enter", "Enter", 13)
        # 验证发送成功：URL 变化 或 页面文字显著增长（Quill/ProseMirror 发送后不一定清空输入框）
        base_len = b.js("document.body.textContent.length") or 0
        base_url = b.js("location.href") or ""
        for _ in range(6):
            time.sleep(2)
            cur_url = b.js("location.href") or ""
            cur_len = b.js("document.body.textContent.length") or 0
            if cur_url != base_url or cur_len > base_len + 120:
                sent = True
                break
        if sent:
            break
        time.sleep(1)
    if not sent:
        raise RuntimeError("三次尝试后消息仍未发出（输入框未清空）")
    print("  发送确认 ✓", flush=True)

    time.sleep(4)

    # 轮询
    best, last, stable = "", 0, 0
    loops = max(1, timeout // 5)
    for i in range(loops):
        time.sleep(5)
        txt = b.js("(()=>{const c=document.body.cloneNode(true);c.querySelectorAll('script,style,noscript').forEach(e=>e.remove());return c.textContent})()") or ""
        if len(txt) > len(best):
            best = txt
        if len(txt) == last:
            stable += 1
            if stable >= 8 and len(txt) > 2000:
                break
        else:
            stable = 0
        last = len(txt)
        if i % 4 == 0:
            print("    等待回答 %ds len=%d" % ((i + 1) * 5, len(txt)), flush=True)

    # 保存
    out = out_path or "/data/ai_ask_%s_%s.txt" % (plat, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        f.write(best)
    print("[5/5] 完成 %d 字 → %s" % (len(best), out), flush=True)
    png = out.replace(".txt", ".png").replace(".json", ".png")
    b.shot(png)
    if len(best) < 2000:
        # 抓到的太少：多半是平台用了封闭 shadow DOM，截图为证
        print("⚠ 文字只拿到 %d 字，已截图 %s（该平台 JS 读不到内容，看截图）" % (len(best), png), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description="用真实浏览器问各家 AI")
    ap.add_argument("platform", nargs="?", help="kimi/doubao/zhipu/deepseek/yuanbao/all/list")
    ap.add_argument("prompt", nargs="?", help="问题文本，或 @文件路径")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("-t", "--timeout", type=int, default=300)
    ap.add_argument("--sync", action="store_true", help="重新复制 profile（东家新登录平台后用）")
    ap.add_argument("--headful", action="store_true", help="带界面启动（调试用）")
    ap.add_argument("--attach", action="store_true", help="连已有浏览器实例，不新开")
    a = ap.parse_args()

    if not a.platform or a.platform == "list":
        print("可用平台：")
        for k, v in PLATFORMS.items():
            print("  %-10s %s" % (k, v["url"]))
        return

    if not a.prompt:
        sys.exit("缺问题文本")
    prompt = open(a.prompt[1:]).read() if a.prompt.startswith("@") else a.prompt

    targets = list(PLATFORMS) if a.platform == "all" else [a.platform]
    for p in targets:
        if p not in PLATFORMS:
            sys.exit("未知平台：%s" % p)
        try:
            ask(p, prompt, a.out, a.timeout, not a.headful, a.sync, a.attach)
        except Exception as e:
            print("❌ %s 失败：%s" % (p, e), flush=True)


if __name__ == "__main__":
    main()
