# fangdi.com.cn 瑞数6 全流程分析（2026-10 实测验证版）

> 本文档基于本机实测（iv8 0.1.4 / Python 3.9）重跑验证后整理，所有证据可复现。
> 验证命令：`python fangdi_iv8.py`、`python fangdi_stage2.py`、`python fangdi_batch.py --district 黄浦区 --pages 1`

---

## 一、目标与数据接口

| 项目 | 内容 |
|------|------|
| 站点 | 上海市官方房地产交易平台 https://www.fangdi.com.cn |
| 列表页 | GET `/old_house/old_house.html`（二手房源挂牌列表） |
| 数据接口 | POST `/oldhouse/selectOldHouse.action` |
| 辅助接口 | POST `/oldhouse/getDic.action`（区县字典 {名称: code}） |
| 防护 | 瑞数动态防护（Rivers），本站特征为"两阶段挑战" |

`selectOldHouse.action` 请求体（application/x-www-form-urlencoded）：

```
district, area, location, price, region, blockName, time,
houseType, listingNo, verifyNo, currentPage
```

返回 JSON：`{"htmlView": "<table>…10条/页…</table>", "RecordCount": …, "PageCount": …}`。
全量约 28 万条（实测黄浦区 7303 条 / 731 页）。

---

## 二、瑞数6 防护机制（本站实测观察）

### 2.1 第一阶段：会话建立（412 挑战）

```
GET /old_house/old_house.html（无有效 cookie）
  └─► 412 + 挑战页（3012B），包含：
        ├─ <meta id="2vmka0flZgDo" content="…" r='m'>    ← 瑞数动态 meta（防重放绑定）
        ├─ 内联 $_ts 初始化：$_ts.nsd=126106; $_ts.cd="混淆串…"  ← 环境检测配置
        ├─ 外联 VM JS：/jhKGIvczkKbR/8WwVutQAyu6q.0515a6f.js（约 205KB，文件名/目录随机）
        └─ Set-Cookie: UA1L1zGonajvO=<初始动态值>          ← 第一层 cookie（"O"段）
```

要点：
- 目录名 `jhKGIvczkKbR`、文件名 `8WwVutQAyu6q`、版本段 `.0515a6f` 全部动态，但外联 script 标签带 `r='m'` 属性，可稳定定位。
- 第一套 JS 是 VM 解释器（字节码执行 + 环境指纹采集 + 行为检测），在浏览器里执行后：
  1. 采集/校验环境指纹（UA、时区、语言、DOM 痕迹等，由 `$_ts.cd` 配置驱动）；
  2. 通过 XHR 把校验结果回传（netLog 可见两次 `GET /old_house/old_house.html`）；
  3. 服务器判定通过后下发第二层 cookie `UA1L1zGonajvP=…`（"P"段，通过凭证）。
- 携带 P cookie 重试 → **200 真实页面**（61KB）。

### 2.2 第二阶段：接口签名（页面内二次挑战）

```
200 页面（61KB）内含第二套瑞数挑战：
  ├─ 新的内联 $_ts 配置
  └─ 外联 VM JS：/jhKGIvczkKbR/jNafbsZdWo4R.0515a6f.js（约 157KB）
      → 该 JS hook 了 XMLHttpRequest 的 open/send

业务代码 new XMLHttpRequest() 打 /oldhouse/selectOldHouse.action 时，
瑞数在发送前重写请求：
  ├─ URL 追加后缀：?XJlCTRRM=<动态签名>（参数名 XJlCTRRM 同样动态）
  └─ 请求头 Cookie 追加新的动态 cookie 段
```

关键性质：
- **签名与请求路径绑定，与 POST body 无关** → 同一套 hook 逻辑适配所有 `.action` 接口（getDic / selectOldHouse / getSaleHouse …）。
- **签名与时间/会话绑定** → 必须"生成即重放"，不可缓存复用（重放窗口极短）。
- 直接裸发（无后缀 / 无新 cookie）→ 412/403。

### 2.3 瑞数6 与旧版差异（本次观察）

- 新增动态 `<meta id=… r='m'>` 标签（2vmka0flZgDo），把 HTML 上下文参与绑定；
- 外联 JS 目录从固定路径改为随机目录（`/jhKGIvczkKbR/`），但 `r='m'` 属性锚点未变；
- 双层动态 cookie 命名同样动态（本站为 `UA1L1zGonajvO/P`），不可写死。

---

## 三、解决原理：为什么 iv8 补环境可行

瑞数两套 JS 的本质都是 **纯 JS 计算**（VM 字节码解释器 + 环境指纹采集 + hook 重写），
不需要真实渲染、不需要真实鼠标轨迹。因此：

