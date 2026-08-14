---
name: Research proposal
about: Propose a new strategy, sleeve, or portfolio-construction change
labels: research
---

**Before you open this:** check `reports/*.json` for an existing `decision`
on something similar, and read `AGENTS.md`'s "Research conventions" and "Why
externally-sourced strategies keep failing here" sections — several
plausible-sounding ideas are already tested and rejected, with reasons.
Overriding a prior decision needs new evidence, not a fresh opinion.

**Hypothesis**

**Objective class**
(`return_enhancer` / `risk_reducer` / `cost_reducer` / `diversifier` — see
`backtest/promotion.py`'s module docstring for which bar applies)

**Which of the four recurring failure modes does this need to survive?**
(selection bias / beta mistaken for alpha / constraint mismatch / gate
strictness on noisy statistics — see AGENTS.md)

**Proposed backtest methodology**
