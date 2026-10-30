# -*- coding: utf-8 -*-
"""
fangdi.com.cn 瑞数反爬补环境（iv8 版）
流程参照 skill.md 案例三：
1. 首次请求拿到 412 拦截页 HTML + 初始 cookie
2. 提取外联反爬 JS 并下载
3. iv8 page.load 执行反爬 JS，生成动态 cookie
4. 携带 cookie 重新请求，返回 200
"""
import console_fix
import re
import iv8
import requests

PAGE_URL = "https://www.fangdi.com.cn/old_house/old_house.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

environment = {
    "location": {
        "href": PAGE_URL,
        "origin": "https://www.fangdi.com.cn",
        "protocol": "https:",
        "host": "www.fangdi.com.cn",
        "hostname": "www.fangdi.com.cn",
        "port": "",
        "pathname": "/old_house/old_house.html",
        "search": "",
        "hash": "",
    },
    "navigator": {
        "userAgent": UA,
        "language": "zh-CN",
        "languages": ["zh-CN", "en-US", "en"],
        "platform": "Win32",
    },
}

headers = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

session = requests.Session()

# ---------- 1. 首次请求，获取反爬拦截页 ----------
resp1 = session.get(PAGE_URL, headers=headers, timeout=15)
print(f"[1] 首次请求 status = {resp1.status_code}")
init_cookies = session.cookies.get_dict()
print(f"    初始 cookie: {list(init_cookies.keys())}")

# ---------- 2. 提取外联反爬 JS URL 并下载 ----------
js_match = re.search(r'src="([^"]+\.js)"[^>]*r=\'m\'', resp1.text)
if not js_match:
    raise RuntimeError("未找到外联反爬 JS，页面结构可能已变化")
js_path = js_match.group(1)
js_url = "https://www.fangdi.com.cn" + js_path
js_code = session.get(js_url, headers=headers, timeout=15).text
print(f"[2] 反爬 JS: {js_url} ({len(js_code)} bytes)")

# ---------- 3. iv8 执行反爬 JS，生成动态 cookie ----------
with iv8.JSContext(environment=environment, config={"timezone": "Asia/Shanghai"}) as ctx:
    snapshot = {
        "baseURL": PAGE_URL,
        "html": resp1.text,
        "headers": [["set-cookie", f"{k}={v}; Path=/"] for k, v in init_cookies.items()],
        "resources": {js_url: {"body": js_code, "status": 200,
                               "headers": [["content-type", "application/javascript"]]}},
    }
    ctx.expose(snapshot, "snapshot")
    ctx.eval("__iv8__.page.load(__iv8__.data.snapshot)")

    # 推进事件循环，让瑞数 JS 的定时器 / XHR 逻辑跑完
    for _ in range(10):
        ctx.eval("__iv8__.eventLoop.sleep(200)")
        if ctx.eval("document.cookie.length") > 0:
            break

    js_cookie = ctx.eval("document.cookie")
    print(f"[3] JS 生成的 cookie: {js_cookie[:120]}{'...' if len(js_cookie) > 120 else ''}")

    # 若瑞数通过 XHR 重试（netLog 中带 cookieHeader），一并取出
    try:
        entries = ctx.eval("__iv8__.netLog.entries", to_py=True) or []
        for e in entries:
            print(f"    netLog: {e.get('method')} {e.get('url')}")
    except Exception:
        pass

if not js_cookie:
    raise RuntimeError("反爬 JS 未生成 cookie，需进一步分析（可开 debug 模式看 API 访问链）")

# ---------- 4. 合并 cookie 重新请求 ----------
for pair in js_cookie.split(";"):
    if "=" in pair:
        k, v = pair.strip().split("=", 1)
        session.cookies.set(k, v, domain="www.fangdi.com.cn")

final = session.get(PAGE_URL, headers={**headers, "Referer": PAGE_URL}, timeout=15)
print(f"[4] 重试请求 status = {final.status_code}, body = {len(final.text)} bytes")
title = re.search(r"<title>(.*?)</title>", final.text, re.S)
print(f"    页面标题: {title.group(1).strip() if title else '(无)'}")

if final.status_code == 200:
    with open("old_house_200.html", "w", encoding="utf-8") as f:
        f.write(final.text)
    print("[OK] 请求返回 200，内容已保存到 old_house_200.html")
else:
    print("[FAIL] 仍被拦截，响应片段：")
    print(final.text[:500])
