# MEME 本地交易终端设计

状态：设计稿，尚未导入开源代码、替换面板或改变交易配置。

## 底座选择

直接派生 bytegen-dev/evm-market-maker 的 dashboard 子目录，保留前端工程和通用组件。
参考版本：3f37476efa2e6d5a3bcd8c7427e240f748832010。
上游：https://github.com/bytegen-dev/evm-market-maker
许可证：ISC。导入时保留原 LICENSE、版权声明，并建立 THIRD_PARTY_NOTICES.md，记录版本和修改范围。

选它的原因是 React / Vite / TypeScript、TanStack Query、Recharts、Lucide 与本项目技术接近，已有标签导航、对话框、图表、会话表、日志与 API 客户端。依赖版本与现有 web 不同，独立构建，先验证锁文件和 Node 兼容性，不直接覆盖现有 web/package.json。

这是前端复用方案，不意味着上游已经实现我们的交易产品。上游围绕指定代币、子钱包和成交量循环；我们的业务围绕自动发现、多候选、持仓和真实盈亏。其 App.tsx 也较集中，迁移时按页面拆开。

FreqUI 是 Vue / PrimeVue 且依赖 Freqtrade API，采用它意味着增加另一套框架和更大的协议适配范围。Hummingbot Dashboard 偏 Hummingbot 实例管理，不作为本次底座。

## 页面与代码复用

| 上游模块 | 使用方式 | 我们的结果 |
| --- | --- | --- |
| components/ui、复制按钮、基础格式化 | 直接复用后统一中文和尺寸 | 按钮、对话框、标签页、状态标识 |
| App.tsx 标签导航、加载和错误态 | 提取为终端外壳 | 全局链筛选、纸面/实盘切换、连接状态 |
| OverviewTab | 保留组织方式，替换业务字段 | 资产、持仓、真实盈亏、运行状态 |
| WalletsTab | 复用列表组件，重写数据与操作 | 钱包余额、Gas、导入、当前策略绑定 |
| StartBotDialog | 复用对话框与提交状态 | 按链设置单笔 U 金额、启停与生效回执 |
| SessionsTab | 复用表格和筛选方式 | 交易历史、策略运行批次、配置版本 |
| SessionChart | 复用图表组件与交互，重写序列 | 权益、已实现盈亏、回撤 |
| LogsTab | 复用列表和刷新结构 | 发现、过滤、提交、回执、退出事件 |
| lib/api.ts | 保留请求封装，替换类型和端点 | 本地终端 API 客户端 |

上游 WalletsTab/API 没有现成的私钥导入接口；需要新增。删除成交量目标、子钱包批量资金分发和归集操作，不把这些按钮改名后连接到实盘。

## 首屏

采用紧凑表格终端，默认进入实盘总览。顶部固定链筛选：全部 / BSC / Robinhood；纸面和实盘有明确标识。左侧导航，主体以无外框区域和表格组织，不堆叠大卡片。

顶部指标：总权益、可用余额、持仓估值、已实现盈亏、未实现盈亏。金额以 U 为主，原生币数量作为辅助值。

主体上方是持仓表，下方是最近成交和执行异常；点击币种从右侧打开详情。右侧显示两条链的运行状态、实际单笔金额、报价更新时间、最近未开仓原因。

| 页面 | 核心内容 |
| --- | --- |
| 总览 | 资产、持仓、盈亏、链状态、最近成交 |
| 发现 | 首次发现时间和市值、当前行情、评级、叙事、KOL 数、已核验盈利钱包数、入场判断 |
| 持仓 | 数量、实际成本、当前估值、收益、持有时长、退出条件、GMGN 链接 |
| 成交 | 买卖时间、链、策略、投入和到账、费用、交易哈希、退出原因 |
| 策略 | 每链配置、单笔 U、仓位限制、止盈止损、实际生效版本 |
| 钱包 | 导入、派生地址、各链余额、Gas、配置状态、策略绑定 |
| 日志 | 按链、币种、阶段和失败原因过滤，详情可展开 |

发现页默认隐藏完整钱包地址，显示聚合证据；未核验 KOL 和已核验盈利钱包分别计数。叙事提供来源和时间；缺数据的字段显示未覆盖，不生成虚构解释。

## 数据接入

