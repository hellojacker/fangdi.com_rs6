# -*- coding: utf-8 -*-
"""
fangdi.com.cn 瑞数第二阶段补环境（iv8 版）
=====================================================
目标：请求 https://www.fangdi.com.cn/oldhouse/selectOldHouse.action 返回 200

原理（类比第一阶段 cookie 生成）：
  第一阶段：412 拦截页 -> 瑞数JS -> 生成动态 cookie(UA1L1zGonajvO) -> 重试得 200 页面
  第二阶段：200 页面里带着【第二套瑞数挑战】(新的 $_ts + 新外联 JS jNafbsZdWo4R...)
           该 JS 会 hook XMLHttpRequest。当 new 一个 XHR 打 .action 时，
           瑞数在 open/send 阶段重写请求，追加：
             - 瑞数后缀(URL suffix)：?XJlCTRRM=<动态签名>
             - 请求头后缀：新生成的动态 cookie(UA1L1zGonajvP) 等，随 Cookie 头发出
           => 提取这两者，用 Python 携带 cookie 重放真实 .action 请求即可 200

关键点：后缀是时间/会话绑定的，必须在同一次运行里“生成即重放”。
"""
import console_fix
import re
import json
import iv8
import requests
from urllib.parse import urljoin

BASE = "https://www.fangdi.com.cn"
PAGE_URL = BASE + "/old_house/old_house.html"
API_PATH = "/oldhouse/selectOldHouse.action"     # 第二阶段目标接口
API_METHOD = "GET"                                # 站点 .action 多为 POST，此处先按 GET 验证签名

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
environment = {
    "location": {"href": PAGE_URL, "origin": BASE, "protocol": "https:",
                 "host": "www.fangdi.com.cn", "hostname": "www.fangdi.com.cn",
                 "port": "", "pathname": "/old_house/old_house.html", "search": "", "hash": ""},
    "navigator": {"userAgent": UA, "language": "zh-CN",
                  "languages": ["zh-CN", "en-US", "en"], "platform": "Win32"},
}
headers = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
session = requests.Session()


def run_stage1_to_200():
    """第一阶段：412 -> iv8 生成 cookie -> 200 页面。返回 (html2, js2_url, js2_code)"""
    resp1 = session.get(PAGE_URL, headers=headers, timeout=15)
    print(f"[1] 首次请求 status = {resp1.status_code}")
    js1_url = BASE + re.search(r'src="([^"]+\.js)"[^>]*r=\'m\'', resp1.text).group(1)
    js1 = session.get(js1_url, headers=headers, timeout=15).text
    print(f"    第一阶段瑞数 JS: {js1_url} ({len(js1)}b)")

    with iv8.JSContext(environment=environment, config={"timezone": "Asia/Shanghai"}) as ctx:
        snap = {"baseURL": PAGE_URL, "html": resp1.text,
                "headers": [["set-cookie", f"{k}={v}; Path=/"] for k, v in session.cookies.get_dict().items()],
                "resources": {js1_url: {"body": js1, "status": 200}}}
        ctx.expose(snap, "snapshot")
        ctx.eval("__iv8__.page.load(__iv8__.data.snapshot)")
        for _ in range(10):
            ctx.eval("__iv8__.eventLoop.sleep(200)")
            if ctx.eval("document.cookie.length") > 0:
                break
        ck = ctx.eval("document.cookie")
    for pair in ck.split(";"):
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            session.cookies.set(k, v, domain="www.fangdi.com.cn")

    resp2 = session.get(PAGE_URL, headers={**headers, "Referer": PAGE_URL}, timeout=15)
    print(f"[2] 第二阶段页面 status = {resp2.status_code} ({len(resp2.text)}b)")
    html2 = resp2.text

    js2_path = re.search(r'src="([^"]+\.js)"[^>]*r=[\'"]m[\'"]', html2).group(1)
    js2_url = urljoin(PAGE_URL, js2_path)
    js2_code = session.get(js2_url, headers=headers, timeout=15).text
    print(f"    第二阶段瑞数 JS: {js2_url} ({len(js2_code)}b)")
    return html2, js2_url, js2_code


