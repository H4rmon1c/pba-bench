# pba-bench: Poison Block Attack benchmark report

## Environment

- Node: `Bitcoin Core daemon version v31.1.0 bitcoind`  (RPC version 310100, subversion `/Satoshi:31.1.0/`)
- bitcoind: `/usr/local/bin/bitcoind`
- CPU: Intel(R) Xeon(R) CPU E5-2680 0 @ 2.70GHz (32 logical / 16 physical cores)
- OS: Linux 6.12.38+deb13-amd64 (x86_64)
- RAM: 270.41 GB

## Results per construction

### N=8500 inputs, K=100 CHECKSIG/input

- Legacy sigops (BIP 54 accounting): **850000**  (BIP 54 limit: 2500)
- Poison tx size: 964.69 KB; weight 3858768 (limit 4,000,000)
- Prep blocks: 44
- Expected sighash preimage bytes (cache-aware): 2.99 GB; theoretical no-cache: 299.31 GB

| metric | median | min | max | n |
|---|---|---|---|---|
| validation wall time (s) | 85.0837 | 85.0837 | 85.0837 | 1 |
| validation CPU time (s) | 85.1200 | 85.1200 | 85.1200 | 1 |
| peak RSS | 162.39 MB | 162.39 MB | 162.39 MB | 1 |
| RPC probe max latency (s) | 24.9363 | 24.9363 | 24.9363 | 1 |

- Run `custom-1786672240-c1-r1`: **accepted**

## Overall outcome

- accepted: 1
