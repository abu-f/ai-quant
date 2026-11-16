# aquant · A 股个人量化研究助手（WSL）

一个跑在 WSL 里的**只读**量化辅助工具：拉行情、存本地、算指标、跑回测、盯自选股、出报告。

> **边界声明（重要）**
> 本项目**不包含任何下单、委托、账户登录、券商接口或自动交易执行功能**。它只做三件事：
> 读取公开行情数据 → 本地计算与分析 → 把结果展示给你。
> 所有交易决策与买卖操作均由使用者本人完成。输出内容仅为数据统计，不构成投资建议。

---

## 一、功能范围

| 能力 | 说明 | 入口 |
| --- | --- | --- |
| 行情获取与增量更新 | 日/周/月 K 线（前复权/后复权/不复权）、**1/5/15/30/60 分钟线**、实时快照、全市场 5000+ 标的列表 | `sync` `quote` `bars` |
| 本地数据仓库 | 日线 SQLite 落地，重复读取零网络请求；多源回退；盘中自动剔除未收盘日 K | 自动 |
| 自选股维护 | 命令行与网页均可增删，改动直接写回 `config/watchlist.yaml` | `watch` / 网页 |
| 技术指标 | MA/EMA、MACD、RSI、KDJ、BOLL、ATR、量能、N 日高低点、回撤 | `ta` |
| 基本面 | PE/PB/市值/换手/量比 + 近 8 期财务摘要（营收、净利、ROE、毛利率、资产负债率…） | `fund` |
| 自选股监控与预警 | 13 类规则：价格、涨跌幅、均线/MACD/KDJ 交叉、RSI 阈值、突破、放量、回撤 | `alert check --loop` |
| 选股筛选 | 7 个预设：低估值、短线强势、放量异动、成交活跃、技术超卖、创阶段新高、均线多头 | `screen` |
| 历史回测 | 8 个策略，含佣金/印花税/过户费/滑点、T+1、100 股整手、组合等权 | `backtest` |
| 每日报告 | Markdown 日报：行情表 + 指标状态 + 规则命中 | `report` |
| 本地可视化 | 深色工作台：分钟/日/周/月 K 线（蜡烛+均线+成交量+MACD）、资金曲线、选股表、预警记录、自选增删 | `web` |
| 环境自检 | Python/依赖/5 个数据接口/数据库可写性一次查完 | `doctor` |

**明确的非目标**：不做实盘交易、不接券商、不提供投资建议、不做高频/Level-2、不预测涨跌。

---

## 二、模块划分

```
quant/
├── quant                 # WSL 启动器（等价于 python -m aquant）
├── setup.sh              # 一键安装（无需 sudo）
├── requirements.txt
├── pyproject.toml        # 可选：pip install -e . 后可用 aquant 命令
├── config/
│   ├── config.yaml       # 数据源、成本、预警、Web 配置
│   └── watchlist.yaml    # 自选股 + 预警规则
├── src/aquant/
│   ├── config.py         # 配置加载（默认值 + YAML 覆盖 + 环境变量）
│   ├── symbols.py        # 代码规范化（600519 ↔ sh600519 ↔ 600519.SH ↔ 1.600519）
│   ├── utils.py          # 时间、格式化、重试、限速
│   ├── store.py          # SQLite：bars / quotes / fundamentals / alerts / backtests
│   ├── data/             # 数据接入层（可插拔）
│   │   ├── base.py       #   Quote 数据结构、带全局限速的 HTTP
│   │   ├── tencent.py    #   日/周/月线主源（双域名）+ 分钟线备用
│   │   ├── sina.py       #   分钟线主源 + 日线备用 + 批量实时
│   │   ├── eastmoney.py  #   实时快照 / 全市场列表 / 财务摘要
│   │   └── provider.py   #   门面：缓存优先 + 多源回退 + 增量更新 + 分钟线内存缓存
│   ├── indicators.py     # 纯 pandas 指标库（无 TA-Lib 依赖）
│   ├── alerts.py         # 规则引擎 + 去重 + 通知
│   ├── screener.py       # 基本面/技术面选股
│   ├── backtest/
│   │   ├── strategies.py # 8 个策略 → 目标仓位序列
│   │   └── engine.py     # 逐日撮合、成本模型、绩效指标
│   ├── report.py         # Markdown 日报
│   ├── cli.py            # argparse 子命令 + rich 表格
│   └── web/              # Flask + ECharts 单页工作台
│       ├── app.py
│       ├── templates/index.html
│       └── static/echarts.min.js   # 本地打包，离线可用
└── tests/test_core.py    # 19 项离线单元测试
```

**数据流**

