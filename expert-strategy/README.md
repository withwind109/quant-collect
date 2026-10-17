# expert-platform — 社区高手 & 高手跟随策略平台

从 xcq-platform 迁移出来的**独立项目**：社区高手（东财高手操作流水）抓取 + 高手跟随策略
（策略文件化 / 统一回测 / 模拟盘前向验证 / 晋级管理）。

Web 层为 **FastAPI + 原生 HTML 前端**（参考源项目 xcq-platform 的 dashboard 架构：
`web/app.py` create_app + 扁平 `web/routes/*.py` + `web/static/` 单 HTML/hash 路由/vanilla JS/
ECharts 本地 vendor），页面直接调用业务模块，无额外中间层；后台任务（爬取 / 回测 / 净值更新）
以子进程方式驱动，状态写 JSON 文件供前端轮询。

## 与原平台的差异

| 维度 | xcq-platform | 本项目 |
|---|---|---|
| Web 层 | FastAPI + 自研 dashboard 前端 | **FastAPI + 原生 HTML**（`web/`，架构对齐源项目，业务模块直连） |
| 存储 | 远端 MySQL（xcq_* 表） | **本地 SQLite**（`data/experts.db`，可随 git 提交） |
| 行情 | qlib bin 文件直读 | **本地 SQLite 价格库**（`data/prices.db`，akshare 兜底下载） |
| 东财认证 | 扫码登录 + 令牌 | **Cookie 认证**（`.env` 的 `EASTMONEY_COOKIE`，不再扫码） |
| 抓取通道 | 裸 HTTP | **无头浏览器**（Playwright：预热主站 + 同源 fetch 拟人会话；批量任务自动走轻量后端） |
| 数据源 | 固定四源 | 源可插拔（`src/sources/`），**未来加渠道只需新增一个源** |
| v3_tiers 引擎 | LightGBM 模型训练流水线 | **暂未迁移**（统一回测配置里已停用该策略，其余策略不受影响） |
| 平台自动化 | 自动交易 / 实盘联动 / 推送 | 未迁移（模拟盘前向净值由 `strategy/paper_sim.py` 按日重放近似） |

## 目录结构

```
expert-platform/
├── main.py                  # CLI 入口（crawl/history/bulk-history/db-sync/strategy/ui/prices）
├── start.sh                 # 一键：venv → 依赖 → chromium → 启动服务（默认 8601）
├── config.yaml              # 爬虫配置（Cookie 走 .env；browser 段控制无头浏览器通道）
├── requirements.txt         # 只保留本项目实际用到的依赖（fastapi/uvicorn/curl_cffi/playwright…）
├── scripts/
│   ├── run_job.py           # 后台任务包装（子进程 + 状态 JSON + 实时日志）
│   └── e2e_check.py         # 无头浏览器回归（渲染两页面、收集 JS 错误/失败请求）
├── configs/expert_follow/   # 策略文件(json5) + 回测配置（内置 + user/）
├── src/                     # 业务模块（与 Web 层解耦，CLI 也可直接驱动）
│   ├── paths.py             #   全部路径常量
│   ├── settings.py          #   config.yaml + .env 加载
│   ├── pipeline.py          #   每日爬取流水线（定交易日→抓榜→展开买入→JSON+SQLite 双写）
│   ├── cli.py               #   click 命令组
│   ├── history.py           #   单组合调仓历史回补
│   ├── bulk_history.py      #   批量回补（跟随策略回测的数据基础）
│   ├── core/                #   db(SQLite+q/qexec) / http / browser / storage / render /
│   │                        #   logger / utils / models / calendar / perf / trade_costs /
│   │                        #   exit_rules / exit_policy
│   ├── sources/             #   eastmoney(Cookie) / xueqiu / tonghuashun / alipay / base
│   └── strategy/            #   跟随策略引擎（迁移自 scripts/expert_follow + experts + recs + expert_v3）
│       ├── prices.py        #     行情价格库（SQLite + akshare）
│       ├── pit_nav.py       #     高手组合逐日净值重建（PIT 排名）
│       ├── backtest_v2.py   #     v2 回测引擎（Ctx / VARIANTS / run_variant）
│       ├── config.py        #     策略文件/回测配置（json5）读写校验
│       ├── selector.py / engine.py / runner.py / store.py / live.py / recon.py / data_health.py
│       ├── accounts.py / promotion.py / alpha.py / registry.py / ledger.py / stops.py / recs_store.py
│       ├── v3_pool.py / v3_screen_rank.py / v3_config.py / v3_store.py
│       ├── watchlist.py     #     关注高手
│       └── paper_sim.py     #     模拟盘前向净值（按日重放）
├── web/                     # FastAPI + 原生 HTML 前端（替代原 Streamlit UI）
│   ├── app.py               #   create_app 工厂 + 路由注册 + 静态挂载 + lifespan 建表
│   ├── jobs.py              #   后台任务（模块级状态 + run_job.py 子进程）
│   ├── routes/
│   │   ├── experts.py       #   /api/expert-crawl/*（爬取/回补/关注/数据浏览）
│   │   └── strategy.py      #   /api/expert-follow/*（看板/策略/回测/发布/晋级/体检）
│   └── static/              #   index.html + css/ + js/（vanilla）+ echarts.min.js（本地 vendor）
└── data/                    # crawl/ history/ history_bulk/ *.db / 运行产出
```