```
真实浏览器里：挑战页 → JS 执行 → cookie/签名
iv8 里：      同一挑战页快照 + 同一 JS → 受控执行 → 相同 cookie/签名
```

iv8（C++ V8 + 浏览器环境）提供 window / DOM / BOM / XHR / canvas / crypto / timer /
eventLoop，用 `page.load(snapshot)` 注入挑战页（HTML + 外联 JS + Set-Cookie 头），
让瑞数 JS 在纯 Python 进程里跑出与浏览器等价的结果。

### 环境配置关键点（决定成败的画像）

| 配置项 | 值 | 原因 |
|--------|-----|------|
| `location.href` | 真实页面 URL | iv8 默认 `about:blank`，瑞数必检 |
| `navigator.userAgent` | Chrome/124 Win64 | 与 Python 重放 UA 一致 |
| `navigator.language/languages` | zh-CN / zh-CN,en-US,en | 语言一致性 |
| `navigator.platform` | Win32 | 与 UA 平台对齐 |
| `config.timezone` | Asia/Shanghai | 时区与语言、站点对齐 |
| 初始 cookie | 经 snapshot `headers: [["set-cookie", …]]` 注入 | 让挑战 JS 拿到服务器下发的 O cookie |

一致性红线：UA ↔ platform ↔ 时区 ↔ 语言整套对齐；Python 重放请求的 UA/Referer/Cookie
必须与 iv8 里的画像一致。

### 两阶段执行模型

1. **第一阶段（一次性）**：临时 JSContext 加载 412 页 + 第一套 JS → `eventLoop.sleep`
   推进定时器/XHR → `document.cookie` 长出 `UA1L1zGonajvP` → 合并进 Python session →
   重试得 200 页面。
2. **第二阶段（常驻）**：新 JSContext 加载 200 页 + 第二套 JS（安装 XHR hook），此后
   每次 `sign()`：
   ```js
   var xhr = new XMLHttpRequest();
   xhr.open(method, path);
   xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded;charset=utf-8");
   xhr.send(body);
   ```
   → `eventLoop.drain()` → 从 `__iv8__.netLog.entries` 捕获被瑞数重写后的 URL
   （带 `?XJlCTRRM=…`）→ 同步 `document.cookie` 最新动态段 → Python `requests`
   携签名 URL + cookie **立即重放**。

---

## 四、完整请求链路（时序）

```
Python session                    fangdi.com.cn
     │  1. GET /old_house/old_house.html
     │───────────────────────────────►  412 + $_ts + meta + VM JS#1
     │                                    + Set-Cookie: UA1L1zGonajvO=…
     │  2. GET /jhKGIvczkKbR/8WwVutQAyu6q.0515a6f.js
     │◄───────────────────────────────  205KB VM JS#1
     │
     │  ┌─ iv8: page.load(挑战页快照) ─ 执行 JS#1 ─ 指纹校验 ─ XHR 回传 ─┐
     │  │         document.cookie 长出 UA1L1zGonajvP=…（通过凭证）        │
     │  └─────────────────────────────────────────────────────────────┘
     │  3. GET 页面（带 O+P cookie）
     │───────────────────────────────►  200 真实页面（内含 $_ts + VM JS#2）
     │  4. GET /jhKGIvczkKbR/jNafbsZdWo4R.0515a6f.js
     │◄───────────────────────────────  157KB VM JS#2
     │
     │  ┌─ iv8（常驻）: 加载 200 页 + JS#2 → hook XHR ─┐
     │  └───────────────────────────────────────────┘
     │
     │  5. [每次调用] iv8 里 new XHR 打 /oldhouse/selectOldHouse.action
     │     → 瑞数重写 URL: ?XJlCTRRM=<签名> + 新动态 cookie
     │  6. Python 携签名 URL + 最新 cookie 重放
     │───────────────────────────────►  200 + 房源 JSON（htmlView）
```

---

## 五、实测验证结果（本次重跑）

```
[第一阶段] 首次请求 status=412；JS 生成 cookie：UA1L1zGonajvO=…; UA1L1zGonajvP=…
           netLog: GET /old_house/old_house.html ×2（校验回传）
           [4] 重试请求 status=200, body=61344B（真实页面）

[第二阶段] 200 页面 status=200 (61402B)；VM JS jNafbsZdWo4R.0515a6f.js (157119B)
           瑞数后缀(URL): /oldhouse/selectOldHouse.action?XJlCTRRM=0ByebualqWqpyyT0S…
           cookieHeader 长度 628
           => status=200，返回 {"htmlView":"<table…>…</table>"}

[端到端]  POST /oldhouse/getDic.action -> 200（862B，区县字典）
          POST /oldhouse/selectOldHouse.action -> 200（4884B）
          [page 1] +10 条，总页数 731，总记录 7303
          [DONE] house_data.json / house_data.csv
```