```
公开行情接口 ──► data/provider.py（限速/重试/回退）──► SQLite 缓存
                                                        │
             ┌──────────────────────────────────────────┼───────────────────┐
             ▼                      ▼                   ▼                   ▼
      indicators.py            alerts.py           backtest/           screener.py
             │                      │                   │                   │
             └──────────► cli.py（终端表格） / web/app.py（浏览器） / report.py（Markdown）
```

分层原则：**数据接入层不计算、计算层不联网、展示层不落库**。新增数据源只需实现 `daily()/quote()` 并注册到 `config.yaml` 的 `bar_priority`。

---

## 三、数据来源建议

| 用途 | 默认源 | 接口 | 特点 / 限制 |
| --- | --- | --- | --- |
| 日/周/月 K 线 | 腾讯 | `ifzq.gtimg.cn/appstock/app/fqkline/get` | 支持 qfq/hfq、最多约 1200 根；**无成交额**（本项目存 NULL，不造数）；主域名被 WAF 拦截时自动切换 `web.ifzq` 备用域名 |
| K 线备用 | 新浪 | `quotes.sina.cn/.../getKLineData` | 最多 1023 根；仅日线；字段干净 |
| **分钟 K 线（主）** | 新浪 | 同上 `scale=1/5/15/30/60` | 最多 1023 根，含成交额；1 分钟约 4 个交易日，60 分钟约 1 年 |
| **分钟 K 线（备）** | 腾讯 | `ifzq.gtimg.cn/appstock/app/kline/mkline` | 仅约 320 根；无成交额 |
| 实时快照（单只） | 东方财富 | `push2.eastmoney.com/api/qt/stock/get` | 带 PE/PB/市值/换手/量比 |
| 全市场列表 | 东方财富 | `push2.eastmoney.com/api/qt/clist/get` | 约 5500 只，用于选股；13 秒左右；分页抓取，单页失败只重试本页 |
| 全市场列表（备） | 新浪 | `vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeData` | 5578 只，约 20 秒；**无量比**、PE 为动态口径；东财被限流时自动接管 |
| 财务摘要 | 东方财富 | `datacenter-web.eastmoney.com/api/data/v1/get` | 近 8 期营收/净利/ROE 等 |
| 批量实时 | 新浪 | `hq.sinajs.cn/list=...` | 一次请求多只，延迟低，无估值字段 |

**为什么这样选**

- 三家公开接口都不需要 token，适合个人长期跑；互为备份，单点故障不至于让工具瘫掉。
- 东财 `push2his`（历史 K 线）在部分网络环境下会被重置，因此 K 线主源选腾讯，东财只用于快照/财务。
- 全局限速 `min_interval_ms: 250` + 重试退避，避免高频请求触发风控。**请不要把间隔调到 100ms 以下**。
- 数据仅供个人研究；商用或再分发请自行确认各接口的服务条款。

**想换/加数据源**

- `akshare`：功能全但依赖重、接口变动频繁。建议作为"补充"而非核心：在 `data/` 下新增 `akshare_provider.py` 实现同名函数即可。
- `tushare pro`：需要 token 和积分，财报/因子更规范，适合做基本面研究。加一个 `tushare.py` 并在 `bar_priority` 里排在前面。
- 本地 CSV/Parquet：实现一个读文件的 `daily()`，适合已有数据的人。

---

## 四、安装（WSL / Ubuntu）

前提：WSL2 + Ubuntu 22.04（本项目已在 `Ubuntu-22.04`、Python 3.10.12 实测）。

```bash
cd /mnt/c/Users/Huawei/Desktop/wsl/quant
./setup.sh
```

脚本做四件事：建虚拟环境 `~/.venvs/aquant` → 引导 pip（Ubuntu 精简镜像常缺 `ensurepip`）→ 装依赖（默认阿里云镜像）→ 初始化数据库。

手动安装（等价）：

```bash
sudo apt install -y python3 python3-venv        # 若系统没有 venv
python3 -m venv --without-pip ~/.venvs/aquant
curl -sSL https://bootstrap.pypa.io/get-pip.py | ~/.venvs/aquant/bin/python -
~/.venvs/aquant/bin/python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
./quant init
```

**目录约定（有意为之）**

| 内容 | 位置 | 原因 |
| --- | --- | --- |
| 代码/配置 | 项目目录（`/mnt/c/...`） | Windows 编辑器可直接改 |
| 虚拟环境 | `~/.venvs/aquant` | 不要放 `/mnt/c`，venv 里大量小文件跨文件系统很慢 |
| 数据库/缓存/报告 | `~/.local/share/aquant` | SQLite 在 drvfs 上锁性能差、易损坏，放 WSL 原生盘更稳 |

可用 `AQUANT_HOME` 改数据目录，`AQUANT_CONFIG` 改配置路径，`AQUANT_VENV` 改虚拟环境位置。

---

## 五、使用方式

### 5.1 命令行

