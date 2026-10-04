import { useState } from "react";
import { KeyRound, Wallet } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { CopyAddressButton } from "@/components/copy-address-button";
import {
  chainName,
  native,
  nativeSymbol,
  terminalApi,
  time,
  usd,
  type Balance,
} from "@/lib/terminal-api";

export default function WalletPanel({
  wallet,
  balances,
  onSaved,
}: {
  wallet: string | null;
  balances: Balance[];
  onSaved: () => unknown;
}) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  async function submit() {
    setPending(true);
    setMessage("");
    const value = key;
    setKey("");
    try {
      const result = await terminalApi.post("wallet/import", {
        private_key: value,
      });
      setMessage(result.message || "钱包已保存");
      onSaved();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "导入失败");
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>交易钱包</h2>
          <p>各链余额与本机签名配置</p>
        </div>
        <Button
          variant="outline"
          onClick={() => {
            setMessage("");
            setOpen(true);
          }}
        >
          <KeyRound size={15} />
          导入钱包
        </Button>
      </div>
      <section className="wallet-identity">
        <Wallet size={23} />
        <div>
          <b>当前策略钱包</b>
          <code>{wallet || "尚未配置"}</code>
        </div>
        {wallet && <CopyAddressButton value={wallet} label="钱包地址" />}
      </section>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>网络</th>
              <th>原生币余额</th>
              <th>原生币折合 U</th>
              <th>USDT</th>
              <th>更新时间</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {(["bsc", "robinhood"] as const).map((chain) => {
              const b = balances.find((b) => b.chain === chain);
              return (
                <tr key={chain}>
                  <td>
                    <b>{chainName(chain)}</b>
                  </td>
                  <td>
                    {native(b?.native_balance)} {nativeSymbol(chain)}
                  </td>
                  <td>{usd(b?.native_balance_usd)}</td>
                  <td>
                    {b?.usdt_balance == null ? "未获取" : usd(b.usdt_balance)}
                  </td>
                  <td>{time(b?.updated_at)}</td>
                  <td>
                    {b?.status === "fresh"
                      ? "已同步"
                      : b?.status === "error" || b?.status === "unavailable"
                        ? "暂无可用数据"
                        : b?.status === "stale"
                          ? "数据已过期"
                          : "等待同步"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Dialog
        open={open}
        onOpenChange={(v) => {
          setOpen(v);
          if (!v) {
            setKey("");
            setMessage("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>导入交易钱包</DialogTitle>
            <DialogDescription>
              保存到本机加密配置，导入不会启动交易。
            </DialogDescription>
          </DialogHeader>
          <form
            className="wallet-form"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <label>
              钱包私钥
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="0x…"
                minLength={64}
                maxLength={66}
                required
              />
            </label>
            <p className="form-message">
              已有运行进程或未平仓持仓时，钱包替换将被锁定。
            </p>
            <Button type="submit" disabled={pending}>
              {pending ? "正在验证并保存" : "验证并保存"}
            </Button>
            <p role="status" className="form-message">
              {message}
            </p>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
