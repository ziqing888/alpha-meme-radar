# GMGN 推特监控 OG 自动搜索脚本 - macOS 使用说明

这个脚本是浏览器用户脚本，macOS 可以直接运行。推荐用 Chrome 或 Edge + Tampermonkey，最稳。

## 推荐方式：Chrome / Edge

1. 在 Mac 上安装浏览器扩展：Tampermonkey 或 Violentmonkey。
2. 打开扩展，选择 `Create a new script`。
3. 删除默认内容。
4. 把这个文件的全部内容粘进去：

   `work/alpha-pool-radar/gmgn_cz_og_autosearch.user.js`

5. 保存脚本。
6. 打开 `https://gmgn.ai/` 并登录你的 GMGN。
7. 在 GMGN 自带的推特监控里添加你要看的账号。
8. 脚本会读取 GMGN 页面和接口返回里的监控内容，命中后自动在 GMGN 页面弹出 OG 候选面板，并尝试填搜索框。

## Safari 方式

Safari 也能用，但兼容性取决于用户脚本扩展。

1. 从 Mac App Store 安装 `Userscripts` 或 Tampermonkey for Safari。
2. 给扩展授权访问 `gmgn.ai`。
3. 新建脚本，把 `gmgn_cz_og_autosearch.user.js` 内容粘进去。
4. 保存后刷新 GMGN 页面。

如果 Safari 里接口拦截不稳定，换 Chrome / Edge。

## 添加你要监控的账号

在脚本里找到：

```js
const WATCH_ACCOUNTS = [
  "cz_binance",
  "heyibinance",
  "binance",
  "binancewallet",
];
```

你可以这样加：

```js
const WATCH_ACCOUNTS = [
  "cz_binance",
  "heyibinance",
  "binance",
  "binancewallet",
  "bnbchain",
  "elonmusk",
];
```

不要带 `@`。

脚本也内置了币安广场来源别名：

- CZ / 赵长鹏 / Binance Square CZ / 币安广场 CZ
- 何一 / Yi He / He Yi / Binance Square He Yi / 币安广场 何一
- Binance / 币安官方
- Binance Wallet / 币安钱包

## 添加人工 OG 映射

有些推文对应的 OG 盘不是字面同名，自动搜索可能找不到。就在 `RULES` 里加：

```js
{
  id: "unique_name",
  author: "cz_binance",
  statusId: "推文ID",
  symbol: "OG",
  token: "0x...",
  chain: "bsc",
  sourceUrl: "https://x.com/cz_binance/status/推文ID",
  note: "Manual override",
}
```

## 当前脚本边界

- 只运行在 `https://gmgn.ai/*`。
- 不直接监控 X。
- 不连接钱包。
- 不下单。
- 只做 GMGN 页面里的搜索框填充和候选面板展示。
