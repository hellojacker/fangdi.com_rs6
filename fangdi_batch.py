# -*- coding: utf-8 -*-
"""
fangdi.com.cn 二手房源 —— 按区县分片 + 断点续采 + cookie 失效页级重试 + 多进程并发
================================================================================
在 fangdi_api.FangdiRuishu 签名器之上：

1. 按 district 分片：默认遍历全部区县（getDic.action 字典），每区县独立进度与输出文件；
2. 断点续采：进度状态写入 scrape_state.json（每页成功后原子落盘 + 文件锁），
   中断后重跑自动续采；失败页下次运行优先补采；
3. cookie 失效页级重试：单页返回 412/403/挑战页（SessionExpiredError）时，
   自动 rebootstrap 重建两阶段环境后重试【当前页】，不重复已抓页面；
4. 多进程并发（--workers N）：每个 worker 独立会话（独立 iv8 上下文/cookie/签名），
   按片粒度并行，state 跨进程文件锁保护。

用法示例：
    python fangdi_batch.py                                  # 全部区县分片，单进程断点续采
    python fangdi_batch.py --workers 10                     # 10 并发（每 worker 一片）
    python fangdi_batch.py --districts 黄浦区,徐汇区 --workers 10
    python fangdi_batch.py --district 黄浦区 --pages 0      # 兼容旧参数：单片抓完
    python fangdi_batch.py --pages 5 --delay 0.8            # 每片最多 5 页
    python fangdi_batch.py --fresh                          # 忽略状态从头重抓
    python fangdi_batch.py --merge                          # 结束后合并全部片为一个文件
    python fangdi_batch.py --param houseType=xxx --param area=yyy
"""
import console_fix
import re
import csv
import json
import os
import sys
import time
import argparse
import multiprocessing as mp

from fangdi_api import FangdiRuishu, BASE, SessionExpiredError
from proxy_pool import ProxyPool

# htmlView 表格 7 列 -> 字段名
COLUMNS = ["district", "location", "house_type", "orientation", "total_floor", "area"]
FIELDS = COLUMNS + ["detail_id", "detail_url", "page"]
STATE_VERSION = 2
DEFAULT_STATE = "scrape_state.json"
DEFAULT_OUTDIR = "out"
# 代理提取接口：key 从环境变量读取（仓库内只放占位，不发布真实 key）
DEFAULT_PROXY_URL = ("https://share.proxy.qg.net/get?key="
                     + os.environ.get("QG_PROXY_KEY", "YOUR_KEY"))


