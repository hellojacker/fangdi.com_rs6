# -*- coding: utf-8 -*-
"""测试：模拟 SessionExpiredError 触发页级重建重试（真实 rebootstrap）"""
# 允许直接 `python <subdir>/<script>.py` 运行：把项目根（含 fangdi_api.py）加入 sys.path
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import console_fix
import os
import shutil
from fangdi_api import FangdiRuishu, SessionExpiredError
from fangdi_batch import ResumableScraper

sc = ResumableScraper(out_dir="out_test", state_path="test_state.json",
                      max_pages=1, delay=0.2)
with FangdiRuishu() as fd:
    dic = fd.get_districts()
    filters = {"district": dic["黄浦区"]}

    orig = fd.select_old_house
    calls = {"n": 0}

    def flaky(page=1, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SessionExpiredError("模拟：cookie 失效")
        return orig(page=page, **kw)

    fd.select_old_house = flaky
    ok = sc._try_page(fd, "黄浦区", 8, filters)
    print("TEST5 模拟会话失效 -> 页级重建重试 page 8:",
          "OK" if ok else "FAIL",
          "| select_old_house 被调用", calls["n"], "次（预期 2：1 次失效 + 1 次重建后）")
    print("      状态: next_page =", sc.slice_state("黄浦区")["next_page"],
          "records =", sc.slice_state("黄浦区")["records"])

# 清理
for p in ("test_state.json", "test_state.json.tmp"):
    if os.path.exists(p):
        os.remove(p)
if os.path.isdir("out_test"):
    shutil.rmtree("out_test")
print("[cleanup] 测试产物已清理")
