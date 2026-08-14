# BIP 110 vs. BIP 54

## Poison Blocks, Sigop DoS Attacks, and Consensus Hardening

---

## 1. The Common Thread

Both **BIP 110** and **BIP 54** address the possibility of constructing valid Bitcoin blocks that are intentionally expensive to validate.

These adversarial blocks are sometimes described as:

> **Poison blocks:** Consensus-valid blocks deliberately engineered to consume excessive validation time or computational resources.

The principal concern is the abuse of legacy signature operations—particularly:

* `OP_CHECKSIG`
* `OP_CHECKMULTISIG`

An attacker can arrange these operations in pathological scripts that remain consensus-valid but take significantly longer than ordinary blocks to verify.

### Core Questions

| Question                                | Consideration                                                                                                  |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Have poison blocks been created before? | Similar constructions have been demonstrated, including on signet.                                             |
| What is the blast radius?               | Potential impact includes block propagation, active miners, node synchronization, and Initial Block Download.  |
| Are existing nodes vulnerable?          | Vulnerability depends on hardware, software version, and whether the node is validating the adversarial chain. |
| Is the attack practical?                | Practicality depends on setup cost, mining access, transaction preparation, and the attacker’s objectives.     |
| Who could execute it?                   | Mining pools, sufficiently funded attackers, or nation-state-level actors are the most plausible candidates.   |
| What are the incentives?                | Disruption, miner targeting, chain degradation, political pressure, or demonstrating a vulnerability.          |

---

## 2. Attack Vectors

There are two primary script locations from which these pathological validation patterns can be constructed:

1. **`scriptPubKey` vector**
2. **`scriptSig` vector**

### High-Level Comparison

| Property                       |              `scriptPubKey` Vector |                                           `scriptSig` Vector |
| ------------------------------ | ---------------------------------: | -----------------------------------------------------------: |
| Preparation required           |                               High |                                                        Lower |
| Setup transactions             |               Approximately 22,000 |                                          Significantly fewer |
| Estimated setup space          |           Approximately 150 blocks |                                                 More compact |
| Transaction standardness       | Setup transactions are nonstandard |                              Can be deployed more discreetly |
| Attack style                   |        Visible, staged preparation |                               More similar to a sneak attack |
| Demonstrated validation impact |     Up to approximately 25 minutes | Approximately 15–30 seconds in current signet demonstrations |
| Addressed by BIP 110           |                        Largely yes |                                                           No |
| Addressed by BIP 54            |                                Yes |                                                          Yes |

---

## 3. `scriptPubKey` Attack Vector

The `scriptPubKey` vector places complex, signature-heavy scripts directly in transaction outputs.

### Attack Signature

The attack requires a large inventory of specially constructed outputs:

| Characteristic             |                                      Approximate Value |
| -------------------------- | -----------------------------------------------------: |
| Setup transactions         |                                                 22,000 |
| Blockchain space required  |                               Approximately 150 blocks |
| Standard relay policy      |                                            Nonstandard |
| Primary operations abused  |                   `OP_CHECKSIG` and `OP_CHECKMULTISIG` |
| Worst-case validation time | Approximately 25 minutes on tested Xeon-class hardware |

Because the setup transactions are nonstandard, they generally cannot propagate through the ordinary public mempool. They would need to be submitted directly to miners or included by a cooperating mining pool.

### Example Construction

See the **Mempool Anarchist Cookbook**:

https://x.com/PortlandHODL/status/1912935936851419424

---

## 4. `scriptSig` Attack Vector

The `scriptSig` vector places the pathological script structure in transaction inputs rather than transaction outputs.

This approach appears to require less visible preparation and may therefore function more like a **sneak attack**.

### Attack Signature

| Characteristic      | Observation                                           |
| ------------------- | ----------------------------------------------------- |
| Preparation         | Less extensive than the `scriptPubKey` vector         |
| Visibility          | Potentially lower                                     |
| Attack style        | More immediate or covert                              |
| Demonstrated impact | Approximately 15–30 seconds of validation time        |
| BIP 110 coverage    | Not fully addressed                                   |
| BIP 54 coverage     | Addressed through per-transaction legacy sigop limits |

Current signet demonstrations suggest that the `scriptSig` vector is substantially less damaging than the worst-case `scriptPubKey` construction. However, it still represents a consensus-valid validation-time attack.

### Examples

Signet transaction:

https://mempool.space/signet/tx/3c961f18fcc47a8f731e4c1a394b3b15cba62c0f8509a11d29d637fdd65697a5

Demonstration and analysis:

https://gist.github.com/0xB10C/75bb5cce79e83057cae31ef06b531dea

---

# 5. Mechanisms of Action

## BIP 110: Limit `scriptPubKey` Size

BIP 110 limits affected `scriptPubKey` scripts to **34 bytes**.

