# -*- coding: utf-8 -*-
"""
fangdi.com.cn 瑞数第二阶段 —— 可复用 API 签名器（iv8 版）
================================================================
在 fangdi_stage2.py（单次验证）基础上，升级为可反复签名任意 .action 接口的工具：

  * 一次 bootstrap：完成第一阶段(412->cookie->200) + 加载第二阶段瑞数 JS，
    在 iv8 里常驻一个已 hook 好 XMLHttpRequest 的浏览器环境。
  * 每次 call()：在 iv8 里 new 一个 XHR 打目标接口 -> 瑞数重写出「URL 后缀」
    (?XJlCTRRM=<签名>) 与动态 cookie(请求头后缀) -> 立即用 Python 携带重放。
  * 后缀是时间/会话绑定的，故 sign 与 replay 在同一函数内瞬时完成；多次请求
    复用同一 iv8 上下文，每次都拿到全新后缀。

真实接口签名（来自 oldhouselist.js）：
  POST /oldhouse/selectOldHouse.action
  Content-Type: application/x-www-form-urlencoded;charset=utf-8
  body: district,area,location,price,region,blockName,time,houseType,
        listingNo,verifyNo,currentPage
"""
import console_fix
import json
import os

import iv8
from urllib.parse import urlencode, urljoin

# ---------- HTTP 后端 ----------
# 站点会校验 TLS/HTTP2 指纹：裸 requests 常常只换回 400（空 body，连挑战页都不给），
# curl_cffi 的 Chrome 指纹能正常拿到 412 挑战页。默认自动优先 curl_cffi。
#   FANGDI_HTTP=auto|curl_cffi|requests      FANGDI_IMPERSONATE=chrome
_HTTP_MODE = os.environ.get("FANGDI_HTTP", "auto").strip().lower()
_http = None
_IMPERSONATE = None
if _HTTP_MODE in ("auto", "curl_cffi"):
    try:
        from curl_cffi import requests as _http            # noqa: F401
        from curl_cffi.requests import exceptions as _http_exc
        _IMPERSONATE = os.environ.get("FANGDI_IMPERSONATE", "chrome")
    except Exception:
        _http = None
if _http is None:
    import requests as _http                               # noqa: F401
    from requests import exceptions as _http_exc
    _IMPERSONATE = None
_NET_EXC = (getattr(_http_exc, "RequestException", Exception),)


def new_http_session():
    """按当前后端创建会话；curl_cffi 走浏览器指纹，requests 走原生栈。"""
    return _http.Session(impersonate=_IMPERSONATE) if _IMPERSONATE else _http.Session()

BASE = "https://www.fangdi.com.cn"
PAGE_URL = BASE + "/old_house/old_house.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class SessionExpiredError(RuntimeError):
    """瑞数会话失效（412/403 或返回挑战页），需要 rebootstrap 重建环境"""


def _is_challenge(resp):
    """判定响应是否为瑞数挑战页（会话失效特征）。"""
    return (resp.status_code in (412, 403)
            or ("$_ts" in resp.text and "htmlView" not in resp.text))


