# -*- coding: utf-8 -*-
"""验证代理对 fangdi.com.cn 的连通性"""
# 允许直接 `python <subdir>/<script>.py` 运行：把项目根（含 fangdi_api.py）加入 sys.path
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import requests, json
import os

API = "https://share.proxy.qg.net/get"
r = requests.get(API, params={"key": os.environ.get("QG_PROXY_KEY", "YOUR_KEY"), "num": 8}, timeout=30)
data = r.json()
print("code:", data.get("code"), "| count:", len(data.get("data", [])))
servers = [d["server"] for d in data["data"]]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
for s in servers:
    px = {"http": f"http://{s}", "https": f"http://{s}"}
    try:
        resp = requests.get("https://www.fangdi.com.cn/old_house/old_house.html",
                            headers={"User-Agent": UA}, proxies=px, timeout=12)
        print(f"{s:24s} -> {resp.status_code} ({len(resp.text)}B)  {'OK(挑战页)' if resp.status_code==412 else 'OK' if resp.status_code==200 else '??'}")
    except Exception as e:
        print(f"{s:24s} -> FAIL {type(e).__name__}: {str(e)[:60]}")
