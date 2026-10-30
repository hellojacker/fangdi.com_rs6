#  Fangdi 二手房源采集说明

基于 **iv8 补环境** 破解 [网上房地产（fangdi.com.cn）](https://www.fangdi.com.cn) 的**瑞数（Rivers）反爬**，采集上海市**二手房源挂牌数据**，并支持批量翻页、结构化落地 JSON / CSV。


> **仓库边界**：这里只保留可运行链路（签名器 / 批量采集 / 代理池 / 探测与回归脚本）+ 分析文档。
> 采集数据（`house_data.*`）、断点状态（`scrape_state.json`）、抓包证据（`*.html` / `*.txt` / `*.bin`）
> 与本地临时目录（`out/`、`__pycache__`、`.idea`）均**未随仓库发布**。

---

## 一、采集的是什么

| 项目 | 说明 |
|------|------|
| **数据来源** | 网上房地产 `https://www.fangdi.com.cn`（上海市官方房地产交易平台） |
| **采集对象** | 二手房源挂牌列表 |
| **数据接口** | `POST https://www.fangdi.com.cn/oldhouse/selectOldHouse.action` |
| **数据规模** | 全量约 **284,845 条 / 28,485 页**（按区县拆分，如黄浦区 7,305 条 / 731 页） |

### 采集字段

每条房源记录包含以下字段（来自接口返回的 `htmlView` 表格）：

| 字段 | 含义 | 示例 |
|------|------|------|
| `district` | 所在区域 | 黄浦区 |
| `location` | 房屋坐落 | 人民路777弄 |
| `house_type` | 房型 | 三室二厅 |
| `orientation` | 朝向 | 南 |
| `total_floor` | 总楼层 | 33 |
| `area` | 面积（㎡） | 114.8500 |
| `detail_id` | 房源详情 ID | e3b0c44298fc1c149afbf4c8996fb924 |
| `detail_url` | 详情页完整链接 | https://www.fangdi.com.cn/old_house/old_house_list_detail.html?id=... |
| `page` | 来源页码 | 1 |

---

## 二、反爬机制（瑞数两阶段）

目标站使用**瑞数动态防护**，分两个阶段，本项目用 iv8（V8 引擎补环境）逐一绕过：

```
第一阶段：拿页面
  GET old_house.html  ──412──►  拦截页(含 $_ts + 外联JS 8WwVutQAyu6q...)
        │  iv8 执行反爬 JS
        ▼
  生成动态 cookie (UA1L1zGonajvO)  ──携带重试──►  200 真实页面

第二阶段：拿接口数据
  200 页面里藏着【第二套瑞数挑战】(新的 $_ts + 外联JS jNafbsZdWo4R...)
        │  iv8 执行第二套 JS → hook 住 XMLHttpRequest
        ▼
  new XHR 打 selectOldHouse.action 时，瑞数重写请求，追加：
        • 瑞数后缀(URL)：?XJlCTRRM=<动态签名>
        • 请求头后缀：新生成的动态 cookie (UA1L1zGonajvP)
        │  提取后缀 + cookie，Python 立即重放
        ▼
  200 + 真实房源 JSON 数据
```

**关键点**：瑞数后缀 `XJlCTRRM` 是**时间/会话绑定**的，每次请求都需在 iv8 里现生成、瞬时重放，不能缓存复用。签名只与请求路径有关，与 POST body 无关，因此同一套逻辑适配所有 `.action` 接口。

---

## 三、文件结构

| 文件 | 作用 |
|------|------|
| **`fangdi_api.py`** | ⭐ **核心库**：可复用签名器 `FangdiRuishu`，封装两阶段补环境 + 任意接口签名 + `rebootstrap()` 会话重建 |
| **`fangdi_batch.py`** | ⭐ **批量采集**：按区县分片 + 断点续采 + cookie 失效页级重试 + 多进程并发 + IP 代理（日常使用入口） |
| **`proxy_pool.py`** | 共享代理池：拉取/轮询/过期剔除/坏代理剔除/自动补充 |
| `fangdi_iv8.py` | 第一阶段演示：412 → 生成 cookie → 200 页面 |
| `fangdi_stage2.py` | 第二阶段演示：单次签名验证 `selectOldHouse.action` |
| `probe_stage1.py` | 挑战页结构探测器（瑞数升级后先跑它比对） |
| `probe_proxy.py` / `probe_proxy2.py` | 代理接口格式 / 代理连通性探测 |
| `test_resume_recover.py` | 会话失效重建 + 断点续采回归测试 |
| `test_proxy.py` | 代理轮换 / rebootstrap 换 IP 回归测试 |
| `ANALYSIS.md` | 瑞数6 全流程分析（实测验证版） |
| `house_data.json` / `house_data.csv` | 采集结果样例（**未随仓库发布**，运行后本地生成） |

---

## 四、环境准备

```bash
# 依赖：Python 3.8–3.14，安装 iv8 与 requests
pip install iv8 requests
```

### 代理 key（可选）

仓库内不放真实 key，默认值是占位 `YOUR_KEY`。要用共享代理时用环境变量注入：

```bash
# PowerShell
$env:QG_PROXY_KEY="你的key"
# CMD
set QG_PROXY_KEY=你的key
# bash
export QG_PROXY_KEY=你的key
```

不设也能跑：拉不到代理会自动直连兜底，或显式 `--no-proxy` 全直连。

> ⚠️ **多解释器注意**：若终端报 `No module named 'iv8'`，说明当前 `python` 不是装了 iv8 的那个解释器。请确认 `python -c "import iv8"` 能成功，或直接用已装好 iv8 的解释器运行。

验证环境：

```bash
python -c "import iv8, requests; print('ok')"
```

---

## 五、使用方法

### 5.1 批量采集（推荐入口：分片 + 断点续采）

```bash
# 全部区县分片采集（每区县独立进度/输出，中断后重跑自动续采）
python fangdi_batch.py

# 10 并发全量采集（页级并发：3 片也能满 10 worker）+ 详情数据 + 合并
python fangdi_batch.py --districts 黄浦区,徐汇区,长宁区 --pages 0 --workers 10 --detail --merge

# 走 IP 代理（默认启用 qg.net 共享代理接口，每个 worker 独立拉取 5 个）
python fangdi_batch.py --workers 10 --proxy-num 10

# 自定义代理接口 / 禁用代理直连
python fangdi_batch.py --proxy-url "https://share.proxy.qg.net/get?key=YOUR_KEY" --proxy-num 10
python fangdi_batch.py --no-proxy

# 指定若干分片
python fangdi_batch.py --districts 黄浦区,徐汇区,浦东新区 --workers 10

# 每片最多抓 5 页（0 = 抓完该片）
python fangdi_batch.py --districts 黄浦区 --pages 0

# 自定义输出目录、状态文件、翻页间隔
python fangdi_batch.py --districts 黄浦区 --delay 0.8 --out-dir data --state data/state.json

# 附加过滤条件（可重复 --param）
python fangdi_batch.py --districts 黄浦区 --param houseType=xxx --param area=yyy

# 合并所有分片为一个汇总文件
python fangdi_batch.py --merge

# 忽略断点状态从头重抓（清空分片数据文件）
python fangdi_batch.py --fresh
```

**断点续采机制**：每页成功后原子更新 `scrape_state.json`（记录每片 next_page/records/failed_pages，
带跨进程文件锁），数据按页增量追加到 `out/<区县>.jsonl` 与 `out/<区县>.csv`。中断（含 Ctrl+C）后
重跑同一命令，自动从断点继续，不重复已抓页面；上次失败的页优先补采。

**多进程并发（--workers N）**：iv8 单上下文不可多线程，故并发采用多进程：每个 worker 独立
`FangdiRuishu` 会话（独立 iv8 上下文/cookie/签名）。**页级并发**：任务粒度是"一页"而非"一片"，
因此 3 个区县也能满 10 并发（探页拿 total_pages → 全量页任务池分发）。断点状态经文件锁保证
跨进程安全（done_pages 集合模型）。内存开销 ≈ N × 一个 iv8 上下文（约 150–300MB），
请按机器内存选择 N；并发度也会线性抬高请求频率，请配合 `--delay` 控制。

**详情采集（--detail）**：列表接口的每条记录带 `detail_id`，详情数据来自签名接口
`POST /oldhouse/selectOldHouseInfo.action`（body: `salebrandid=<detail_id>`，返回 37 字段 JSON：
挂牌号、核验编号、总价、建筑面积、房型、楼层、朝向、装修、经纪机构及电话、经纬度等）。
详情逐条任务化并发采集，落盘 `out/<区县>_detail.jsonl`；`--merge` 时按 detail_id join，
详情字段加 `d_` 前缀合并进最终 house_data.json/csv。

**IP 代理（--proxy-url / --proxy-num / --no-proxy）**：默认启用共享代理接口
`https://share.proxy.qg.net/get?key=YOUR_KEY`（返回 JSON，`server` 字段为 `ip:port`，
`deadline` 为有效期）。行为：

- **单独 IP 模型**：每个 worker（及主进程）独立拉取 `--proxy-num` 个代理组成自己的池，
  且**粘性（sticky）固定其中一个 IP 全程使用**——单进程/单 worker 的 cookie 会话与 IP
  一一对应；不同 worker 的代理池互不重叠（共享接口提取语义保证）；
- 当前 IP 连接失败（ProxyError/超时等）自动剔除，粘到池内下一个备用 IP，全部失败则直连兜底；
- 会话失效 `rebootstrap` 时**强制切换代理 IP**（换 IP + 换 cookie 双保险，规避 IP 级风控）；
- 代理按 `deadline` 自动过期剔除、池空自动补充拉取；
- 代理接口本身直连访问（不走代理），拉取失败不影响采集（直连兜底）；
- 日志可观测：`[proxy] worker#xxx 独立代理池: [...]` 展示各 worker 的 IP 列表，
  `[proxy] 使用代理 ip:port` 只在切换时打印。

**cookie 失效页级重试**：单页返回 412/403/挑战页（瑞数会话失效）时，自动 `rebootstrap`
重建两阶段环境（412→新 cookie→200→重装 XHR hook）后**只重试当前页**，不重抓已成功页面；
重建次数可用 `--cookie-retries` 调整（默认 3）。普通失败按退避重试 3 次，仍失败记入
`failed_pages`，下次运行自动补采。

**命令行参数**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--districts` | 区县列表（逗号分隔），缺省=全部区县分片 | 全部 |
| `--district` | 兼容旧参数：单区县 | 无 |
| `--pages` | 每片最多抓取页数，`0` = 抓完该片（默认 0） | 0 |
| `--delay` | 每页之间的间隔秒数（降低风控概率） | 0.5 |
| `--workers` | 并发 worker 数（页级并发，片数少也能满并发；每 worker 独立会话） | 1 |
| `--detail` | 采集房源详情（selectOldHouseInfo.action 签名接口） | 关 |
| `--detail-delay` | 每条详情间隔秒数 | 0.1 |
| `--param K=V` | 附加过滤参数，可重复 | 无 |
| `--out-dir` | 分片输出目录（jsonl/csv/json 每片一份） | out |
| `--state` | 断点状态文件路径 | scrape_state.json |
| `--cookie-retries` | cookie 失效时单页重建环境次数 | 3 |
| `--proxy-url` | 代理提取接口 URL（默认 qg.net 共享代理） | `https://share.proxy.qg.net/get?key=YOUR_KEY` |
| `--proxy-num` | 每次/每个 worker 拉取的代理数量 | 5 |
| `--no-proxy` | 禁用代理，全部直连 | 关 |
| `--fresh` | 重置断点状态与分片数据文件后重抓 | 关 |
| `--merge` | 结束后合并全部分片为 house_data.json/csv | 关 |
| `--quiet` | 减少日志 | 关 |

### 5.2 作为库调用（自定义逻辑）

```python
from fangdi_api import FangdiRuishu

with FangdiRuishu() as fd:
    # 获取区县字典 {名称: code}
    districts = fd.get_districts()          # {'黄浦区': '27d3af3bd45acf5e', ...}

    # 查询房源（返回 requests.Response + 签名URL）
    resp, signed_url = fd.select_old_house(page=1, district=districts['黄浦区'])
    html_view = resp.json()['htmlView']     # 房源表格 HTML

    # 也可签名任意其它 .action 接口
    resp, _ = fd.call('POST', '/oldhouse/getSaleHouse.action')
```

`FangdiRuishu` 主要方法：

| 方法 | 说明 |
|------|------|
| `bootstrap()` | 完成两阶段补环境，常驻一个已 hook XHR 的 iv8 上下文（`with` 会自动调用） |
| `rebootstrap()` | 会话失效时调用：清 cookie、关闭旧上下文、重建两阶段环境 |
| `sign(method, path, body)` | 在 iv8 里 new XHR，返回瑞数重写后的签名请求信息 |
| `call(method, path, data)` | 签名 + 同步动态 cookie + 立即用 Python 重放，返回 `(response, signed_url)`；响应为 412/403/挑战页时抛 `SessionExpiredError` |
| `get_districts()` | 获取区县字典 `{名称: code}` |
| `select_old_house(page, **filters)` | 查询二手房源，支持分页与过滤 |

---

## 六、数据格式样例

### JSON（`house_data.json`）

```json
[
  {
    "district": "黄浦区",
    "location": "人民路777弄",
    "house_type": "",
    "orientation": "南",
    "total_floor": "33",
    "area": "114.8500",
    "detail_id": "e3b0c44298fc1c149afbf4c8996fb924",
    "detail_url": "https://www.fangdi.com.cn/old_house/old_house_list_detail.html?id=e3b0c44298fc1c149afbf4c8996fb924",
    "page": 1
  }
]
```

### CSV（`house_data.csv`）

```csv
district,location,house_type,orientation,total_floor,area,detail_id,detail_url,page
黄浦区,人民路777弄,,南,33,114.8500,e3b0c44298fc1c149afbf4c8996fb924,https://www.fangdi.com.cn/old_house/old_house_list_detail.html?id=e3b0c44298fc1c149afbf4c8996fb924,1
```

---

## 七、区县代码表

`--district` 传中文名称即可，脚本会自动解析为对应 code（下表为参考，实际以运行时 `get_districts()` 为准）：

| 区县 | code | 区县 | code |
|------|------|------|------|
| 黄浦区 | 27d3af3bd45acf5e | 闵行区 | e5cf885d0ef25e94 |
| 徐汇区 | 53c7b05ea45cf006 | 嘉定区 | d095fa89de83a452 |
| 长宁区 | c25cb8e534080b41 | 浦东新区 | 5391be55e75e5506 |
| 静安区 | 3e9d0ed5cf865b9b | 奉贤区 | 45c69c52e212e98d |
| 普陀区 | 9999a061b2bf2968 | 松江区 | c3a04b33d3238385 |
| 虹口区 | e153da3d0ca1946e | 金山区 | 1c1f50e015c71814 |
| 杨浦区 | 5560708b21685990 | 青浦区 | ef8c008816798859 |
| 宝山区 | 2385fa574f10a564 | 崇明区 | d6ff877c62587d67 |

---

## 八、查询过滤参数

`select_old_house()` / `--param` 支持的过滤字段（对应站点搜索表单）：

| 参数 | 含义 |
|------|------|
| `district` | 所在区域（区县 code） |
| `region` | 板块/区域细分 |
| `area` | 面积范围 |
| `price` | 房屋总价范围 |
| `time` | 挂牌时间 |
| `houseType` | 房屋类型 |
| `location` | 房屋坐落（关键字） |
| `blockName` | 小区名称 |
| `listingNo` | 挂牌编号 |
| `verifyNo` | 核验编号 |
| `currentPage` | 页码（由 `page` 参数控制） |

---

## 九、注意事项

1. **签名时效**：瑞数后缀与时间/会话绑定，脚本已做到「生成即重放」，请勿手动缓存复用旧后缀。
2. **风控与频率**：全量数据近 28 万条，建议**按区县分批**采集并设置 `--delay`（如 0.5~1s）。若出现 412/413，脚本会自动退避重试；持续失败请降低频率或更换 IP。
3. **解释器一致性**：确保运行脚本的 Python 解释器已安装 `iv8`（见第四节）。
4. **合规使用**：本项目仅用于技术研究与学习，请遵守目标网站的服务条款及相关法律法规，合理控制采集频率，勿用于商业用途或造成服务端压力。

---

## 十、快速验证

```bash
# 跑一遍最小示例（黄浦区前 2 页），再跑一遍验证断点续采
python fangdi_batch.py --districts 黄浦区 --pages 2
python fangdi_batch.py --districts 黄浦区 --pages 4
```

预期输出：

```
[2] 第二阶段页面 status=200
[3] 第二阶段环境就绪（XHR 已被瑞数 hook）
    POST /oldhouse/getDic.action -> 200 ...
[plan] 分片列表(1): ['黄浦区']
[slice] 黄浦区 (code=27d3af3bd45acf5e) 从 page 1 开始
    [黄浦区] page 1 +10 条 (累计 10/7303)
    [黄浦区] page 2 +10 条 (累计 20/7303)
[stop] 达到分片页数上限 max_pages=2
[DONE] 本次新增 20 条

（第二次运行：）
[state] 已载入断点状态: 1 个分片
[slice] 黄浦区 从 page 3 开始，总页数 731
    [黄浦区] page 3 +10 条 ...（从断点继续，不重复已抓页面）
```