结论：现有方案在当前瑞数版本（0515a6f）下**全部有效**，无需修改。

---

## 六、代码索引与使用

| 文件 | 作用 |
|------|------|
| `fangdi_api.py` | 核心库 `FangdiRuishu`：两阶段 bootstrap + `sign()/call()` 签名器 + `rebootstrap()` 会话失效重建 + `SessionExpiredError` 检测 + `_request()` 代理重试出口 |
| `fangdi_batch.py` | 批量采集：按区县分片 + 断点续采（scrape_state.json）+ cookie 失效页级重试 + 多进程并发（`--workers`）+ IP 代理（日常入口） |
| `proxy_pool.py` | 共享代理池：拉取（qg.net 接口）/轮询/过期剔除/坏代理剔除/自动补充 |
| `fangdi_iv8.py` | 第一阶段演示（412→cookie→200） |
| `fangdi_stage2.py` | 第二阶段演示（签名→重放→200） |
| `console_fix.py` | Windows 控制台 GBK/UTF-8 输出修复 |
| `probe_stage1.py` | 挑战页结构探测（升级后先跑它看结构是否变化） |
| `probe_proxy.py` / `probe_proxy2.py` | 代理接口格式 / 代理连通性探测 |
| `test_resume_recover.py` | 断点续采 / 会话失效页级重建回归测试 |
| `test_proxy.py` | 坏代理轮换 / rebootstrap 换 IP 回归测试 |
| `old_house_200.html` | 第二阶段真实页面样本（分析证据） |

运行（必须用装了 iv8 的 Python 3.9）：

```bash
# 单进程分片断点续采
python fangdi_batch.py --districts 黄浦区 --pages 1

# 10 并发全量分片
python fangdi_batch.py --workers 10 --delay 1.0
```

### 6.1 分片断点续采 + 多进程并发架构

**数据模型**：以区县（`getDic.action` 字典，16 个）为分片单位，每片独立输出文件
`out/<区县>.jsonl/.csv`（页成功后增量追加）与进度状态。状态统一存 `scrape_state.json`：

```json
{"version": 2, "districts": {
  "黄浦区": {"code": "…", "total_pages": 731, "total_records": 7303,
            "next_page": 5, "records": 40, "failed_pages": [], "done": false}}}
```

**断点续采**：每页成功后「锁内读文件最新 state → 改本片 key → 原子写回」（临时文件 +
`os.replace`）。中断（含 Ctrl+C）后重跑同命令：从 `next_page` 继续、优先补采
`failed_pages`、已完成片（`done`）自动跳过。数据文件按页追加，页粒度幂等。

**cookie 失效页级重试**：`call()` 检测到 412/403/挑战页抛 `SessionExpiredError` →
`rebootstrap()`（清 cookie → 412 → 新 O/P cookie → 200 → 重装 XHR hook）→ **只重试当前页**，
不重抓已成功页面；重建次数 `--cookie-retries`（默认 3）。普通失败退避重试 3 次，
仍失败记入 `failed_pages` 下次补采。

**多进程并发（`--workers N`）**：iv8 单上下文不可多线程（签名必须串行且与 cookie 绑定），
故并发采用**多进程 + 每 worker 独立会话**：

```
主进程：bootstrap 一次拿区县字典 -> 过滤已完成片 -> 任务列表 [(片名, code), ...]
  └─ multiprocessing.Pool(N)
       ├─ worker 0: 独立 FangdiRuishu（独立 session/iv8/cookie/签名）
       │      bootstrap 一次，跨片复用 fd；片内串行翻页 + delay
       ├─ worker 1: 同上（处理下一个片任务）
       └─ ...
共享：scrape_state.json（mkdir 文件锁：读-改-原子写）；每片数据文件唯一写者，无需锁
```

关键设计取舍：

- **每 worker 独立会话**：10 worker = 10 个独立访客（独立 O/P cookie、独立签名序列），
  服务器端互不干扰，规避「单 cookie 并发签名竞态」问题；
- **worker 跨片复用 bootstrap**：16 片 10 worker 时只做 10 次 412 挑战（而非 16 次）；
- **Windows spawn 安全**：worker 入口为模块级函数 + `if __name__ == "__main__"` 保护；
- **内存开销 ≈ N × 一个 iv8 上下文（约 150–300MB）**，10 并发约 2–3GB；
- **频率**：并发度线性抬高请求速率（10 worker + delay 1.0 ≈ 10 req/s），
  全量 28.5 万条约 30–40 分钟；官方站建议 `--delay ≥ 0.5`。

