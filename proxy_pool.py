# -*- coding: utf-8 -*-
"""
共享代理池（qg.net 接口风格）
================================
接口: GET https://share.proxy.qg.net/get?key=<KEY>&num=<数量>
返回 JSON:
  {"code": "SUCCESS", "data": [{"server": "ip:port", "deadline": "2026-10-30 13:00:26", ...}]}

特性:
  * fetch(num)        拉取一批代理（直连接口，不走代理，避免循环依赖）
  * get()             粘性取代理（sticky=True 时固定当前代理直到被剔除）
  * force_next()      强制切换到下一个代理（会话失效重建时明确换 IP）
  * mark_bad(item)    剔除连接失败代理
  * deadline 过期判断（接口时间按北京时间处理）
"""
import time
import json
import threading
import requests
from datetime import datetime, timezone, timedelta

CN_TZ = timezone(timedelta(hours=8))


class ProxyPool:
    def __init__(self, api_url, num=5, refill_gap=3.0, verbose=True, sticky=True):
        self.api_url = api_url
        self.num = num
        self.refill_gap = refill_gap      # 两次拉取接口最小间隔（防狂拉）
        self.verbose = verbose
        self.sticky = sticky              # True=固定当前代理（worker 单独 IP），坏了才换
        self.items = []                   # [{"server","proxy","deadline(ts)"}]
        self.idx = 0
        self.current = None               # 粘性模式下当前固定代理
        self.last_fetch = 0.0
        self.lock = threading.Lock()

    def _log(self, *a):
        if self.verbose:
            print("[proxy] " + " ".join(str(x) for x in a))

    def fetch(self, num=None):
        """从接口拉取 num 个代理，追加进池。失败抛异常（由调用方决定直连兜底）。"""
        n = num or self.num
        resp = requests.get(self.api_url, params={"num": n}, timeout=20)
        data = resp.json()
        if data.get("code") != "SUCCESS":
            raise RuntimeError(f"代理接口返回 {data.get('code')}: {str(data)[:120]}")
        added = 0
        for d in data.get("data", []):
            server = d.get("server")
            if not server or ":" not in server:
                continue
            deadline = None
            try:
                deadline = datetime.strptime(d["deadline"], "%Y-%m-%d %H:%M:%S") \
                                 .replace(tzinfo=CN_TZ).timestamp()
            except Exception:
                pass
            item = {"server": server,
                    "proxy": {"http": f"http://{server}", "https": f"http://{server}"},
                    "deadline": deadline}
            self.items.append(item)
            added += 1
        self.last_fetch = time.time()
        self._log(f"拉取 {added} 个代理 (池内共 {len(self.items)})")
        return added

    def _purge_expired(self):
        now = time.time()
        before = len(self.items)
        self.items = [it for it in self.items
                      if it["deadline"] is None or it["deadline"] > now]
        if before != len(self.items):
            self._log(f"剔除 {before - len(self.items)} 个过期代理")

    def _maybe_refill(self):
        if self.items or (time.time() - self.last_fetch) < self.refill_gap:
            return
        try:
            self.fetch()
        except Exception as e:
            self._log(f"补充拉取失败: {e}")

    def get(self):
        """取代理。sticky=True：固定返回 current（坏掉被剔除后才换下一个）；
        sticky=False：轮询。池空且拉不到返回 None。"""
        with self.lock:
            self._purge_expired()
            if self.sticky and self.current is not None:
                if self.current in self.items:
                    return self.current
                self.current = None          # current 已不在池（被剔/过期）
            self._maybe_refill()
            if not self.items:
                return None
            item = self.items[self.idx % len(self.items)]
            self.idx += 1
            if self.sticky:
                self.current = item
            return item

    def force_next(self):
        """强制切换：从 current 之外选下一个代理并固定。池空返回 None。"""
        with self.lock:
            self._purge_expired()
            self._maybe_refill()
            if not self.items:
                self.current = None
                return None
            if len(self.items) == 1:
                self.current = self.items[0]
                return self.current
            if self.current in self.items:
                idx = (self.items.index(self.current) + 1) % len(self.items)
            else:
                idx = self.idx % len(self.items)
            self.current = self.items[idx]
            self.idx = idx + 1
            return self.current

    def mark_bad(self, item):
        """剔除连接失败的代理；若为当前粘性代理则清除，下次 get 换新。"""
        with self.lock:
            if item in self.items:
                self.items.remove(item)
                if item == self.current:
                    self.current = None
                self._log(f"剔除坏代理 {item['server']} (池内剩 {len(self.items)})")

    def size(self):
        return len(self.items)
