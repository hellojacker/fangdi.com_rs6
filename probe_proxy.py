# -*- coding: utf-8 -*-
"""探测 qg.net 共享代理接口返回格式"""
import requests
import os

url = "https://share.proxy.qg.net/get"
params = {"key": os.environ.get("QG_PROXY_KEY", "YOUR_KEY"), "num": 5}
r = requests.get(url, params=params, timeout=30)
print("status:", r.status_code)
print("headers ct:", r.headers.get("content-type"))
print("---- body repr ----")
print(repr(r.text[:800]))
print("---- lines ----")
for ln in r.text.strip().splitlines()[:10]:
    print("  |", ln)