实测（2026-10）：4 并发 × 4 新片、4 并发断点续采（各片从 page 2 继续）、
10 并发 × 全 16 分片任务轮转，state 锁无冲突、统计与数据全部正确。

### 6.3 页级并发 + 详情采集

**页级并发**（`--workers` > 1 时）：任务粒度从"片"细化为"页"，3 个区县也能满 10 并发：

```
阶段 A（探页）: 无 total_pages 的片先并发抓 page 1（拿到 RecordCount/PageCount）
阶段 B（列表）: 按 done_pages 集合生成全量未完成页任务 (name, code, page)
               Pool.map 分发，10 worker 各自独立会话/独立 IP 并行签名+重放
阶段 C（详情）: --detail 时，从各片 jsonl 提取 detail_id，排除已抓详情，
               生成任务 (name, detail_id) 并发抓取
```

断点模型从"next_page 游标"升级为 **done_pages 集合**（老 state 自动按 next_page 迁移），
每页成功锁内写 done_pages；失败页 failed_pages 下次优先补采；片数据文件 append
加文件锁（页级并发下多 worker 写同一片 jsonl）。

**详情数据链**（实测）：

```
列表记录 detail_id（如 e3b0c442…）
  └─ POST /oldhouse/selectOldHouseInfo.action   body: salebrandid=<detail_id>
       └─ 200 JSON，37 字段：salebrandid(挂牌号)/applyinspectid(核验编号)/
          brokercontractid(委托号)/price(总价)/buildingarea(建筑面积)/
          flatstyle(房型)/totalfloor(总楼层)/orientation(朝向)/
          fitment+decoration(装修)/landuse(用途)/finishdate(竣工)/
          institutionname+institutionphone(经纪机构及电话)/
          latitude+longitude(经纬度)/tempinspectdate(核验日期)/haslease 等
```

要点：
- 详情页 HTML（old_house_list_detail.html / old_house_release_detail.html）是**空壳中转页**，
  字段值由 JS 动态填充，真正的数据就在这个签名接口里，**一条签名请求拿到全部详情**，无需抓页面；
- 详情页响应是 UTF-8（meta 与内容一致），勿按 GBK 解码；
- 详情逐条任务化，落盘 `out/<区县>_detail.jsonl`，断点续采（detail_done 计数 +
  已抓 id 集合差集）；`--merge` 时按 detail_id join，详情字段加 `d_` 前缀。

**全量实测（2026-10，10 并发 + 代理 + 详情）**：

| 分片 | 列表页 | 列表记录 | 详情 | 备注 |
|------|--------|----------|------|------|
| 黄浦区 | 731/730 页 | 7296 条 | 100% | 采集期间服务端数据微增 |
| 徐汇区 | 1912/1912 页 | 19111 条 | 100% | 完整 |
| 长宁区 | 1083/1083 页 | 10822 条 | 100% | 完整 |
| **合计** | — | **37259 条** | **100% 含详情** | failed_pages=0, detail_failed=0 |

合并产物 `house_data.json` / `house_data.csv`：每行 = 列表 9 字段 + 36 个 `d_` 前缀详情字段。
关键字段非空率：挂牌号/核验编号/总价/建筑面积/总楼层/装修/用途/经纪机构及电话 100%，
朝向 99.7%、竣工日期 99.7%、房型 27.8%（接口本身为 null）。耗时约 40 分钟
（列表 ~15 分钟 + 详情 ~25 分钟）；qg.net 免费代理配额耗尽后 worker 自动直连兜底，
全部 200（说明本站瑞数对独立会话的直连容忍度高，代理主要起 IP 隔离作用）。

### 6.4 多挂牌房源与联系人（一房多挂处理）

**平台机制**：同一房屋（oldhouseid）可被多家经纪机构分别挂牌。列表接口按挂牌粒度返回，
同一房屋多挂牌 = 列表多行（实测 19179 个唯一房屋 / 37259 条挂牌记录，9152 个房屋在
数据中多于一行）。每条挂牌的详情（selectOldHouseInfo）带 `num`（核验/历史计数，
**不等于当前挂牌数**，不可用作挂牌数判断）与自己的机构信息。

**三处挂牌信息源（精度递增）**：