def gen_suffix_and_cookie(html2, js2_url, js2_code):
    """
    第二阶段：iv8 加载 200 页面 + 第二阶段瑞数 JS，安装 XHR hook，
    new 一个 XHR 打 API_PATH，捕获瑞数重写后的 URL 后缀 + cookieHeader。
    返回 (full_signed_url, cookie_header, js_cookie)
    """
    with iv8.JSContext(environment=environment, config={"timezone": "Asia/Shanghai"}) as ctx:
        snap2 = {"baseURL": PAGE_URL, "html": html2,
                 "headers": [["set-cookie", f"{k}={v}; Path=/"] for k, v in session.cookies.get_dict().items()],
                 "resources": {js2_url: {"body": js2_code, "status": 200,
                                         "headers": [["content-type", "application/javascript"]]}}}
        ctx.expose(snap2, "snapshot2")
        ctx.eval("__iv8__.page.load(__iv8__.data.snapshot2)")
        for _ in range(10):
            ctx.eval("__iv8__.eventLoop.sleep(300)")

        # 记录当前 netLog 条数，便于定位我们新发的那条
        base_n = ctx.eval("__iv8__.netLog.entries.length")

        # new 一个 XHR 打目标接口 —— 瑞数 hook 会重写出后缀
        ctx.eval(f"""
            (function() {{
                var xhr = new XMLHttpRequest();
                xhr.open({json.dumps(API_METHOD)}, {json.dumps(API_PATH)});
                xhr.send();
            }})();
        """)
        ctx.eval("__iv8__.eventLoop.drain()")
        ctx.eval("__iv8__.eventLoop.sleep(200)")

        entries = ctx.eval("__iv8__.netLog.entries", to_py=True) or []
        js_cookie = ctx.eval("document.cookie")

        # 找到打向 API_PATH 的那条（带签名后缀）
        target = None
        for e in entries[base_n:]:
            if API_PATH in e.get("url", ""):
                target = e
                break
        if target is None:  # 兜底：全表里找
            for e in entries:
                if API_PATH in e.get("url", ""):
                    target = e
                    break
        return target, js_cookie, entries


# ================= 主流程 =================
html2, js2_url, js2_code = run_stage1_to_200()

print("[3] iv8 执行第二阶段瑞数 JS，new XHR 生成后缀 ...")
target, js_cookie, entries = gen_suffix_and_cookie(html2, js2_url, js2_code)

if not target:
    raise RuntimeError("未捕获到 selectOldHouse.action 的重写请求")

signed_url = target["url"]
cookie_header = target.get("cookieHeader", "")
req_headers = target.get("headers", [])
print(f"    瑞数后缀(URL): {signed_url}")
print(f"    请求头(netLog.headers): {req_headers}")
print(f"    cookieHeader 长度: {len(cookie_header)}")

# 把 iv8 里新生成的动态 cookie(如 UA1L1zGonajvP) 合并进 Python session
for pair in js_cookie.split(";"):
    if "=" in pair:
        k, v = pair.strip().split("=", 1)
        session.cookies.set(k, v, domain="www.fangdi.com.cn")

# ---- 用捕获到的签名 URL + cookie 重放真实请求 ----
print("[4] 携带瑞数后缀 + cookie 重放真实请求 ...")
replay_headers = {
    "User-Agent": UA,
    "Referer": PAGE_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}
resp = session.request(API_METHOD, signed_url, headers=replay_headers, timeout=15)
print(f"    => status = {resp.status_code}, body = {len(resp.text)}b")
print(f"    => 响应片段: {resp.text[:300]}")

open("stage2_signed_url.txt", "w", encoding="utf-8").write(signed_url)
open("stage2_response.txt", "w", encoding="utf-8").write(resp.text)
if resp.status_code == 200:
    print("[OK] selectOldHouse.action 返回 200")
else:
    print(f"[INFO] 返回 {resp.status_code}（若为 412/403 需检查后缀时效或改用 POST/补参数）")