```bash
./quant doctor                                  # 先自检：依赖、五个数据接口、数据库
./quant sync                                    # 同步自选股日线（默认 800 根）
./quant sync 600519 000001 --days 1200          # 指定标的与根数
./quant quote                                   # 自选股实时行情
./quant quote 600519 --cached                   # 只用缓存，不联网
./quant bars 600519 --days 30 --csv out.csv     # 看日 K 并导出
./quant bars 600519 --period 5m --days 48       # 5 分钟 K 线（1m/5m/15m/30m/60m）
./quant ta 600519                               # 指标快照 + 近期明细
./quant fund 600519 --periods 8                 # 估值 + 财务摘要
./quant watch list                              # 查看自选股
./quant watch add 600036 --note 银行            # 加入自选（自动补名称）
./quant watch rm 600036                         # 移出自选（支持多个代码）
./quant screen --preset value --limit 20        # 选股（--list 看全部预设）
./quant backtest 600519 --strategy ma_cross --param fast=5 --param slow=20 --days 500 --capital 500000
./quant backtest 600519,000001 --strategy macd  # 多标的等权组合
./quant alert check                             # 跑一次预警
./quant alert check --loop --interval 60        # 盯盘（仅本地提醒）
./quant report                                  # 生成 Markdown 日报
./quant web                                     # 启动可视化工作台
```

其他：`--json` 输出机器可读结果；`--verbose` 看调试日志；`-V` 版本。所有命令 `-h` 有中文帮助。

### 5.2 可视化工作台

```bash
./quant web            # 默认 127.0.0.1:8770
```

Windows 浏览器打开 `http://127.0.0.1:8770`（WSL2 会自动把 localhost 转发到 WSL）。五个页签：

- **自选股**：顶部输入代码即可加入，每行右侧 `×` 移出（直接改 `config/watchlist.yaml`）；现价/涨跌/成交额/PE/PB/MA20/RSI/信号标签，点行进入个股。
- **个股分析**：蜡烛图 + MA5/10/20/60 + 成交量 + MACD（红涨绿跌，符合 A 股习惯），右侧指标快照；周期可切 **日/周/月 + 1/5/15/30/60 分钟**，勾选「自动刷新(30s)」可盯分钟线；`☆ 加入自选 / ★ 已在自选` 一键切换，另有一键看基本面。
- **回测**：标的以标签形式管理——输入代码回车即添加，也可从「＋ 从自选股选择」下拉加入（最多 8 只，多只按等权组合回测），点标签上的 `×` 移除；选策略/参数/资金/区间，出 12 项绩效卡片、策略 vs 买入持有资金曲线、逐笔交易。
- **选股**：7 个预设直接出表。
- **预警记录**：本地 SQLite 里的历史命中。

支持深链接：`/?tab=stock&code=600519&period=5m`、`/?tab=backtest&code=000001`。

### 5.3 自选股与预警规则

编辑 `config/watchlist.yaml`，保存即生效（无需重启）：

```yaml
watchlist:
  - code: "600519"
    name: 贵州茅台
    rules:
      - {type: price_above, value: 1600}
      - {type: pct_change_below, value: -3}
      - {type: ma_cross, fast: 5, slow: 20, direction: up}
      - {type: volume_spike, ratio: 2.0, window: 5}
```

| 规则 type | 参数 | 含义 |
| --- | --- | --- |
| `price_above` / `price_below` | `value` | 收盘价达到/跌破 |
| `price_near` | `value`,`pct` | 距目标价 ±pct% 内 |
| `pct_change_above` / `pct_change_below` | `value` | 当日涨跌幅（跌用负数） |
| `ma_cross` | `fast`,`slow`,`direction` | 均线金叉/死叉 |
| `price_cross_ma` | `ma`,`direction` | 收盘价穿越均线 |
| `macd_cross` / `kdj_cross` | `direction` | 指标金叉/死叉 |
| `rsi_threshold` | `period`,`op`,`value` | RSI 超买/超卖 |
| `breakout` | `window`,`direction` | 突破 N 日高/低点 |
| `volume_spike` | `ratio`,`window` | 成交量 ≥ N 日均量的 ratio 倍 |
| `drawdown_from_high` | `window`,`pct` | 距 N 日高点回撤 ≥ pct% |

同一规则同一交易日只提醒一次（SQLite 唯一约束去重）。`./quant alert rules` 可随时查表。

### 5.4 定时任务（可选）

```bash
crontab -e
# 每个交易日 15:20 更新日线并生成日报
20 15 * * 1-5 cd /mnt/c/Users/Huawei/Desktop/wsl/quant && ./quant sync >/dev/null 2>&1 && ./quant report >/dev/null 2>&1
# 交易时段每 5 分钟检查一次预警（输出追加到日志）
*/5 9-15 * * 1-5 cd /mnt/c/Users/Huawei/Desktop/wsl/quant && ./quant alert check >> ~/.local/share/aquant/logs/alert.log 2>&1
```