```
1. 列表行          district/location/… + 无机构字段
2. 详情接口        selectOldHouseInfo.action -> institutionname(总公司名)/institutionphone(总部电话)
3. 挂牌列表接口    selectNewOldHouse.action (POST: oldhouseid+currentPage)
                   -> htmlView 表格：核验编号 | 发布人/经纪机构名称(含分公司后缀) | 联系电话
                   + RecordCount/PageCount（当前挂牌数权威值）+ 房屋信息(wyyard/buildingarea…)
4. 联系人接口      searchContactInfo.action (salebrandid+agencyid+str_AgencyShow)
                   -> explain(机构/受理处), name(门店名), tel(电话), adr(受理处地址)
```

**离线对账结论（样例房屋 num=38）**：挂牌接口 4 条挂牌的核验编号与我们数据 4 行
**一一对应**（挂牌完整性 OK）；但机构名精度不同——我们的是总公司名，挂牌接口是
带分公司后缀的门店名（如"德佑房地产经纪有限公司第一千零二十三分公司"），
且个别电话不同（总部电话 vs 门店电话）。

**补全方案（--branch，已实现）**：对每个唯一 oldhouseid 调 `selectNewOldHouse.action`
（分页拉全），解析挂牌行，按核验编号 join 回记录，新增 `b_branch_name`（门店精确名）、
`b_branch_phone`（门店电话）、`b_release_id` 字段；落盘 `<片>_branch.jsonl`，
断点续采；merge 时自动 join。联系人接口（searchContactInfo）需 release_detail 页的
agencyid 参数，待进一步分析。

### 6.2 IP 代理接入

代理接口：`GET https://share.proxy.qg.net/get?key=YOUR_KEY&num=N`，返回 JSON，
`data[].server` 为 `ip:port`（HTTP 代理），`data[].deadline` 为有效期。实测 8/8 连通
fangdi（拿到 412 挑战页）。

**架构（单独 IP 模型）**：每个 worker（及主进程取区县字典）独立一个 `ProxyPool`，
各自拉取 `--proxy-num` 个代理，互不共享、互不重叠；**粘性（sticky）固定其中一个 IP
全程使用**——单进程/单 worker 的 cookie 会话与 IP 一一对应，不同 worker 各用各的 IP；
`FangdiRuishu` 所有对外 HTTP 统一走 `_request()` 出口：

```
_request(method, url)
  ├─ 池有代理：粘性当前 IP 发起 -> 成功返回
  ├─ 当前 IP 失败：剔除 -> 粘到池内下一个备用 IP
  └─ 池空/全部失败：直连兜底（不阻断采集）

rebootstrap()（会话失效）
  └─ rotate_proxy()：force_next 强制切换到下一个 IP，再清 cookie 重走 412->cookie->200
     => 换 IP + 换 cookie 双保险，规避 IP 级风控
```

**ProxyPool 行为**：`deadline` 过期自动剔除；池空且距上次拉取超过 3s 自动补充；
`mark_bad()` 剔除当前粘性代理后自动换下一个；粘性跨片保持（同一 worker 处理多片
全程同一 IP），仅连接失败/会话失效才切换。

**实测（2026-10）**：单进程全程固定一个 IP（118.213.44.246）；3 worker 并发各自
独立代理池（9 个 IP 互不重叠），各粘一个 IP 跨片使用（W92 处理嘉定+闵行两片均用
49.64.42.59），坏代理（ConnectionError）自动剔除换备用。注意共享代理存在被瑞数
风控的可能性（数据中心 IP 特征），出现 412 循环时脚本会自动换 IP + 换 cookie 重试，
若持续失败建议换高质量代理源。

---

## 七、升级应变与注意

1. **结构变化探测器**：若某天 412/重放失败，先跑 `probe_stage1.py`，对照第二节检查：
   - `r='m'` 外联 script 是否还在（定位锚点）；
   - meta 标签 / `$_ts.nsd` / `$_ts.cd` 是否变化；
   - cookie 名字（`UA1L1zGonajvO/P`）是否换名——代码里已按 `document.cookie` 动态读取，不写死。
2. **签名时效**：`XJlCTRRM` 后缀时间/会话绑定，必须生成即重放（`call()` 已保证）。
3. **频率风控**：全量近 28 万条，按区县分片 + `--delay 0.5~1s`；412/413 自动退避重试；
   并发时注意 `--workers` 与 `--delay` 的组合速率（10 worker + delay 1.0 ≈ 10 req/s）。
4. **解释器一致性**：只用 `python`（PATH 上的 3.7 无 iv8）。
5. **若瑞数升级到行为采集型**（真实鼠标轨迹 / 二次动态 meta 校验变严）：iv8 路线仍可先用
   （先试更完整指纹 + 真实页面 HTML 注入），失败再切真浏览器采集路线（Playwright/CDP），
   按 skill 的 P4 判决（服务器 200）决定。