This prevents an attacker from placing sufficiently large and complex scripts directly into transaction outputs.

### Effect

```text
Maximum affected scriptPubKey size: 34 bytes
```

At 34 bytes, there is not enough room to construct the large signature-checking scripts required by the `scriptPubKey` poison-block vector.

### Coverage

| Vector                      | BIP 110 Result                               |
| --------------------------- | -------------------------------------------- |
| `scriptPubKey` sigop attack | Largely prevented                            |
| `scriptSig` sigop attack    | Not prevented                                |
| Duration                    | Temporary; restrictions expire               |
| Approach                    | Restricts script construction by output size |

> **Key limitation:** BIP 110 targets the location used by the known `scriptPubKey` attack rather than placing a general limit on pathological legacy sigop density.

---

## BIP 54: Per-Transaction Legacy Sigop Limit

BIP 54 limits the number of legacy signature operations allowed in any individual transaction.

```cpp
static constexpr unsigned int MAX_TX_LEGACY_SIGOPS{2'500};
```

### Sigop Accounting

Legacy multisig operations are conservatively counted as:

```text
OP_CHECKMULTISIG = 16 sigops
```

This accounting prevents a transaction from concentrating an extreme number of legacy signature checks, regardless of whether the construction is placed in a `scriptPubKey` or a `scriptSig`.

### Coverage

| Vector                      | BIP 54 Result                                    |
| --------------------------- | ------------------------------------------------ |
| `scriptPubKey` sigop attack | Prevented                                        |
| `scriptSig` sigop attack    | Prevented                                        |
| Duration                    | Permanent consensus rule                         |
| Approach                    | Directly limits pathological sigop concentration |

> **Key advantage:** BIP 54 addresses the underlying resource-exhaustion mechanism rather than only restricting one script location.

---

# 6. Validation-Time Effects

## Observed or Estimated Impact

| Block Construction                     |                            Approximate Validation Time |
| -------------------------------------- | -----------------------------------------------------: |
| Normal Bitcoin block                   |     Generally well below one second on modern hardware |
| `scriptSig` demonstration              |                            Approximately 15–30 seconds |
| Worst-case `scriptPubKey` construction | Approximately 25 minutes on tested Xeon-class hardware |

These results are hardware- and implementation-dependent, but they illustrate the difference between the two attack families.

---

## Blast Radius

### Active Nodes

A poison block could temporarily stall nodes as they validate it.

Potential consequences include:

* Delayed block relay
* Increased stale-block risk
* Temporary peer disconnections
* CPU saturation
* Delayed mempool and chain-state updates

### Mining Nodes

Mining infrastructure is particularly sensitive to validation delays.

A miner that spends several minutes validating a newly received block may continue working on the previous tip while competitors have already advanced to the next block.

| Impact                      | Consequence                                 |
| --------------------------- | ------------------------------------------- |
| Slow block validation       | Miner continues hashing on stale work       |
| Delayed template generation | Reduced effective hashrate                  |
| Block relay disruption      | Increased orphan or stale-block risk        |
| Uneven hardware performance | Better-provisioned miners gain an advantage |

### Initial Block Download

A poison block remains part of the blockchain and must be validated by nodes performing Initial Block Download.

A single pathological block may add minutes to IBD. Repeated poison blocks could compound the impact.

| Number of Poison Blocks | Approximate Added Validation Time at 25 Minutes Each |
| ----------------------: | ---------------------------------------------------: |
|                       1 |                                           25 minutes |
|                      10 |                                  4 hours, 10 minutes |
|                     100 |                                 41 hours, 40 minutes |
|                   1,000 |                                Approximately 17 days |

This assumes each block reaches the demonstrated worst case and that validation costs remain roughly additive.

---

# 7. Practicality of a Poison-Block Attack

## Setup Cost

The `scriptPubKey` attack is expensive to prepare because it requires approximately:

* 22,000 setup transactions
* 150 blocks of transaction data
* Direct miner cooperation because the transactions are nonstandard

The attacker would either need to mine the setup transactions or pay a miner to include them.

## Mining Requirements

Producing the final poison block requires access to block production.

Potential attackers include:

| Attacker           | Practicality                                |
| ------------------ | ------------------------------------------- |
| Ordinary user      | Very low                                    |
| Wealthy individual | Possible only with miner cooperation        |
| Small mining pool  | Technically possible but economically risky |
| Large mining pool  | Plausible                                   |
| Nation-state actor | Plausible if disruption is the objective    |

## Incentives

The attack provides little direct financial profit. Its value is primarily disruptive.

Possible motivations include:

* Degrading Bitcoin node availability
* Targeting miners with weaker hardware
* Increasing Initial Block Download times
* Demonstrating political or technical power
* Creating pressure for a consensus change
* Disrupting network confidence
* Causing temporary mining centralization pressure

