# -*- coding: utf-8 -*-
"""测试：坏代理剔除轮换 + 代理真实生效 + rebootstrap 换 IP"""
# 允许直接 `python <subdir>/<script>.py` 运行：把项目根（含 fangdi_api.py）加入 sys.path
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import console_fix
import os
from proxy_pool import ProxyPool
from fangdi_api import FangdiRuishu

pool = ProxyPool("https://share.proxy.qg.net/get?key="
                 + os.environ.get("QG_PROXY_KEY", "YOUR_KEY"), num=3, verbose=True)
pool.fetch()
# 池头插入一个必失败的坏代理
bad = {"server": "127.0.0.1:1",
       "proxy": {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"},
       "deadline": None}
pool.items.insert(0, bad)
print(f"池内代理: {[p['server'] for p in pool.items]}")

with FangdiRuishu(proxy_pool=pool) as fd:
    dic = fd.get_districts()
    print("TEST1 坏代理轮换后 getDic:", len(dic), "个区县")
    resp, _ = fd.select_old_house(page=1, district=dic.get("金山区"))
    print("TEST2 selectOldHouse status:", resp.status_code)
    fd.rebootstrap()
    print("TEST3 rebootstrap 后 getDic:", len(fd.get_districts()), "个区县")
    print("TEST4 当前 session.proxies:", fd.session.proxies)