# ---------------- 解析 ----------------
def parse_house_table(html_view):
    """从 htmlView 的 <table> 解析房源记录（跳过表头行）。"""
    records = []
    for row in re.findall(r"<tr[^>]*onclick=[^>]*>(.*?)</tr>", html_view, re.S):
        cells = [re.sub(r"<[^>]+>", "", td).strip()
                 for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < len(COLUMNS):
            continue
        rec = {col: cells[i] for i, col in enumerate(COLUMNS)}
        idm = re.search(r"detail\.html\?id=([A-Za-z0-9]+)", row)
        rec["detail_id"] = idm.group(1) if idm else ""
        rec["detail_url"] = (f"{BASE}/old_house/old_house_list_detail.html?id={rec['detail_id']}"
                             if rec["detail_id"] else "")
        records.append(rec)
    return records


def parse_paging(html_view):
    """解析总记录数 / 总页数。"""
    rc = re.search(r'id="RecordCount"[^>]*value="(\d+)"', html_view)
    pc = re.search(r'id="PageCount"[^>]*value="(\d+)"', html_view)
    return (int(rc.group(1)) if rc else None,
            int(pc.group(1)) if pc else None)


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- 断点续采调度器（多进程安全） ----------------
class ResumableScraper:
    def __init__(self, out_dir=DEFAULT_OUTDIR, state_path=DEFAULT_STATE,
                 delay=0.5, max_pages=0, cookie_retries=3, verbose=True, prefix="",
                 proxy_url=None, proxy_num=5):
        self.out_dir = out_dir
        self.state_path = state_path
        self.delay = delay
        self.max_pages = max_pages          # 0 = 每片抓完
        self.cookie_retries = cookie_retries
        self.verbose = verbose
        self.prefix = prefix
        self.proxy_url = proxy_url          # None=直连
        self.proxy_num = proxy_num
        self.state = {"version": STATE_VERSION, "districts": {}}
        os.makedirs(out_dir, exist_ok=True)
        self.load_state()

    def make_proxy_pool(self):
        """按配置建代理池（sticky：一个进程固定一个 IP，坏了才换）；
        拉取失败返回 None（直连兜底，不阻断采集）。"""
        if not self.proxy_url:
            return None
        pool = ProxyPool(self.proxy_url, num=self.proxy_num, verbose=self.verbose,
                         sticky=True)
        try:
            pool.fetch()
            self._log(f"[proxy] 本进程独立代理池: {[p['server'] for p in pool.items]}")
        except Exception as e:
            self._log(f"[proxy] 拉取代理失败: {e}，本次将直连兜底")
            return None
        return pool

    def _log(self, *a):
        if self.verbose:
            print((f"[{self.prefix}] " if self.prefix else "") + " ".join(str(x) for x in a))

    # ---------- 跨进程文件锁（mkdir 原子性） ----------
    def _acquire_lock(self, timeout=30):
        lockdir = self.state_path + ".lock"
        t0 = time.time()
        while True:
            try:
                os.mkdir(lockdir)
                return lockdir
            except FileExistsError:
                if time.time() - t0 > timeout:
                    raise TimeoutError(f"state 锁等待超时: {lockdir}")
                time.sleep(0.05)

    def _release_lock(self, lockdir):
        try:
            os.rmdir(lockdir)
        except OSError:
            pass

    # ---------- 状态读写 ----------
    def load_state(self, quiet=False):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                if isinstance(st, dict) and "districts" in st:
                    self.state = st
                    # 老状态迁移：无 done_pages 时按 next_page 推断已抓页
                    for name, sl in self.state["districts"].items():
                        if "done_pages" not in sl:
                            np_ = sl.get("next_page", 1)
                            sl["done_pages"] = list(range(1, np_))
                        if "detail_done" not in sl:
                            sl["detail_done"] = 0
                        if "detail_failed" not in sl:
                            sl["detail_failed"] = []
                        if "branch_done" not in sl:
                            sl["branch_done"] = 0
                        if "branch_failed" not in sl:
                            sl["branch_failed"] = []
                    if not quiet:
                        self._log(f"[state] 已载入断点状态: {len(st['districts'])} 个分片")
            except Exception as e:
                if not quiet:
                    self._log(f"[state] 状态文件损坏({e})，重新开始")

    def save_state(self):
        atomic_write_json(self.state_path, self.state)

    def slice_state(self, name):
        return self.state["districts"].setdefault(
            name, {"code": "", "total_pages": None, "total_records": None,
                   "next_page": 1, "records": 0, "done_pages": [],
                   "failed_pages": [], "detail_done": 0, "detail_failed": [],
                   "branch_done": 0, "branch_failed": [],
                   "done": False})

    def _update_state_locked(self, name, updater):
        """锁内：读最新 state -> 改指定片 -> 原子写回。返回 updater 结果。"""
        lock = self._acquire_lock()
        try:
            self.load_state(quiet=True)          # 用文件最新值刷新内存视图
            sl = self.slice_state(name)
            result = updater(sl)
            self.save_state()
            return result
        finally:
            self._release_lock(lock)

    def reset_fresh(self, names=None):
        """--fresh：重置状态 + 清空对应分片的数据文件。"""
        lock = self._acquire_lock()
        try:
            self.load_state(quiet=True)
            if names is None:
                self.state["districts"] = {}
            else:
                for n in names:
                    self.state["districts"].pop(n, None)
            self.save_state()
        finally:
            self._release_lock(lock)
        if names:
            for n in names:
                for ext in (".jsonl", ".csv", ".json"):
                    p = os.path.join(self.out_dir, n + ext)
                    if os.path.exists(p):
                        os.remove(p)

    # ---------- 分片数据文件（页级并发多 worker 写同一片，需文件锁） ----------
    def _append_records(self, name, page, recs):
        """页成功后：JSONL + CSV 增量追加落盘（片数据文件锁保护）。"""
        lockdir = os.path.join(self.out_dir, f"{name}.jsonl.lock")
        self._acquire_file_lock(lockdir)
        try:
            jl = os.path.join(self.out_dir, f"{name}.jsonl")
            with open(jl, "a", encoding="utf-8") as f:
                for r in recs:
                    r2 = dict(r)
                    r2["page"] = page
                    f.write(json.dumps(r2, ensure_ascii=False) + "\n")
            csvp = os.path.join(self.out_dir, f"{name}.csv")
            is_new = not os.path.exists(csvp)
            with open(csvp, "a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if is_new:
                    w.writeheader()
                for r in recs:
                    w.writerow({**r, "page": page})
        finally:
            self._release_file_lock(lockdir)

    def _acquire_file_lock(self, lockdir, timeout=30):
        t0 = time.time()
        while True:
            try:
                os.mkdir(lockdir)
                return
            except FileExistsError:
                if time.time() - t0 > timeout:
                    raise TimeoutError(f"文件锁等待超时: {lockdir}")
                time.sleep(0.02)

    def _release_file_lock(self, lockdir):
        try:
            os.rmdir(lockdir)
        except OSError:
            pass

    def _write_slice_json(self, name):
        """片完成后生成完整 JSON 数组（兼容旧 house_data.json 格式）。"""
        jl = os.path.join(self.out_dir, f"{name}.jsonl")
        if not os.path.exists(jl):
            return
        rows = []
        with open(jl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        atomic_write_json(os.path.join(self.out_dir, f"{name}.json"), rows)
        return len(rows)

    # ---------- 单页抓取（页粒度重试 + cookie 失效重建） ----------
    def _try_page(self, fd, name, page, filters):
        """抓一页并落盘。成功 True；普通失败退避重试 3 次；
        SessionExpiredError 则 rebootstrap 后重试当前页（cookie_retries 次）。"""

        def commit(recs, total_rc, total_pc, rebuild=False):
            self._append_records(name, page, recs)
            sl_ref = [None]

            def updater(sl):
                dp = sl.setdefault("done_pages", [])
                if page not in dp:                      # 防御：同页重复提交不重复计数
                    dp.append(page)
                    sl["records"] += len(recs)
                if total_rc is not None:
                    sl["total_records"] = total_rc
                if total_pc is not None:
                    sl["total_pages"] = total_pc
                sl["next_page"] = max(sl.get("next_page", 1), page + 1)  # 兼容字段
                if page in sl["failed_pages"]:
                    sl["failed_pages"].remove(page)
                sl_ref[0] = sl
            self._update_state_locked(name, updater)
            sl = sl_ref[0] or self.slice_state(name)
            self._log(f"    [{name}] page {page} +{len(recs)} 条 "
                      f"(累计 {sl['records']}{'/' + str(sl['total_records']) if sl['total_records'] else ''}"
                      f"{'，重建后' if rebuild else ''})")
            return True

        for attempt in range(1, 4):
            try:
                resp, _signed = fd.select_old_house(page=page, **filters)
                html_view = resp.json().get("htmlView", "") if resp.status_code == 200 else ""
                if resp.status_code != 200 or not html_view.strip():
                    raise RuntimeError(f"status={resp.status_code} 无 htmlView")
                recs = parse_house_table(html_view)
                total_rc, total_pc = parse_paging(html_view)
                return commit(recs, total_rc, total_pc)
            except SessionExpiredError:
                self._log(f"    [{name}] page {page} 会话失效，重建环境后重试当前页 ...")
                for r in range(1, self.cookie_retries + 1):
                    try:
                        fd.rebootstrap()
                        resp, _signed = fd.select_old_house(page=page, **filters)
                        if resp.status_code != 200:
                            raise SessionExpiredError(f"status={resp.status_code}")
                        html_view = resp.json().get("htmlView", "")
                        if not html_view.strip():
                            raise SessionExpiredError("无 htmlView")
                        recs = parse_house_table(html_view)
                        total_rc, total_pc = parse_paging(html_view)
                        return commit(recs, total_rc, total_pc, rebuild=True)
                    except SessionExpiredError:
                        wait = 1.0 * r
                        self._log(f"    [{name}] 重建第 {r} 次仍失效，{wait:.0f}s 后重试")
                        time.sleep(wait)
                return False
            except Exception as e:
                wait = 1.5 * attempt
                self._log(f"    [{name}] page {page} 失败: {e}，{wait:.1f}s 后重试({attempt}/3)")
                time.sleep(wait)
        return False

    # ---------- 单个分片 ----------
    def _scrape_slice(self, fd, name, code, extra_params):
        def set_code(sl):
            sl["code"] = code
        self._update_state_locked(name, set_code)
        sl = self.slice_state(name)
        filters = {"district": code}
        filters.update(extra_params or {})
        self._log(f"[slice] {name} (code={code}) 从 page {sl['next_page']} 开始"
                  f"{'，总页数 ' + str(sl['total_pages']) if sl['total_pages'] else ''}")

        # 1) 优先补采上次失败的页
        for pg in list(sl["failed_pages"]):
            self._log(f"    [recover] 补采失败页 {pg}")
            if self._try_page(fd, name, pg, filters) and self.delay:
                time.sleep(self.delay)

        # 2) 正常翻页（自己片是唯一写者，内存读安全）
        page = self.slice_state(name)["next_page"]
        while True:
            if self.max_pages and page > self.max_pages:
                self._log(f"    [stop] 达到分片页数上限 max_pages={self.max_pages}")
                break
            sl = self.slice_state(name)
            if sl["total_pages"] and page > sl["total_pages"]:
                break
            ok = self._try_page(fd, name, page, filters)
            if not ok:
                def mark_failed(sl2):
                    if page not in sl2["failed_pages"]:
                        sl2["failed_pages"].append(page)
                self._update_state_locked(name, mark_failed)
                self._log(f"    [warn] page {page} 多次重试仍失败，记入 failed_pages，先跳过")
            page += 1
            if self.delay:
                time.sleep(self.delay)

        # 3) 片完成判定 + 生成 JSON
        def finalize(sl2):
            sl2["done"] = bool(sl2["total_pages"] and sl2["next_page"] > sl2["total_pages"]
                               and not sl2["failed_pages"])
            return dict(sl2)
        sl = self._update_state_locked(name, finalize)
        if sl["done"]:
            n = self._write_slice_json(name)
            self._log(f"    [slice] {name} 完成：{sl['records']} 条 -> {name}.jsonl/.csv/.json"
                      f"{('（json 汇总 ' + str(n) + ' 条）') if n else ''}")
        else:
            self._log(f"    [slice] {name} 暂停：已到 page {sl['next_page'] - 1}，"
                      f"失败页 {sl['failed_pages']}（重跑本命令自动续采/补采）")

    # ---------- 详情页数据采集 ----------
    def _append_detail(self, name, detail_id, data):
        """详情 JSON 追加到 <name>_detail.jsonl（片数据文件锁保护）。"""
        dj = os.path.join(self.out_dir, f"{name}_detail.jsonl")
        lockdir = dj + ".lock"
        self._acquire_file_lock(lockdir)
        try:
            row = {"detail_id": detail_id}
            row.update({k: v for k, v in data.items() if k != "detail_id"})
            with open(dj, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        finally:
            self._release_file_lock(lockdir)

    def _try_detail(self, fd, name, detail_id, detail_delay=0.1):
        """抓一条详情（selectOldHouseInfo.action 签名请求）。
        会话失效自动 rebootstrap 后重试该条。"""
        def commit(data):
            self._append_detail(name, detail_id, data)
            def updater(sl):
                sl["detail_done"] = sl.get("detail_done", 0) + 1
                if detail_id in sl.get("detail_failed", []):
                    sl["detail_failed"].remove(detail_id)
            self._update_state_locked(name, updater)
            if detail_delay:
                time.sleep(detail_delay)
            return True

        for attempt in range(1, 4):
            try:
                resp, _s = fd.call("POST", "/oldhouse/selectOldHouseInfo.action",
                                   {"salebrandid": detail_id})
                if resp.status_code != 200:
                    raise RuntimeError(f"status={resp.status_code}")
                data = resp.json()
                if not isinstance(data, dict) or not data.get("salebrandid"):
                    raise RuntimeError("详情响应异常")
                return commit(data)
            except SessionExpiredError:
                self._log(f"    [{name}] 详情 {detail_id} 会话失效，重建后重试 ...")
                for r in range(1, self.cookie_retries + 1):
                    try:
                        fd.rebootstrap()
                        resp, _s = fd.call("POST", "/oldhouse/selectOldHouseInfo.action",
                                           {"salebrandid": detail_id})
                        if resp.status_code != 200:
                            raise SessionExpiredError(f"status={resp.status_code}")
                        data = resp.json()
                        if not isinstance(data, dict) or not data.get("salebrandid"):
                            raise SessionExpiredError("详情响应异常")
                        return commit(data)
                    except SessionExpiredError:
                        wait = 1.0 * r
                        self._log(f"    [{name}] 详情重建第 {r} 次仍失效，{wait:.0f}s 后重试")
                        time.sleep(wait)
                return False
            except Exception as e:
                wait = 1.5 * attempt
                self._log(f"    [{name}] 详情 {detail_id} 失败: {e}，{wait:.1f}s 后重试({attempt}/3)")
                time.sleep(wait)
        def mark_failed(sl):
            if detail_id not in sl.get("detail_failed", []):
                sl["detail_failed"].append(detail_id)
        self._update_state_locked(name, mark_failed)
        return False

    def _build_detail_tasks(self, targets):
        """从各片列表 jsonl 提取 detail_id，排除已抓详情，生成任务列表（失败的优先）。"""
        tasks = []
        for name in targets:
            jl = os.path.join(self.out_dir, f"{name}.jsonl")
            if not os.path.exists(jl):
                continue
            ids, seen = [], set()
            with open(jl, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        did = json.loads(line).get("detail_id")
                    except Exception:
                        continue
                    if did and did not in seen:
                        seen.add(did)
                        ids.append(did)
            done_ids = set()
            dj = os.path.join(self.out_dir, f"{name}_detail.jsonl")
            if os.path.exists(dj):
                with open(dj, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                done_ids.add(json.loads(line).get("detail_id"))
                            except Exception:
                                pass
            sl = self.slice_state(name)
            failed = [i for i in sl.get("detail_failed", []) if i not in done_ids]
            rest = [i for i in ids if i not in done_ids and i not in set(failed)]
            tasks.extend([(name, i) for i in failed + rest])
        self._log(f"[detail] 生成详情任务 {len(tasks)} 条")
        return tasks

    # ---------- 挂牌列表采集（多挂牌门店名/电话补全） ----------
    def _append_branch(self, name, hid, listings):
        """挂牌列表追加到 <name>_branch.jsonl（片数据文件锁保护）。"""
        bj = os.path.join(self.out_dir, f"{name}_branch.jsonl")
        lockdir = bj + ".lock"
        self._acquire_file_lock(lockdir)
        try:
            row = {"oldhouseid": hid, "listings": listings}
            with open(bj, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        finally:
            self._release_file_lock(lockdir)

    def _parse_branch_rows(self, hv):
        """解析挂牌列表 htmlView：返回 [{applyinspectno, branch_name, branch_phone, release_id}]。"""
        out = []
        for tr in re.findall(r"<tr[^>]*onclick[^>]*>(.*?)</tr>", hv, re.S):
            cells = [re.sub(r"<[^>]+>", "", td).strip()
                     for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) < 3:
                continue
            link = re.search(r"id=(\d+)", tr)
            out.append({"applyinspectno": cells[0], "branch_name": cells[1],
                        "branch_phone": cells[2],
                        "release_id": link.group(1) if link else ""})
        return out

    def _try_branch(self, fd, name, hid, delay=0.1):
        """抓一个房屋的全部挂牌列表（selectNewOldHouse.action，分页）。
        会话失效自动 rebootstrap 后重试。"""
        def commit(listings):
            self._append_branch(name, hid, listings)
            def updater(sl):
                sl["branch_done"] = sl.get("branch_done", 0) + 1
                if hid in sl.get("branch_failed", []):
                    sl["branch_failed"].remove(hid)
            self._update_state_locked(name, updater)
            if delay:
                time.sleep(delay)
            return True

        def fetch_all():
            listings, page = [], 1
            while True:
                resp, _s = fd.call("POST", "/oldhouse/selectNewOldHouse.action",
                                   {"oldhouseid": hid, "currentPage": str(page)})
                if resp.status_code != 200:
                    raise RuntimeError(f"status={resp.status_code}")
                data = resp.json()
                hv = data.get("htmlView", "")
                listings.extend(self._parse_branch_rows(hv))
                pc = re.search(r'id="PageCount"[^>]*value="(\d+)"', hv)
                total_pages = int(pc.group(1)) if pc else 1
                if page >= total_pages or not listings:
                    break
                page += 1
            if not listings:
                raise RuntimeError("挂牌列表为空")
            return listings

        for attempt in range(1, 4):
            try:
                return commit(fetch_all())
            except SessionExpiredError:
                self._log(f"    [{name}] 挂牌 {hid} 会话失效，重建后重试 ...")
                for r in range(1, self.cookie_retries + 1):
                    try:
                        fd.rebootstrap()
                        return commit(fetch_all())
                    except SessionExpiredError:
                        wait = 1.0 * r
                        self._log(f"    [{name}] 挂牌重建第 {r} 次仍失效，{wait:.0f}s 后重试")
                        time.sleep(wait)
                return False
            except Exception as e:
                wait = 1.5 * attempt
                self._log(f"    [{name}] 挂牌 {hid} 失败: {e}，{wait:.1f}s 后重试({attempt}/3)")
                time.sleep(wait)
        def mark_failed(sl):
            if hid not in sl.get("branch_failed", []):
                sl["branch_failed"].append(hid)
        self._update_state_locked(name, mark_failed)
        return False

    def _build_branch_tasks(self, targets):
        """从各片列表 jsonl 提取唯一 oldhouseid（需从详情 join？列表没有 oldhouseid，
        从 _detail.jsonl 提取 detail_id->oldhouseid 映射），排除已抓，生成任务。"""
        tasks = []
        for name in targets:
            dj = os.path.join(self.out_dir, f"{name}_detail.jsonl")
            if not os.path.exists(dj):
                continue
            hids, seen = [], set()
            with open(dj, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    hid = row.get("oldhouseid")
                    if hid and hid not in seen:
                        seen.add(hid)
                        hids.append(hid)
            done = set()
            bj = os.path.join(self.out_dir, f"{name}_branch.jsonl")
            if os.path.exists(bj):
                with open(bj, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                done.add(json.loads(line).get("oldhouseid"))
                            except Exception:
                                pass
            sl = self.slice_state(name)
            failed = [h for h in sl.get("branch_failed", []) if h not in done]
            rest = [h for h in hids if h not in done and h not in set(failed)]
            tasks.extend([(name, h) for h in failed + rest])
        self._log(f"[branch] 生成挂牌任务 {len(tasks)} 条")
        return tasks

    # ---------- 单进程页级循环 ----------
    def _run_sequential(self, targets, dic, extra_params, with_detail, detail_delay,
                        with_branch=False):
        pool = self.make_proxy_pool()
        with FangdiRuishu(proxy_pool=pool) as fd:
            # 阶段 A/B：列表页
            for name in targets:
                sl = self.slice_state(name)
                if sl.get("done") and not sl.get("failed_pages"):
                    self._log(f"[skip] {name} 已完成，跳过")
                    continue
                self._update_state_locked(name, lambda s: s.update(code=dic[name]))
                filters = {"district": dic[name]}
                filters.update(extra_params or {})
                page_tasks = self._build_page_tasks(name)
                self._log(f"[slice] {name} 待抓 {len(page_tasks)} 页")
                for page in page_tasks:
                    self._try_page(fd, name, page, filters)
                    if self.delay:
                        time.sleep(self.delay)
            # 阶段 C：详情
            if with_detail:
                for (name, detail_id) in self._build_detail_tasks(targets):
                    self._try_detail(fd, name, detail_id, detail_delay)
            # 阶段 D：挂牌列表
            if with_branch:
                for (name, hid) in self._build_branch_tasks(targets):
                    self._try_branch(fd, name, hid, detail_delay)
        self._report_detail_summary()
        if with_branch:
            done_b = sum(s.get("branch_done", 0) for s in self.state["districts"].values())
            failed_b = sum(len(s.get("branch_failed", []))
                           for s in self.state["districts"].values())
            self._log(f"[branch] 挂牌已抓 {done_b} 个房屋"
                      f"{'，失败 ' + str(failed_b) + ' 个（重跑自动补采）' if failed_b else ''}")

    def _build_page_tasks(self, name):
        """按 done_pages/total_pages 生成该片未完成页任务（失败页优先）。"""
        sl = self.slice_state(name)
        done = set(sl.get("done_pages") or [])
        failed = set(sl.get("failed_pages") or [])
        tasks = sorted(failed - done)
        if sl.get("total_pages"):
            end = sl["total_pages"] if not self.max_pages else min(sl["total_pages"], self.max_pages)
            tasks += [p for p in range(1, end + 1) if p not in done and p not in failed]
        elif self.max_pages:
            tasks += [p for p in range(1, self.max_pages + 1) if p not in done]
        return tasks

    def _report_detail_summary(self):
        done = sum(s.get("detail_done", 0) for s in self.state["districts"].values())
        failed = sum(len(s.get("detail_failed", [])) for s in self.state["districts"].values())
        self._log(f"[detail] 详情已抓 {done} 条"
                  f"{'，失败 ' + str(failed) + ' 条（重跑自动补采）' if failed else ''}")

    # ---------- 主入口（单进程 / 多进程） ----------
    def _resolve_targets(self, districts, dic):
        if districts:
            missing = [d for d in districts if d not in dic]
            if missing:
                raise SystemExit(f"未知区县 {missing}，可选: {sorted(dic)}")
            return [d for d in districts]
        return [d for d in dic if dic[d]]     # 排除 code 为 0/空的占位

    def run(self, districts=None, extra_params=None, workers=1,
            with_detail=False, detail_delay=0.1, with_branch=False):
        """districts: None=全部区县分片；list=指定分片。
        workers>1 走多进程页级并发（3 片也能满 10 并发）；with_detail 采集详情；
        with_branch 采集挂牌列表（多挂牌门店名/电话补全）。"""
        pool = self.make_proxy_pool()
        with FangdiRuishu(proxy_pool=pool) as fd:
            dic = self._get_districts_safe(fd)
        targets = self._resolve_targets(districts, dic)
        self._log(f"[plan] 分片列表({len(targets)}): {targets}")

        if workers > 1:
            return self._run_parallel_pages(targets, dic, extra_params, workers,
                                            with_detail, detail_delay, with_branch)
        return self._run_sequential(targets, dic, extra_params, with_detail, detail_delay,
                                    with_branch)

    def _run_parallel_pages(self, targets, dic, extra_params, workers,
                            with_detail, detail_delay, with_branch=False):
        nproc = max(1, workers)
        # 过滤已完成片
        pending = [n for n in targets
                   if not (self.slice_state(n).get("done")
                           and not self.slice_state(n).get("failed_pages")
                           and not (with_detail and self._slice_has_pending_detail(n)))]
        if not pending:
            self._log("[plan] 全部分片及详情已完成，无需采集")
            return 0
        self._log(f"[plan] 待采集分片 {len(pending)} 个，启用 {nproc} 个并发 worker（页级并发）")

        shared = {
            "out_dir": self.out_dir, "state_path": self.state_path,
            "delay": self.delay, "max_pages": self.max_pages,
            "cookie_retries": self.cookie_retries, "extra": extra_params or {},
            "verbose": self.verbose,
            "proxy_url": self.proxy_url, "proxy_num": self.proxy_num,
            "detail_delay": detail_delay,
        }
        try:
            with mp.Pool(processes=nproc, initializer=_worker_init,
                         initargs=(shared,)) as pool:
                # 阶段 A：探页（无 total_pages 且 page 1 未抓的片先抓 page 1）
                probe = [(n, dic[n], 1) for n in pending
                         if not self.slice_state(n).get("total_pages")
                         and 1 not in set(self.slice_state(n).get("done_pages") or [])]
                if probe:
                    self._log(f"[phase A] 探页任务 {len(probe)} 个")
                    pool.map(_worker_page, probe)
                    self.load_state(quiet=True)

                # 阶段 B：全量页任务
                page_tasks = []
                for n in pending:
                    page_tasks.extend([(n, dic[n], p) for p in self._build_page_tasks(n)])
                self._log(f"[phase B] 列表页任务 {len(page_tasks)} 个")
                if page_tasks:
                    pool.map(_worker_page, page_tasks)
                    self.load_state(quiet=True)

                # 阶段 C：详情任务
                if with_detail:
                    detail_tasks = self._build_detail_tasks(pending)
                    if detail_tasks:
                        self._log(f"[phase C] 详情任务 {len(detail_tasks)} 个")
                        pool.map(_worker_detail, detail_tasks)
                        self.load_state(quiet=True)

                # 阶段 D：挂牌列表任务（多挂牌门店名/电话补全）
                if with_branch:
                    branch_tasks = self._build_branch_tasks(pending)
                    if branch_tasks:
                        self._log(f"[phase D] 挂牌任务 {len(branch_tasks)} 个")
                        pool.map(_worker_branch, branch_tasks)
                        self.load_state(quiet=True)
        except KeyboardInterrupt:
            self._log("\n[interrupt] 已终止 worker，断点状态已逐页落盘，重跑自动续采。")
            sys.exit(130)

        self._report_parallel(pending)
        return 0

    def _slice_has_pending_detail(self, name):
        dj = os.path.join(self.out_dir, f"{name}_detail.jsonl")
        done_ids = set()
        if os.path.exists(dj):
            with open(dj, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            done_ids.add(json.loads(line).get("detail_id"))
                        except Exception:
                            pass
        jl = os.path.join(self.out_dir, f"{name}.jsonl")
        if not os.path.exists(jl):
            return False
        for line in open(jl, "r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                did = json.loads(line).get("detail_id")
            except Exception:
                continue
            if did and did not in done_ids:
                return True
        return False

    def _report_parallel(self, pending):
        failed_pages = {n: s["failed_pages"] for n, s in self.state["districts"].items()
                        if s["failed_pages"]}
        self._log(f"\n[DONE] 分片 {pending}")
        if failed_pages:
            self._log(f"[FAILED] 页级失败: {failed_pages}（重跑本命令自动补采）")
        self._report_detail_summary()

    def _get_districts_safe(self, fd):
        for _ in range(self.cookie_retries + 1):
            try:
                return fd.get_districts()
            except SessionExpiredError:
                fd.rebootstrap()
        raise SystemExit("获取区县字典失败：会话反复失效，请稍后重试")


# ---------------- 多进程 worker ----------------
_G = {}


def _worker_init(shared):
    """Pool initializer：每个 worker 建独立会话（独立 iv8 上下文/cookie）+
    独立代理池（sticky 固定一个 IP，与其它 worker 隔离）。"""
    _G["cfg"] = shared
    pool = None
    if shared.get("proxy_url"):
        pool = ProxyPool(shared["proxy_url"], num=shared.get("proxy_num", 5),
                         verbose=shared.get("verbose", False), sticky=True)
        try:
            pool.fetch()
            print(f"[proxy] worker#{os.getpid() % 1000} 独立代理池: "
                  f"{[p['server'] for p in pool.items]}")
        except Exception as e:
            print(f"[proxy] worker 拉取代理失败: {e}，将直连兜底")
            pool = None
    fd = FangdiRuishu(verbose=shared.get("verbose", False), proxy_pool=pool)
    fd.bootstrap()
    _G["fd"] = fd


def _worker_slice_ctx(task):
    """worker 内复用 ResumableScraper（缓存，避免每任务 load_state）。"""
    sc = _G.get("sc")
    if sc is None:
        cfg = _G["cfg"]
        sc = ResumableScraper(out_dir=cfg["out_dir"], state_path=cfg["state_path"],
                              delay=cfg["delay"], max_pages=cfg["max_pages"],
                              cookie_retries=cfg["cookie_retries"],
                              verbose=cfg["verbose"],
                              prefix=f"W{os.getpid() % 1000}")
        _G["sc"] = sc
    return sc


def _worker_page(task):
    """页级任务：抓列表一页。返回 (name, page, ok, err)。"""
    name, code, page = task
    sc = _worker_slice_ctx(task)
    filters = {"district": code}
    filters.update(_G["cfg"]["extra"])
    try:
        ok = sc._try_page(_G["fd"], name, page, filters)
        return (name, page, ok, "" if ok else "多次重试失败")
    except Exception as e:
        return (name, page, False, str(e))


def _worker_detail(task):
    """详情任务：抓一条房源详情。返回 (name, detail_id, ok, err)。"""
    name, detail_id = task
    sc = _worker_slice_ctx(task)
    try:
        ok = sc._try_detail(_G["fd"], name, detail_id,
                            _G["cfg"].get("detail_delay", 0.1))
        return (name, detail_id, ok, "" if ok else "多次重试失败")
    except Exception as e:
        return (name, detail_id, False, str(e))


def _worker_branch(task):
    """挂牌任务：抓一个房屋的全部挂牌（门店名/电话）。返回 (name, hid, ok, err)。"""
    name, hid = task
    sc = _worker_slice_ctx(task)
    try:
        ok = sc._try_branch(_G["fd"], name, hid, _G["cfg"].get("detail_delay", 0.1))
        return (name, hid, ok, "" if ok else "多次重试失败")
    except Exception as e:
        return (name, hid, False, str(e))


def merge_all(out_dir, out_json="house_data.json", out_csv="house_data.csv"):
    """合并 out_dir 下所有分片：列表 jsonl 与详情 jsonl 按 detail_id join
    （详情字段加 d_ 前缀），挂牌列表按核验编号 join（门店名/电话，b_ 前缀）。
    输出单文件 JSON + CSV。"""
    # 1) 读详情
    details = {}
    for f in sorted(os.listdir(out_dir)):
        if not f.endswith("_detail.jsonl"):
            continue
        with open(os.path.join(out_dir, f), "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("detail_id"):
                    details[row["detail_id"]] = row
    # 2) 读挂牌列表：applyinspectno -> {branch_name, branch_phone}
    branches = {}
    for f in sorted(os.listdir(out_dir)):
        if not f.endswith("_branch.jsonl"):
            continue
        with open(os.path.join(out_dir, f), "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                for lst in row.get("listings", []):
                    key = str(lst.get("applyinspectno", "")).lstrip("0")
                    branches[key] = {"b_branch_name": lst.get("branch_name", ""),
                                     "b_branch_phone": lst.get("branch_phone", ""),
                                     "b_release_id": lst.get("release_id", "")}
    # 3) 列表行 + join
    rows = []
    field_set = set()
    for jl in sorted(os.listdir(out_dir)):
        if not jl.endswith(".jsonl") or jl.endswith("_detail.jsonl") or jl.endswith("_branch.jsonl"):
            continue
        with open(os.path.join(out_dir, jl), "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                d = details.get(r.get("detail_id"))
                if d:
                    for k, v in d.items():
                        if k != "detail_id":
                            r["d_" + k] = v
                    br = branches.get(str(d.get("applyinspectid", "")).lstrip("0"))
                    if br:
                        r.update(br)
                field_set.update(r.keys())
                rows.append(r)
    if not rows:
        print("[merge] 无数据可合并")
        return
    atomic_write_json(out_json, rows)
    fields = ["district", "location", "house_type", "orientation", "total_floor",
              "area", "detail_id", "detail_url", "page"]
    fields += sorted(k for k in field_set if k not in fields)
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    joined = sum(1 for r in rows if any(k.startswith("d_") for k in r))
    branched = sum(1 for r in rows if "b_branch_name" in r)
    print(f"[merge] 合并 {len(rows)} 条（详情 {joined}，门店补全 {branched}）"
          f" -> {out_json} / {out_csv}")


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description="fangdi 二手房源分片断点续采（支持多进程并发）")
    ap.add_argument("--districts", default=None,
                    help="区县列表（逗号分隔），如 黄浦区,徐汇区；缺省=全部区县分片")
    ap.add_argument("--district", default=None, help="兼容旧参数：单区县（等价 --districts 黄浦区）")
    ap.add_argument("--pages", type=int, default=0, help="每片最多抓取页数，0=抓完该片（默认 0）")
    ap.add_argument("--delay", type=float, default=0.5, help="每页间隔秒数（默认 0.5）")
    ap.add_argument("--workers", type=int, default=1, help="并发 worker 数（默认 1；每个 worker 独立会话）")
    ap.add_argument("--param", action="append", default=[], metavar="K=V", help="附加过滤参数，可重复")
    ap.add_argument("--out-dir", default=DEFAULT_OUTDIR, help="分片输出目录（默认 out/）")
    ap.add_argument("--state", default=DEFAULT_STATE, help="断点状态文件（默认 scrape_state.json）")
    ap.add_argument("--cookie-retries", type=int, default=3,
                    help="cookie 失效时单页最多重建环境次数（默认 3）")
    ap.add_argument("--proxy-url", default=DEFAULT_PROXY_URL,
                    help="代理提取接口 URL（默认 qg.net 共享代理；设空字符串=禁用）")
    ap.add_argument("--proxy-num", type=int, default=5,
                    help="每次/每个 worker 拉取的代理数量（默认 5）")
    ap.add_argument("--no-proxy", action="store_true", help="禁用代理，全部直连")
    ap.add_argument("--detail", action="store_true", help="采集房源详情数据（selectOldHouseInfo.action）")
    ap.add_argument("--detail-delay", type=float, default=0.1, help="每条详情间隔秒数（默认 0.1）")
    ap.add_argument("--branch", action="store_true",
                    help="采集挂牌列表（selectNewOldHouse.action：多挂牌门店名/电话补全）")
    ap.add_argument("--fresh", action="store_true", help="忽略断点状态，从头重抓（并清空分片数据文件）")
    ap.add_argument("--merge", action="store_true", help="结束后合并全部分片为 house_data.json/csv")
    ap.add_argument("--quiet", action="store_true", help="减少日志输出")
    args = ap.parse_args()

    extra = {}
    for kv in args.param:
        if "=" in kv:
            k, v = kv.split("=", 1)
            extra[k] = v

    districts = None
    if args.districts:
        districts = [d.strip() for d in args.districts.split(",") if d.strip()]
    elif args.district:
        districts = [args.district]

    scraper = ResumableScraper(out_dir=args.out_dir, state_path=args.state,
                               delay=args.delay, max_pages=args.pages,
                               cookie_retries=args.cookie_retries,
                               verbose=not args.quiet,
                               proxy_url=None if args.no_proxy else (args.proxy_url or None),
                               proxy_num=args.proxy_num)
    if args.fresh:
        print("[fresh] 重置断点状态与分片数据文件 ...")
        scraper.reset_fresh(districts)

    try:
        scraper.run(districts=districts, extra_params=extra, workers=args.workers,
                    with_detail=args.detail, detail_delay=args.detail_delay,
                    with_branch=args.branch)
    except KeyboardInterrupt:
        scraper.save_state()
        print("\n[interrupt] 已保存断点状态，重跑本命令自动续采。")
        sys.exit(130)

    if args.merge:
        merge_all(args.out_dir)


if __name__ == "__main__":
    mp.freeze_support()
    main()