## Economic Disincentives

A mining pool executing the attack could harm itself through:

* Lost transaction fees
* Reduced mining efficiency
* Reputational damage
* Customer or hashrate departures
* Increased stale-block exposure
* Defensive action from other ecosystem participants

> The attack is technically possible, but the incentives are more consistent with sabotage than profit-seeking behavior.

---

# 8. BIP 110 vs. BIP 54

| Category                   | BIP 110                             | BIP 54                               |
| -------------------------- | ----------------------------------- | ------------------------------------ |
| Primary defense            | Limits affected `scriptPubKey` size | Limits legacy sigops per transaction |
| `scriptPubKey` vector      | Prevented                           | Prevented                            |
| `scriptSig` vector         | Not fully prevented                 | Prevented                            |
| Scope                      | Unintentional                       | Broad and mechanism-focused          |
| Duration                   | Temporary                           | Permanent                            |
| Additional consensus fixes | No                                  | Yes                                  |
| Security completeness      | Partial                             | More complete                        |

---

# 9. Additional Consensus Fixes in BIP 54

BIP 54 addresses more than sigop-based poison blocks. It also proposes fixes for several long-standing consensus edge cases.

## 9.1 Timewarp and Difficulty-Adjustment Manipulation

Bitcoin’s difficulty adjustment relies on block timestamps.

Historical boundary conditions allow miners to manipulate timestamps around difficulty-adjustment periods. This can be used to distort the measured duration of a difficulty epoch and produce repeated downward difficulty adjustments.

BIP 54 addresses:

* The classic timewarp attack
* Abuse of Median Time Past
* Manipulation of the start time of a new difficulty-adjustment period
* Repeated artificial negative difficulty adjustments

---

## 9.2 Duplicate Transaction IDs

Two transactions with identical serialized contents have the same TXID.

Ordinary transactions cannot normally be duplicated because their inputs reference previously created outputs, which cannot be spent twice in the same valid chain.

Coinbase transactions are different:

```text
Previous output hash: 0000000000000000000000000000000000000000000000000000000000000000
Previous output index: 0xffffffff
```

Every coinbase transaction spends from the same special null input. Therefore, without additional uniqueness requirements, separate coinbase transactions could theoretically produce duplicate TXIDs.

### Why Coinbase Transactions Are Special

| Transaction Type     | Can Naturally Reproduce the Same Inputs?                    |
| -------------------- | ----------------------------------------------------------- |
| Ordinary transaction | No, except through a hash collision or invalid double spend |
| Coinbase transaction | Yes, because every coinbase uses the same null input        |

BIP 54 introduces additional coinbase transaction requirements that ensure practical TXID uniqueness.

The first height at which the relevant duplicate-TXID conditions could reappear is:

```text
Block height 1,983,702
```

---

## 9.3 Disabling 64-Byte Transactions

Bitcoin’s Merkle tree uses 32-byte hashes.

An internal Merkle-tree node consists of two concatenated hashes:

```text
32-byte left hash + 32-byte right hash = 64 bytes
```

A transaction containing exactly 64 bytes of non-witness serialized data can therefore be structurally confused with an internal Merkle-tree node.

This ambiguity can complicate Merkle inclusion proofs and create vulnerabilities for simplified verification systems.

BIP 54 makes exactly 64-byte transactions invalid, removing the ambiguity at the consensus layer.

---

# 10. Conclusion

Both BIP 110 and BIP 54 prevent the known `scriptPubKey` poison-block construction that abuses `OP_CHECKSIG` and `OP_CHECKMULTISIG`.

BIP 110 accomplishes this by limiting affected `scriptPubKey` scripts to 34 bytes. This is an effective targeted defense, but it is temporary and does not fully address equivalent constructions using `scriptSig`.

BIP 54 directly limits every transaction to 2,500 legacy sigops. This prevents pathological sigop concentration regardless of whether the attack is constructed through a `scriptPubKey` or a `scriptSig`.

## Final Assessment

| Conclusion                                                | Assessment                              |
| --------------------------------------------------------- | --------------------------------------- |
| Does BIP 110 stop the demonstrated `scriptPubKey` attack? | Yes                                     |
| Does BIP 110 stop the `scriptSig` attack family?          | No                                      |
| Does BIP 54 stop both vectors?                            | Yes                                     |
| Which proposal provides broader security coverage?        | BIP 54                                  |
| Does BIP 54 solve additional consensus issues?            | Yes                                     |
| Is BIP 110 still useful?                                  | Yes, as a temporary targeted mitigation |

> **Security conclusion:** BIP 54 offers more complete and durable protection because it addresses the underlying sigop resource-exhaustion mechanism while also correcting several additional consensus-layer edge cases.
