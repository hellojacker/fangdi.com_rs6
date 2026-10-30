# -*- coding: utf-8 -*-
"""探测 fangdi.com.cn 当前瑞数第一阶段挑战结构"""
# 允许直接 `python <subdir>/<script>.py` 运行：把项目根（含 fangdi_api.py）加入 sys.path
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import requests, re, sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
s = requests.Session()
h = {"User-Agent": UA,
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

r1 = s.get("https://www.fangdi.com.cn/old_house/old_house.html", headers=h, timeout=20)
print("status:", r1.status_code, "len:", len(r1.text))
print("cookies:", s.cookies.get_dict())
print("has $_ts:", "$_ts" in r1.text)
print("---- 页面头部片段 ----")
print(r1.text[:600].replace("\n", " ")[:600])
print("---- script 标签 ----")
for m in re.finditer(r"<script[^>]*>", r1.text):
    print(" ", m.group(0)[:150])
# 正则匹配瑞数外联 JS
for pat in [r"src=\"([^\"]+\.js)\"[^>]*r='m'", r"src='([^']+\.js)'[^>]*r='m'",
            r"src=\"([^\"]+\.js)\"", r"src='([^']+\.js)'"]:
    ms = re.findall(pat, r1.text)
    print("pattern", pat[:40], "->", ms[:5])
