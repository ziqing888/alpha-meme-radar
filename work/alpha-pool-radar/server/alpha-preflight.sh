#!/usr/bin/env bash
set -euo pipefail

chain="${1:-}"
case "$chain" in
  bsc)
    chain_id=56
    rpc_var=BSC_RPC_URL
    ;;
  robinhood)
    chain_id=4663
    rpc_var=ROBINHOOD_RPC_URL
    ;;
  *)
    echo "unsupported chain: $chain" >&2
    exit 64
    ;;
esac

config_root=/etc/alpha-radar
executor_root=/opt/alpha-radar/okx-dex-executor

set -a
# shellcheck disable=SC1091
source "$config_root/runtime.env"
set +a

read_secret() {
  local name="$1"
  local value
  value="$(<"$config_root/secrets/$name")"
  if [[ -z "$value" ]]; then
    echo "empty secret: $name" >&2
    exit 78
  fi
  printf '%s' "$value"
}

export OKX_API_KEY="$(read_secret okx_api_key)"
export OKX_SECRET_KEY="$(read_secret okx_secret_key)"
export OKX_PASSPHRASE="$(read_secret okx_passphrase)"
export OKX_API_PASSPHRASE="$OKX_PASSPHRASE"
export EVM_PRIVATE_KEY="$(read_secret evm_private_key)"
export BSC_PRIVATE_KEY="$EVM_PRIVATE_KEY"
export EXECUTION_CHAIN_NAME="$chain"
export EXECUTION_CHAIN_ID="$chain_id"
export EVM_WALLET_ADDRESS="$BSC_WALLET_ADDRESS"

rpc_url="${!rpc_var:-}"
if [[ -z "$rpc_url" ]]; then
  echo "missing RPC URL: $rpc_var" >&2
  exit 78
fi
export EVM_RPC_URL="$rpc_url"
export BSC_RPC_URL="$rpc_url"

export OKX_LIVE_ENABLED=0
export OKX_ALLOW_AUTOMATED_TRADES=0

exec /usr/local/bin/node "$executor_root/src/okx_dex_executor.mjs" --preflight --remote-preflight
