# REA #6404 bench: final table (baseline baec375c3, Opus, $25 cap per arm, one run each)

Blind score = new five-way review (fresh reviewer, reshuffled; 8 items x 1-5 = max 40). Old four-way score in
parentheses. D v0.4.1 / v0.4.2 were not reviewed (unfinished).

| arm | macc | finished | wall s | cost $ | gates | verify:ghidra | oracle | related regressions | out-of-bounds | diff | blind |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A single session | - | yes | 749 | 3.89 | 11/11 | ok | 60/60 | 0 | 0 | 19 +421/-25 | 31 (31) |
| B subagents (3) | - | yes | 1029 | 7.61 | 11/11 | ok | 60/60 | 0 | 0 | 21 +637/-41 | **33** (34) |
| C agent team (2) | - | yes | 721 | 6.27 | 11/11 | ok | 58/60 | 0 | 0 | 21 +649/-34 | 32 (34) |
| D | v0.4.0 | yes (review APPROVE) | 1543 | 7.97 | 11/11 | ok | 60/60 | 1 | 0 | 19 +926/-25 | 25 (25) |
| D | v0.4.1 | **no** (directory in test:focused) | 947 | 5.44 | 10/11 | fail | 59/60 | 8 | 0 | 14 +282/-13 | - |
| D | v0.4.2 | **no** (bridge task ran verify:ghidra alone) | 1461 | 6.52 | 11/11 | fail | 24/60 | 1 | 0 | 14 +827/-11 | - |
| D | v0.4.3 | **no** (real_verifier failed after review REJECT) | 2680 | 13.49 | 11/11 | ok | 56/60 | 0 | 1 (src/hopper/HopperProviderCapabilities.ts) | 22 +1528/-34 | 28 (-) |

Other spend: oracle 5.09, teams smoke 0.39, blind reviews 2.25 + 2.43. Total bench spend ~ $61.4.