class FangdiRuishu:
    """瑞数两阶段补环境 + API 签名器"""

    def __init__(self, page_url=PAGE_URL, verbose=True, proxy_pool=None):
        self.page_url = page_url
        self.verbose = verbose
        self.session = new_http_session()
        self.proxy_pool = proxy_pool          # ProxyPool 实例；None=直连
        self.ctx = None
        self._last_proxy_server = None        # 观测：当前生效代理
        self.environment = {
            "location": {"href": page_url, "origin": BASE, "protocol": "https:",
                         "host": "www.fangdi.com.cn", "hostname": "www.fangdi.com.cn",
                         "port": "", "pathname": page_url.split("fangdi.com.cn")[1],
                         "search": "", "hash": ""},
            "navigator": {"userAgent": UA, "language": "zh-CN",
                          "languages": ["zh-CN", "en-US", "en"], "platform": "Win32"},
        }
        self.base_headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _log(self, *a):
        if self.verbose:
            print(*a)

    # ---------- 统一 HTTP 出口：带代理重试 ----------
    def _request(self, method, url, **kwargs):
        """所有对外请求走这里。配置了代理池时：粘性使用当前代理发起，
        连接失败换下一个；全部失败或池空则直连兜底。"""
        pool = self.proxy_pool
        if pool is None:
            return self.session.request(method, url, **kwargs)
        attempts = max(pool.size(), 1)
        last_err = None
        for _ in range(attempts):
            item = pool.get()
            if item is None:
                break
            if item["server"] != self._last_proxy_server:   # 变化才打日志（可观测粘性）
                self._last_proxy_server = item["server"]
                self._log(f"    [proxy] 使用代理 {item['server']}")
            try:
                self.session.proxies = item["proxy"]
                return self.session.request(method, url, **kwargs)
            except _NET_EXC as e:
                last_err = e
                pool.mark_bad(item)
                self._log(f"    [proxy] {item['server']} 失败({type(e).__name__})，换下一个")
        self.session.proxies = {}
        self._log("    [proxy] 无可用代理，本次直连兜底")
        return self.session.request(method, url, **kwargs)

    def rotate_proxy(self):
        """强制切换到下一个代理（会话失效重建时明确换 IP，规避 IP 级风控）。"""
        if not self.proxy_pool:
            return
        item = self.proxy_pool.force_next()
        if item:
            self.session.proxies = item["proxy"]
            self._last_proxy_server = item["server"]
            self._log(f"    [proxy] 切换代理 -> {item['server']}")
        else:
            self.session.proxies = {}
            self._last_proxy_server = None

    # ---------- 第一阶段：412 -> 生成 cookie -> 200 页面 ----------
    def _stage1(self):
        import re
        resp1 = self._request("GET", self.page_url, headers=self.base_headers, timeout=15)
        self._log(f"[1] 首次请求 status={resp1.status_code}")
        js1_url = BASE + re.search(r'src="([^"]+\.js)"[^>]*r=\'m\'', resp1.text).group(1)
        js1 = self._request("GET", js1_url, headers=self.base_headers, timeout=15).text
        with iv8.JSContext(environment=self.environment, config={"timezone": "Asia/Shanghai"}) as c:
            snap = {"baseURL": self.page_url, "html": resp1.text,
                    "headers": [["set-cookie", f"{k}={v}; Path=/"] for k, v in self.session.cookies.get_dict().items()],
                    "resources": {js1_url: {"body": js1, "status": 200}}}
            c.expose(snap, "snapshot")
            c.eval("__iv8__.page.load(__iv8__.data.snapshot)")
            for _ in range(10):
                c.eval("__iv8__.eventLoop.sleep(200)")
                if c.eval("document.cookie.length") > 0:
                    break
            ck = c.eval("document.cookie")
        self._merge_cookies(ck)
        resp2 = self._request("GET", self.page_url,
                              headers={**self.base_headers, "Referer": self.page_url}, timeout=15)
        self._log(f"[2] 第二阶段页面 status={resp2.status_code}")
        return resp2.text

    def _merge_cookies(self, cookie_str):
        for pair in (cookie_str or "").split(";"):
            if "=" in pair:
                k, v = pair.strip().split("=", 1)
                self.session.cookies.set(k, v, domain="www.fangdi.com.cn")

    # ---------- bootstrap：常驻第二阶段 iv8 环境（装好 XHR hook）----------
    def bootstrap(self):
        import re
        if self.ctx is not None:          # 防御：旧上下文先关闭，避免泄漏
            self.close()
        html2 = self._stage1()
        js2_path = re.search(r'src="([^"]+\.js)"[^>]*r=[\'"]m[\'"]', html2).group(1)
        js2_url = urljoin(self.page_url, js2_path)
        js2_code = self._request("GET", js2_url, headers=self.base_headers, timeout=15).text
        self._log(f"    第二阶段瑞数 JS: {js2_url} ({len(js2_code)}b)")

        self.ctx = iv8.JSContext(environment=self.environment, config={"timezone": "Asia/Shanghai"})
        snap2 = {"baseURL": self.page_url, "html": html2,
                 "headers": [["set-cookie", f"{k}={v}; Path=/"] for k, v in self.session.cookies.get_dict().items()],
                 "resources": {js2_url: {"body": js2_code, "status": 200,
                                         "headers": [["content-type", "application/javascript"]]}}}
        self.ctx.expose(snap2, "snapshot2")
        self.ctx.eval("__iv8__.page.load(__iv8__.data.snapshot2)")
        for _ in range(10):
            self.ctx.eval("__iv8__.eventLoop.sleep(300)")
        self._log("[3] 第二阶段环境就绪（XHR 已被瑞数 hook）")
        return self

    # ---------- 核心：在 iv8 里 new XHR，拿到瑞数重写后的签名 URL ----------
    def sign(self, method, path, body=""):
        base_n = self.ctx.eval("__iv8__.netLog.entries.length")
        set_ctype = 'xhr.setRequestHeader("Content-Type","application/x-www-form-urlencoded;charset=utf-8");' if body else ""
        self.ctx.eval(f"""
            (function() {{
                var xhr = new XMLHttpRequest();
                xhr.open({json.dumps(method)}, {json.dumps(path)});
                {set_ctype}
                xhr.send({json.dumps(body)});
            }})();
        """)
        self.ctx.eval("__iv8__.eventLoop.drain()")
        self.ctx.eval("__iv8__.eventLoop.sleep(150)")
        entries = self.ctx.eval("__iv8__.netLog.entries", to_py=True) or []
        pure_path = path.split("?")[0]
        for e in entries[base_n:]:
            if pure_path in e.get("url", ""):
                return e
        for e in reversed(entries):
            if pure_path in e.get("url", ""):
                return e
        raise RuntimeError(f"未捕获到 {path} 的瑞数重写请求")

    # ---------- 会话失效重建：换 IP + 清 cookie 重走两阶段 ----------
    def rebootstrap(self):
        """cookie 失效（412/403/挑战页）时调用：切换代理 IP、关闭旧 iv8 上下文、
        清空会话 cookie、重新走 412->cookie->200 + 第二阶段环境。"""
        self.close()
        self.rotate_proxy()          # 换 IP 与换 cookie 同步，规避 IP 级风控
        self.session.cookies.clear()
        self._log("[rebootstrap] 检测到会话失效，清空 cookie 重建两阶段环境 ...")
        return self.bootstrap()

    # ---------- 签名 + 立即重放 ----------
    def call(self, method, path, data=None):
        body = urlencode(data) if data else ""
        entry = self.sign(method, path, body)
        signed_url = entry["url"]
        # 同步 iv8 里最新的动态 cookie（请求头后缀），再重放
        self._merge_cookies(self.ctx.eval("document.cookie"))
        replay_headers = {
            "User-Agent": UA,
            "Referer": self.page_url,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        }
        if body:
            replay_headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
        resp = self._request(method, signed_url,
                             data=body if body else None,
                             headers=replay_headers, timeout=15)
        self._log(f"    {method} {path} -> {resp.status_code} ({len(resp.text)}b)  后缀: {signed_url.split('?')[-1][:40]}...")
        if _is_challenge(resp):
            self._log(f"    [session-expired] {method} {path} 返回挑战页/拦截，抛 SessionExpiredError")
            raise SessionExpiredError(f"{method} {path} 会话失效 status={resp.status_code}")
        return resp, signed_url

    # ---------- 业务封装 ----------
    def get_districts(self):
        """获取区县字典（getDic.action），返回 {name: code}"""
        resp, _ = self.call("POST", "/oldhouse/getDic.action")
        mapping = {}
        try:
            for d in resp.json().get("dicList", []):
                if str(d.get("code")) != "0":
                    mapping[d.get("name")] = d.get("code")
        except Exception:
            pass
        return mapping

    def select_old_house(self, page=1, **filters):
        """查询二手房源；filters 可含 district/area/location/price/region/
        blockName/time/houseType/listingNo/verifyNo"""
        data = {"district": "", "area": "", "location": "", "price": "", "region": "",
                "blockName": "", "time": "", "houseType": "", "listingNo": "",
                "verifyNo": "", "currentPage": str(page)}
        data.update({k: v for k, v in filters.items() if v is not None})
        resp, signed = self.call("POST", "/oldhouse/selectOldHouse.action", data)
        return resp, signed

    def close(self):
        if self.ctx:
            self.ctx.close()
            self.ctx = None

    def __enter__(self):
        return self.bootstrap()

    def __exit__(self, *exc):
        self.close()


# ===================== 演示 =====================
if __name__ == "__main__":
    with FangdiRuishu() as fd:
        # 1) 区县字典（验证多接口签名复用）
        districts = fd.get_districts()
        print(f"\n[区县字典] 共 {len(districts)} 个，示例: {dict(list(districts.items())[:5])}")

        # 2) 全量第 1 页
        resp, signed = fd.select_old_house(page=1)
        print(f"\n[全量 page=1] status={resp.status_code}")
        open("api_page1.html", "w", encoding="utf-8").write(resp.text)

        # 3) 指定区县 + 分页（取字典第一个区县，查第 2 页）
        if districts:
            name, code = next(iter(districts.items()))
            resp2, signed2 = fd.select_old_house(page=2, district=code)
            print(f"\n[{name}(code={code}) page=2] status={resp2.status_code}")
            open("api_district_page2.html", "w", encoding="utf-8").write(resp2.text)
            print(f"    签名URL: {signed2}")

        print("\n[DONE] 结果已保存 api_page1.html / api_district_page2.html")
