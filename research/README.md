# Research trail

Curated primary-source notes used to design the benchmark. The raw downloads
(`*.html`, `*.diff`, `*.ipynb`) are kept out of git; see the links to fetch them.

| File | Source | What it is |
|---|---|---|
| `bip54.txt` | https://bips.dev/54/ | BIP 54 "Consensus Cleanup" spec: 2,500 legacy sigop limit, sigop accounting, rationale. |
| `feature_bip54.py` | https://github.com/bitcoin/bitcoin/pull/35793 | Bitcoin Core's BIP 54 functional test (the `bad-txns-legacy-sigops` rejection, and the `OP_DUP OP_NOTIF … OP_CHECKMULTISIG` limit-hitting pattern). |
| `gist_e5409f3ea5825e9d5bbc8dcdbac6d576_README.md` | https://gist.github.com/portlandhodl/e5409f3ea5825e9d5bbc8dcdbac6d576 | Portland HODL's "BIP 110 vs BIP 54" write-up describing the `scriptPubKey` and `scriptSig` attack vectors and reported figures. |
| `signet_notebook.txt` | https://gist.github.com/0xB10C/75bb5cce79e83057cae31ef06b531dea | 0xB10C's signet notebook measuring block *propagation* and validation duration during slow-to-validate blocks. |

## Post-BIP54 worst-case research (this work)

| File | What it is |
|---|---|
| `BIP54_BOUNDARY_MAP.md` | Every BIP54 rule mapped to its Bitcoin Core source, constant, reject reason, regtest behaviour, and the resource it bounds; the live 2500-sigop boundary; the key architectural facts (per-block sigop cap does not count spent-scriptPubKey sigops; `OP_CHECKMULTISIG` adds `nKeysCount` to the op budget). |
| `POST_BIP54_WORST_CASE.md` | The empirical answer to "what becomes the dominant worst-case after BIP54": a BIP54-valid split CHECKMULTISIG block reaches ~104 s single-threaded — comparable to the pre-BIP54 poison. |

The BIP54 reference implementation tested is Bitcoin Core PR #35793
(commit `9630491bf2135d03dac586d3492cfca9939f6fbb`); see
`BIP54_BOUNDARY_MAP.md` for build provenance and binary SHA-256.

## Other sources consulted

* Bitcoin Core source (v31.1.0): `src/script/interpreter.cpp` (legacy `SignatureHash`,
  `SigHashCache`, `CheckECDSASignature`), `src/script/script.cpp` (`GetSigOpCount`),
  `src/consensus/tx_verify.cpp` (block sigop cost), `src/policy/policy.h` /
  `policy.cpp` (`MAX_TX_LEGACY_SIGOPS`, `CheckSigopsBIP54`), `src/script/script.h`
  (`MAX_SCRIPT_SIZE`), `src/script/interpreter.h` (`MAX_OPS_PER_SCRIPT`).
* https://www.bitmex.com/blog/attack-blocks — BitMEX overview of attack blocks.
* https://delvingbitcoin.org/t/great-consensus-cleanup-revival/710 — BIP 54 /
  worst-case validation discussion.