## 快速开始

```bash
# 1. 一键安装 + 启动（自动建/复用 venv：优先 .venv/，否则 .env/；依赖、chromium、服务全包）
./start.sh                  # 默认 8601 端口；./start.sh 8602 换端口；--install 只装不起

# 手动方式（等价）：
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # 无头浏览器通道（可选，失败自动回退 curl）

# 2. 配置东财 Cookie（首次运行 start.sh 会从 .env.example 生成 .env）
#    .env → EASTMONEY_COOKIE=...

# 3. 自检（凭据 + 各源连通性）
python main.py check

# 4. 爬取今日高手买入（自动推断最近有数据的交易日）
python main.py crawl --sources eastmoney,tonghuashun --top 50

# 5. 初始化行情价格库（回测/前向模拟前必须；指数 + 高手持仓票自动补齐）
python main.py prices --index 000300.SH

# 6. 批量回补高手调仓历史（跟随策略的数据基础；--limit 20 先小批量试）
python main.py bulk-history --limit 20

# 7. 跑一次统一回测
python main.py strategy run --backtest unified_sim
```

启动后访问 **http://127.0.0.1:8601**（`python main.py ui --port 8601` 等价），两个页面：

* **社区高手**（`#/experts`）：触发爬取 / 按日期浏览（高手买入主视图、高手榜、成交明细、个股聚合、热度榜、信号、覆盖度）/ 关注管理 / 单组合与批量历史回补。
* **高手跟随策略**（`#/expert-strategy`）：策略看板（模拟账户前向表现 + 晋级）/ 策略文件（json5 原文 + 解析 + 指纹 + 另存为）/ 回测配置 / 运行回测（后台运行 + 记录 + 发布）/ 数据体检。

API 示例（与页面同源，也可供脚本调用）：

```bash
curl localhost:8601/api/expert-crawl/dates            # 可用交易日
curl localhost:8601/api/expert-crawl/data?date=...    # 当日全量数据
curl -X POST localhost:8601/api/expert-crawl/run \
     -H 'Content-Type: application/json' -d '{"sources":["eastmoney"],"top":50}'
curl localhost:8601/api/expert-crawl/status           # 全部后台任务状态
```

## 跟随策略引擎说明（迁移保持原口径）

* **选人（PIT，无前视）**：只用信号日前一交易日收盘及以前的数据算排名/筛选（`pit_nav.py` 逐日净值重建）。
* **执行口径**：T 日收盘算目标 → T+1 开盘 ±0.2% 滑点调仓；佣金万 2.5（最低 5 元）+ 卖出印花税 0.05%；涨停不买、跌停不卖；单票上限 20%；调仓变化 <0.5% 净值不动。
* **统一回测**：所有版本同一回测期、同一套闸门（`unified_sim.json5`，不放宽）；随机对照从同一合格池抽同样人数（20 种子，80 分位）。
* **发布**：过闸门 → 直接发布模拟盘；未过闸门 → 只能「前向观察」（账户名带 🧪）；改策略文件 = 新版本（指纹变，需重新回测/发布）。
* **晋级**：request / approve / revoke 三态，只生成待确认单。
* **幸存者偏差**：高手名单来自东财流水里出现过的组合，PIT 排名消不掉名单本身的事故后性 —— 回测收益当上限参考，真正的检验看模拟盘前向表现。

## 已知限制（后续可补）

1. `v3_tiers` 引擎（LightGBM 模型训练流水线 expert_imitation）未迁移；统一回测配置中已停用，其余策略（mirror / signal）全部可用。
2. 模拟盘前向净值由 `paper_sim.py` 按日重放近似（不含日内止损止盈的精细模拟）；原平台的实时条件单 / 自动交易联动未迁移。
3. 实盘（rtV3 流水）数据依赖每日爬取自动累积到 `crawl_trade(market_type='live')`，样本不足时 data_env=live 的回测结论仅供参考。
4. 关注列表（watchlist）为空时 v1 镜像策略无可跟高手，属预期。
5. 前端无登录鉴权（内网工具定位，与原平台一致）；券商登录态是服务端概念，本项目只用 Cookie。