---

## 六、回测的口径与局限

**成交假设**

- 信号在**当日收盘后**用当日及之前数据计算，**次日开盘**成交 —— 天然满足 T+1，且不含未来函数（有单测覆盖）。
- 100 股整手；资金不足 1 手时不交易并给出明确提示（例如 10 万本金买茅台）。
- 成本：佣金万 2.5（最低 5 元）、卖出印花税万 5、过户费万 0.1、单边滑点 2bp，均可在 `config.yaml` 调整。

**绩效指标**：累计/年化收益、年化波动、夏普（无风险 2%）、最大回撤及持续天数、Calmar、交易次数、胜率、平均盈亏、盈亏比、平均持仓、持仓时间占比、同期买入持有、超额收益。

**必须知道的局限**

- 只做多、满仓/空仓两种状态，不支持加仓、止损、融券、分红再投资、停牌撮合细节。
- 复权价回测与真实成交价存在差异；停牌日按数据源实际 K 线处理。
- 未计冲击成本、涨跌停无法成交、ST/退市规则。
- 参数越多越容易过拟合。**样本外验证 + 多标的检验**比调参更重要。

---

## 七、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `No module named pip` / `ensurepip is not available` | Ubuntu 精简镜像缺 `python3-venv`；`setup.sh` 已用 `--without-pip` + get-pip 绕过，无需 sudo |
| 拉数据失败 / 超时 | 公开接口偶发限流；工具会自动回退到本地缓存并在日志里记录。检查 `./quant doctor` 三个源是否可用，必要时调大 `request_timeout` |
| 表格挤成省略号 | 终端太窄；`AQUANT_WIDTH=140 ./quant quote` 或拉宽窗口 |
| 浏览器打不开 `127.0.0.1:8770` | 确认 `./quant web` 仍在运行；WSL2 一般自动转发，若被代理软件拦截可改用 `--host 0.0.0.0` 并访问 WSL IP（`hostname -I`） |
| 页面报 `Unexpected token '<'` | 旧版本 API 出错时会返回 HTML 错误页；现已统一返回 JSON。若仍出现，说明服务是旧进程，重启 `./quant web` 即可 |
| 分钟线只有最近几天 | 分钟线受接口上限约束（1023 根）：1 分钟约 4 个交易日、5 分钟约 3 周、60 分钟约 1 年。分钟线不落库，只在内存缓存 30 秒 |
| 选股报 `RemoteDisconnected` / `Connection aborted` | 全市场列表要分 20~30 页抓取，公开接口偶发断连。现已做三件事：单页失败只重试本页、覆盖率不足 90% 直接弃用（不污染缓存）、东财连续失败自动切新浪并在 15 分钟内熔断。若仍失败，稍等几十秒再点一次即可 |
| 最后一根分钟 K 线一直在变 | 正常：当前未走完的那根 bar，收盘后才定稿 |
| 腾讯 K 线偶发 501 | 旧域名 `web.ifzq` 会被 WAF 拦截，代码已自动切到 `ifzq.gtimg.cn`；两个都失败时回退新浪或本地缓存 |
| 盘中指标总在变 | 设计如此：15:10 前自动剔除当日未收盘 K 线（`data.exclude_intraday_bar: false` 可关闭） |
| 回测 0 笔交易 | 初始资金买不起 1 手；加大 `--capital` 或换低价标的 |
| 财务数据空缺 | 东财财务接口偶发限流，稍后重试；已缓存 12 小时 |

---

## 八、测试与自检

```bash
PYTHONPATH=src ~/.venvs/aquant/bin/python -m unittest discover -s tests -v
```

27 项离线测试覆盖：代码规范化、指标数值与「无未来函数」、规则校验与触发、回测成本单调性、T+1、组合回测、SQLite 幂等写入与预警去重、分钟线报文解析、自选股增删、全市场快照分页容错与备用源切换。全部用合成数据/固定报文，不依赖网络。

---

## 九、可扩展方向

1. **因子库**：把 `indicators.py` 扩展成因子面板，做横截面排序回测。
2. **组合层**：仓位管理（等权/波动率倒数）、行业中性、再平衡周期。
3. **数据源插件**：akshare/tushare/本地 CSV，接口已按可插拔设计。
4. **通知渠道**：`alerts.notify()` 里接 Server酱/企业微信机器人/邮件（注意：只发提醒，不发指令）。
5. **报告增强**：日报加图（matplotlib 或直接内嵌 ECharts 的 HTML）。

---

*再次声明：本项目为个人研究工具，不含任何交易执行能力。市场有风险，决策请自负。*
