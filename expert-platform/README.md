# expert-platform — 社区高手 & 高手跟随策略平台

从 xcq-platform 迁移出来的**独立项目**：社区高手（东财高手操作流水）抓取 + 高手跟随策略
（策略文件化 / 统一回测 / 模拟盘前向验证 / 晋级管理）。原平台的 FastAPI dashboard 中间层
**没有迁移** —— Streamlit 页面直接调用业务模块（架构参考 multi-factor-stock），
后台任务（爬取 / 回测）以子进程方式驱动。

## 与原平台的差异

| 维度 | xcq-platform | 本项目 |
|---|---|---|
| Web 层 | FastAPI + 自研 dashboard 前端 | **Streamlit**（`src/ui/app.py`，无中间层） |
| 存储 | 远端 MySQL（xcq_* 表） | **本地 SQLite**（`data/experts.db`，可随 git 提交） |
| 行情 | qlib bin 文件直读 | **本地 SQLite 价格库**（`data/prices.db`，akshare 兜底下载） |
| 东财认证 | 扫码登录 + 令牌 | **Cookie 认证**（`.env` 的 `EASTMONEY_COOKIE`，不再扫码） |
| 数据源 | 固定四源 | 源可插拔（`src/sources/`），**未来加渠道只需新增一个源** |
| v3_tiers 引擎 | LightGBM 模型训练流水线 | **暂未迁移**（统一回测配置里已停用该策略，其余策略不受影响） |
| 平台自动化 | 自动交易 / 实盘联动 / 推送 | 未迁移（模拟盘前向净值由 `strategy/paper_sim.py` 按日重放近似） |

## 目录结构

```
expert-platform/
├── main.py                  # CLI 入口（crawl/history/bulk-history/db-sync/strategy/ui/prices）
├── run_streamlit.sh         # 启动 Streamlit（默认 8501 端口）
├── config.yaml              # 爬虫配置（Cookie 走 .env）
├── requirements.txt         # 只保留本项目实际用到的依赖
├── scripts/run_job.py       # 后台任务包装（子进程 + 状态文件，供页面轮询）
├── configs/expert_follow/   # 策略文件(json5) + 回测配置（内置 + user/）
├── src/
│   ├── paths.py             # 全部路径常量
│   ├── settings.py          # config.yaml + .env 加载
│   ├── pipeline.py          # 每日爬取流水线（定交易日→抓榜→展开买入→JSON+SQLite 双写）
│   ├── cli.py               # click 命令组
│   ├── history.py           # 单组合调仓历史回补
│   ├── bulk_history.py      # 批量回补（跟随策略回测的数据基础）
│   ├── core/                # db(SQLite+q/qexec) / http / browser / storage / render /
│   │                        # logger / utils / models / calendar / perf / trade_costs /
│   │                        # exit_rules / exit_policy
│   ├── sources/             # eastmoney(Cookie) / xueqiu / tonghuashun / alipay / base
│   ├── strategy/            # 跟随策略引擎（迁移自 scripts/expert_follow + experts + recs + expert_v3）
│   │   ├── prices.py        #   行情价格库（SQLite + akshare）
│   │   ├── pit_nav.py       #   高手组合逐日净值重建（PIT 排名）
│   │   ├── backtest_v2.py   #   v2 回测引擎（Ctx / VARIANTS / run_variant）
│   │   ├── config.py        #   策略文件/回测配置（json5）读写校验
│   │   ├── selector.py / engine.py / runner.py / store.py / live.py / recon.py / data_health.py
│   │   ├── accounts.py / promotion.py / alpha.py / registry.py / ledger.py / stops.py / recs_store.py
│   │   ├── v3_pool.py / v3_screen_rank.py / v3_config.py / v3_store.py
│   │   ├── watchlist.py     #   关注高手
│   │   └── paper_sim.py     #   模拟盘前向净值（按日重放）
│   └── ui/                  # Streamlit（app/components/routes/views，参考 multi-factor-stock）
└── data/                    # crawl/ history/ history_bulk/ follow_backtest/ *.db / logs 输出
```

## 快速开始

```bash
# 1. 虚拟环境（只装可用依赖）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置东财 Cookie（已提供在 .env；失效后重新复制浏览器 Cookie 即可）
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

# 8. 启动 UI
./run_streamlit.sh          # 或 python main.py ui --port 8501
```

UI 两个页面：
* **社区高手**：触发爬取 / 按日期浏览（高手买入主视图、高手榜、成交明细、个股聚合、热度榜、信号、覆盖度）/ 关注管理 / 单组合与批量历史回补。
* **高手跟随策略**：策略看板（模拟账户前向表现 + 晋级）/ 策略文件（json5 原文 + 解析 + 指纹 + 另存为）/ 回测配置 / 运行回测（后台运行 + 记录 + 发布）/ 数据体检。

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