建议新增 work/alpha-pool-radar/terminal-ui/ 存放派生前端，本地服务最终同源提供构建产物和 /api/terminal/*。开发预览使用独立端口，8771 在验收后切换。

第一步复用 alpha_live_dashboard.py 的白名单数据投影和 SDK 状态读取；新增独立 terminal_api 和 terminal_control 模块，避免把全部控制逻辑继续写进一个 Handler。

| 拟新增 API | 来源与行为 |
| --- | --- |
| GET /api/terminal/overview | SDK 状态、真实进程心跳、余额、账本的统一投影 |
| GET /api/terminal/positions | 两链 SDK positions，加行情及其时间戳 |
| GET /api/terminal/orders | 成交账本和待确认交易，支持分页、链和时间过滤 |
| GET /api/terminal/candidates | 雷达报告、execution-input、过滤原因 |
| GET /api/terminal/performance | 成交成本与结算结果、入出金、权益快照 |
| GET /api/terminal/strategies | requested_config、effective_config、版本和生效时间 |
| POST /api/terminal/strategies/:chain/config | 校验后提交配置，由执行器确认应用 |
| POST /api/terminal/strategies/:chain/control | start / pause_entries / resume_entries，返回操作 ID |
| GET /api/terminal/operations/:id | 查询执行器实际处理结果 |
| GET /api/terminal/wallets | 只返回地址、链、余额、绑定和凭据状态 |
| POST /api/terminal/wallets/import | 本机导入，派生地址并加密保存 |
| GET /api/terminal/events | SSE 推送变更和心跳，断线后补快照 |

现有服务只有状态读取和 roundtrip/start 等少量接口，新增控制、钱包、余额和历史 API 是真实工作量，不能仅靠换皮得到。

行情优先级为持仓、可执行候选、普通观察；页面显示数据真实时间。SSE 加快已有数据到页面的传递，不会让旧行情变新。网络断开、行情过期、进程停止分开显示。

## 配置与执行一致性

前端读取执行器实际配置，不能用 ExecutionConfig.paper() 默认值充当实盘状态。
用户修改后显示“待应用”，收到执行器版本回执后才显示“已生效”。启停按链独立；暂停开仓时仍管理已持仓退出。

当前需求是单笔 5U，换算数量在订单详情显示。最大持仓、最大敞口和每日亏损上限逐项核对执行器是否真正实现；尚未实现的项目标注未启用，不能因为 Python 配置里有字段就显示已生效。
多链合计限额必须通过统一预算预留实现；每链各有三仓不能在总览写成全局最多三仓。

控制操作需要去重和回执，不能直接让网页写账本或拼接任意 shell。启停必须定位链、钱包和运行实例，防止重复启动。界面关闭后由后端继续管理交易。

## 钱包导入体验

本轮默认设计为本机 8771 单用户操作：钱包页打开导入框，填写名称与私钥，后端派生地址，展示公钥地址和链余额，完成加密保存后选择策略绑定。
沿用 Windows DPAPI 的本机保护和现有钱包配置迁移；浏览器字段提交后清空，不进入 localStorage、日志或响应。服务使用同源检查、CSRF 和字段白名单；含密钥表单不加载第三方脚本。

已有策略和持仓绑定 wallet_id，导入另一钱包不会自动改变现有持仓的签名钱包。首版可展示多个钱包，但每条策略只能绑定一个；多钱包并行交易不在首版范围。
公开雷达网页继续读取行情；远程钱包管理需要独立的认证与部署设计，不把 localhost 导入接口直接暴露出去。

## 盈亏定义

买入成本取真实支付与费用，卖出收益取实际净到账，保留原生币和成交时 U 估值。若使用已扣 Gas 的净到账，不能再重复扣一次 Gas。
充值和提现属于资金流，不记成策略盈利。跨链原生币不能直接相加；统一成有时间依据的 U 估值后聚合。
市价估值和按当前卖出路由预计可到账分开显示；没有新鲜卖出报价时不宣称可兑现收益。
旧记录若缺少成本、Gas 或交易时汇率，显示统计覆盖率并排除不完整的净收益汇总。权益曲线从采集起点生成，不回填虚构历史。

## 落地顺序与验收

1. 固定上游版本，导入 dashboard 前端，保留来源声明，构建并形成可预览页面。
2. 接入总览、持仓、成交、发现和日志的真实只读数据，验证时间戳、缺失值、统计口径。
3. 加入钱包导入及策略配置，完成金额、限额和进程状态的端到端回执。
4. 用隔离假钱包及假交易器验证控制流程；浏览器核对桌面、手机、空数据和断线状态。
5. 验收后切换 8771 静态资源；保留旧页面回退路径，交易账本和持仓不迁移重建。

验收关键：前端 5U 必须与执行器实际配置一致；重复点启动不产生两个实例；过期心跳不显示运行正常；报价失联不能显示实时价格；部分卖出正确分摊成本；导入钱包后读接口和日志不返回密钥；老持仓不会改绑新钱包。

## 首版落地状态（2026-09-09）

已固定上游提交 `3f37476efa2e6d5a3bcd8c7427e240f748832010`，将 `dashboard/` 派生到 `work/alpha-pool-radar/terminal-ui`，保留 ISC 许可证与来源说明。复用了 React/Vite 工程和按钮、标签、弹窗等组件，没有接入上游交易引擎。

已切换本机 `http://127.0.0.1:8771/`，原启动命令 `open_okx_dashboard.cmd` 不变。旧页面及 `alpha_live_dashboard.py` 文件保留，需要回退时可单独启动旧服务；新版不开放旧版的 `/roundtrip/start` 写接口。现有交易进程、账本及持仓未迁移、未重启。

已实现四个标签：交易（持仓、真实成交、费用、原生币已记账收益）、发现（候选与未入场原因）、策略（从实际进程核对金额）、钱包（本机加密导入及余额）。收益图按需加载，历史成本不全的记录保留缺失提示；该图不是完整权益曲线。

首版边界：钱包仍是单个当前策略钱包，不支持多钱包选择或独立绑定。有运行进程、持仓或未核实状态时禁止更换。金额表单仅保存草案，尚未接执行器应用回执；没有网页启动/暂停交易按钮。不能把保存草案视为下次启动自动应用。Robinhood USDT 合约未配置，余额列显示未获取；BNB/ETH 与 BSC USDT 后台只读更新，失败标记过期或不可用。

验证：前端生产构建通过；数据、控制、余额与 HTTP 集成共 102 个测试及 16 个子测试通过；浏览器完成桌面、390px 手机布局、四标签、筛选、图表及钱包弹窗检查。未用真实私钥做导入覆盖，也未通过本轮测试发起真实交易。

## 当前实现补充（2026-09-09）

本节描述当前代码，补充并取代上文首版状态中“金额仅保存草案、没有网页启停按钮”的限制；此前设计和验证记录保留为阶段记录，不代表所有设计项已完成。已接入网页执行命令及独立历史记账 sidecar，但尚未通过真实实盘操作验证新控制协议，也不代表历史盈亏已经全部补齐。

### 网页执行命令与回执

`alpha_terminal_server.py` 使用 `ExecutionControl`，策略页通过同源、CSRF 校验的 `POST /api/terminal/strategy/command` 提交命令，通过 `GET /api/terminal/runtime` 读取进程与回执。`POST /api/terminal/strategy/config` 仍只保存金额草案；保存草案不等于生效，也不修改启动器默认值。

| action | 当前实现 |
| --- | --- |
| `start` | 明确确认后，以指定链、当前钱包和 `amount_usd` 启动执行器；已有同链进程或启动冲突时拒绝重复启动。 |
| `pause` | 关闭新增买入，继续按现有策略管理和退出已有持仓。 |
| `resume` | 恢复新增买入，不另起一个执行器。 |
| `apply` | 修改当前执行器后续买入的单笔金额；不改已有持仓、退出规则或启动器默认值。 |
| `stop` | 仅在本地零持仓、无待确认交易，且链上 pending nonce 检查通过时退出；不强制杀进程、不自动清仓。 |

请求包含 `action`、`chain`、`wallet_address`、UUID `request_id`；针对运行中进程的 `pause/resume/apply/stop` 必须提供与当前进程一致的 `expected_pid`（expected PID）。`start/apply` 还需 `amount_usd`。同一 UUID 用于同一请求的去重；改变内容重用 UUID 会被拒绝。

HTTP `202` 的 `submitted/starting` 仅表示受理，不能显示为已生效。运行中命令经每链 `terminal-control` 文件交给新执行器，由 SDK `live-status.json` 中的 `terminal_control` 发布 ack。界面应核对本次 UUID、链、钱包、PID 与新鲜心跳；启动还需核对实际 worker 与 launcher 的父子进程关系。超时、心跳过期、拒绝或无 ack 退出必须保留未确认状态。协议拒绝过期、目标 PID 不符或早于当前进程启动时间的命令。

已经运行的旧版 workers 未被本轮实现修改、热升级或自动重启。它们显示 `legacy_worker`，新命令会返回 `worker_upgrade_required`；需要操作者另行安排、确认当前持仓与待确认交易状态后，手动受控重启以加载新协议。重启网页服务不能升级交易进程。新增进程独占锁不会自动删除历史崩溃遗留锁，遗留锁需人工核对。本次文档更新不执行任何启停、应用金额或交易操作。

### 非阻塞余额与历史记账

服务已组合 `alpha_terminal_data.py` 的白名单投影、`alpha_terminal_balances.Collector.snapshot(wallet)` 与 `alpha_terminal_accounting.Collector.enrich_snapshot(snapshot)`。两个 Collector 的界面查询返回缓存，不等待 RPC。余额只读查询 BSC/RH 原生币与 BSC USDT；RH USDT 合约仍未确认，保持 `null`。BNB/ETH 的 USDT ticker 只用于当前估值，不是历史成交时美元汇率。余额失败或过期不假填零。

记账模块只针对 snapshot 中已成交订单核验链、钱包、交易哈希、回执成功状态和所在区块。默认每 20 秒最多处理 3 笔，单 RPC 超时 3 秒，每轮最多 30 次 RPC、15 秒请求预算；失败退避，最长 300 秒。只读链上数据，唯一持久记账输出为原子写入的 `outputs/terminal-accounting.json`，不改真实 SDK state、fill 或订单账本，不签名、不提交交易。

手续费使用 `gasUsed * effectiveGasPrice`，并加上回执提供的 `l1Fee`。可独立补出回执手续费与买入 `tx_value_native_atomic`；`native_spent_atomic` 是含本笔手续费的原生币总支出，扣除 trace 证实退款后的净买入成本是 `buy_cost_native_atomic`，二者不能混用。

卖出原生币到账需要有效 `debug_traceTransaction` / `callTracer` 内部转移证据；回滚分支和 DELEGATECALL 的继承 value 不算真实收付。trace 不可用或校验失败时，保留已核验 gas/value，净到账、净成本或 PnL 缺什么就保持 `null`，绝不拿区块前后原生币余额差拼出卖出收入。

FIFO 按区块、交易及日志顺序处理钱包实际 ERC20 Transfer 数量，以历史 `balanceOf` 和钱包转账日志核对库存，仅扫描涉及币种的有界历史窗口，默认最多 10,000 个区块，不是全链 indexer。期初库存或外部转入的成本未知时保留未知成本批次；日志不全、归档 RPC 不可用、窗口超限、多资产对价不明确时，不宣称完整成本或盈亏。部分卖出分摊已核验买入费用，卖出净到账扣本笔费用，尾笔保留整数分摊余数，不重复扣费。

费用口径固定为 `fee_scope: transaction_only`，不含独立 approval 授权交易费用；没有补全历史 USD 汇率、充值提现流水或完整权益曲线。不同链的原生币盈亏不能直接相加。

### 展示与验证边界

每单的独立链上证据在 `order.accounting`，与原账本 PnL 分开展示；sidecar 只填订单外层原本缺失的字段，不覆盖 SDK 已有非空记账值。价格涨幅、账本已记账收益、sidecar 核验收益不是同一指标，账本收益图也不等于完整历史净收益或账户权益曲线。

顶层 `accounting` 分别报告 `total`、`attempted`、`receipt_covered`、`gas_covered`、`payment_covered`、`fifo_covered`、`pnl_covered` 和 `persistence_status`。覆盖数不是“所有历史交易已完成”：gas/payment/FIFO/PnL 覆盖都要求 `receipt_status == "verified"`。从持久 sidecar 恢复的数值先标记 `stale`，后台重新核验前不计入当前覆盖率，也不能作为可信 FIFO 依据；缓存写入失败单独显示，不伪装成持久化成功。

新执行命令与记账逻辑已用隔离 Python fixtures、模拟进程/启动器及 Node 协议测试验证；没有通过本轮验证执行真实实盘 start/pause/resume/apply/stop、强制重启旧 workers 或真实买卖。少量历史只读 RPC 核验可取得 gas/value，但未取得有效 trace 的记录仍缺净成本与完整 PnL。测试通过不代表新协议已被旧进程采用，也不代表所有历史盈亏已补齐。

### 旧版切换交接

`work/alpha-pool-radar/prepare_terminal_cutover.ps1` 默认只检查，不改变进程。它核对本机面板中的钱包、链、持仓、待确认订单、新鲜的候选等待状态，以及本 checkout 的 Node/PowerShell 父子进程；再用只读 RPC 检查 chainId 和 latest/pending nonce。检查结果是时间点快照，不是对空闲状态加锁。

操作者自行运行该脚本加 `-StopLegacy`，输入 `STOP LEGACY` 后，脚本再次核查并仅终止已列出的旧 Node 进程，不启动替代进程、不提交交易。旧版没有 graceful drain，刚提交的交易仍可能随后落链；终止后必须重新检查持仓和待确认交易，再由操作者从策略页分别启动新版。不要使用该交接流程处理持仓中、正在下单或无法核实状态的执行器。PID 复用判断按 CIM 可提供的微秒精度核对进程启动时间。

只读实机检查与 `test_terminal_cutover.ps1` 的 12 项隔离检查已通过；停止分支没有在实盘执行。两条原交易进程保持不变。
