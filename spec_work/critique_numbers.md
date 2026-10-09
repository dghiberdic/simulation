# Numbers critique of the final draft (9 Oct 2026)

Read-only check of `Project Round 2 Spec.md` against `DECISIONS_0810.md`, `SOURCES_0810.md` and `strawman_data.md`. The economy was re-run on a copy of `sim_b`, patched to the final seeds and gain share 0.3. The copy is at `/home/camus/.claude/jobs/68d07d30/tmp/numcheck/sim_final`, with the scripts `check.py` and `ups.py`. No tracked file was touched.

## Verdict

- The economy works as §5.3 claims at the turn-4 event:
  - a trailing lab profits alone (about +0.63);
  - a trailing pair both profit (about +1.05 each);
  - the top two labs lose (about −0.53).
- build_influence at 30 Capital per point stays below intrusion for every trailing seat.
- The pace is about 1.2 C per turn in ordinary play.
- Every seed and constant in §3–§5, §8, §9 and Appendix A matches DECISIONS.
- There is one must-fix: a greedy xAI cannot pay the 5-Capital fee at turn 4.
- There are several wording fixes where §4 overstates its sources, and one false clause in §8.

## Must-fix

1. **xAI cannot afford the 5-Capital intrusion fee at turn 4 if it buys compute greedily** (L173, L395, L405).
   - xAI starts with Capital 15 and earns about 1.9 a turn. Compute costs 0.35 per unit.
   - xAI's Capital over turns 1–3 runs 6.4 → 4.0 → 2.7. After turn-4 income it holds **4.6**, which is under 5.
   - This holds in 100% of 2,000 shock seeds; shocks barely move income. With accelerate +20 it holds 3.9.
   - The pre-check would reject xAI's intrusion, and xAI is the seat with the largest gain (2.83). Under idle pre-play xAI holds 22.6, so this bites only if xAI buys.
   - Minimal fix: a Capital fee of **3 per target**. The fee is already marked provisional, and the EV changes by only 0.01 PS (2 Capital × 0.15/30).
   - Alternative: keep 5 and say in §9.2 that the scripted checks must confirm every seat can pay at the event.

## Should-fix

2. **L300: "it raises the three capability terms" is false for two of the three.**
   - An intrusion raises world capability.
   - Equity falls when the copier is not the lowest lab: the mean rises and the minimum stays put. An xAI solo copy gives equity −0.0065 (GDM is the minimum at turn 8).
   - Concentration moves by about 0.0002 either way.
   - Suggested wording: "it raises world capability and can move concentration and equity either way".
3. **L297: define the integrity denominator and guard it.**
   - "All capability gained" should be stated as Σ over actors of ΔC since t=0, including the intrusion gains.
   - At t=0 the term is 0/0.
   - After one turn, the world's total gain was as low as −0.19 in 500 idle seeds with shocks, which makes the ratio negative or unbounded.
   - At the end of 8 turns the denominator is about 39, and 4-turn T5 runs give about 17, so end-of-run use is safe.
   - Minimal fix: denominator = Σ max(0, ΔCᵢ), and integrity = 1 when it is 0.
4. **L294: the concentration term is inert.**
   - 1 − HHI of C̃ shares is 0.7990 at t=0, against a maximum of 0.8000. It moves by less than 0.0005 under any intrusion pattern tested, including all three trailing labs copying.
   - Rubric item 3 (L278) is mapped onto a term that cannot register anything.
   - No normalisation fixes this: the shares are near-equal by construction.
   - Options: say in §8 that it is inert at these seeds, or set w₂ = 0 and map item 3 to world capability or equity. I would not invent a rescaling.
5. **L144: the Anthropic capital wording overstates the source.**
   - The IPO filing gives 2025 compute ($7.33B) and Q1 2026 compute per revenue dollar (0.71). It gives **no H1 or Q2 2026 compute total** (strawman_data §5).
   - Q2 is a PitchBook ratio (0.56) × Q2 revenue ($11.5B, Bloomberg).
   - Suggested wording: "built up from its first-quarter 2026 compute ratio (IPO filing) and an estimate of second-quarter compute cost, plus its SpaceX fee".
6. **L142: talent periods.**
   - The spec says "2025 for Anthropic, OpenAI and Meta".
   - strawman_data, which re-read the Fortune piece, says Fortune gives **no period** for Anthropic (22:1) or OpenAI (5.7:1). Only Meta (2025) and GDM (Q3 2026) are dated. SOURCES_0810 says 2025 for all three.
   - Suggested wording: "Meta 2025, Google DeepMind Q3 2026, Anthropic and OpenAI undated".
7. **L146: xAI's Influence inputs use a different basis from the other labs.**
   - Users: Grok's 117M from the S-1 counts users of Grok features inside X. The others use Sensor Tower app-only counts, and Meta was deliberately kept app-only (61M) rather than its 1B+ claim.
   - With app-only Grok (50M) xAI's seed is 41.25 → **41**, not 42.
   - Lobbying: xAI's ~$3.0M mixes SpaceX's Q1 2026 in-house figure (secondary source, annualised) with 2025 outside-firm filings, but L146 says "2025 disclosures".
   - Either note both points, or use 41.
8. **L316, hindcast: the prescribed response cannot fire usefully.**
   - The engine gives the frontier lab +2.7 over turns 1–2 under greedy play (+2.1 idle), against a real +4.5. The bottom three get +2.0 to +2.8, against a real +2.5.
   - The frontier gap of 1.8–2.4 sits inside ECI's 90% interval (about ±3), so the test passes.
   - If it ever failed, the 1.3 rerun adds only about 0.2 over two turns.
   - The text already calls it a sanity check. I would add that the engine runs about 2 ECI below the real frontier over those two months: the real frontier moved 2.25 a month.
9. **L138, nit: "the gap between the two groups is about 8 ECI".**
   - The gap between the groups is 7.3 (162.07 − 154.77).
   - The largest seed gap is 8.9. Say "7–9" or "about 7".

## 1. Numbers against DECISIONS

Everything checked matches, with no mismatches:
- **Seeds (§4 table, D1–D5).**
- **§3.1:** a 3.6, ΔK 0.85, pace 1.2 (sensitivity 1.3), a range 2–6, C̃ = C − 100, US stock 2,450 (+150), China 260 (+11), price 0.25 × 1.4, accelerate +20, invest 0.8% (1.008¹² = 1.100 ✓).
- **§3.3:** cap 50%, purchase cap 30.
- **§5.3:** fees 5 + 2, success p, q 0.04 with cap 0.45, lead 0.15, gain 0.3 with minimum 0.35, penalties 10/15/15/5, whistleblow fees 3/5.
- **§5.4:** shock SD 0.7, 500 tokens.
- **§8:** weights 0.85/0.15, the UPS terms (D17).
- **§9:** hindcast +4.5/+2.5, T1 ladder 0.3 → 0.45 → fee 0, ρ ≥ 0.5 and SE ≤ 5, T6 10 points and 20%, T8 κ 0.6 and MAD 1.0, $750/$50/$48, pilot order, MacroJury at turns 2 and 6.
- **Appendix A:** 0.85/0.15; 30% and at least 0.35; 5 Capital + 2 Inf; Mon 3 Aug, Thu 6 Aug and Mon 2 Nov 2026 are the correct weekdays, and turn 4 is November.

## 2. Derived values

| Check | Result |
|---|---|
| Talent sum | Arrival shares 0.957/0.851/0.667/0.750/0.667 give 24.58/21.87/17.14/19.28/17.14, which round to 24.6/21.9/17.1/19.3/17.1 and **sum to 100.0** ✓ |
| Capital G/M/X = 50 × units ÷ 310 | 42.1 / 26.5 / 15.3 → 42 / 26 / 15 ✓. Meta 26.45 rounds down, which is fine. |
| K = C − 3.6 ln(units) | A 142.98, O 141.45, G 134.77, M 135.84, X 137.51. These match strawman_data; the spec states only the rule. ✓ |
| Labs' share at t=0 | 1,076 ÷ 2,450 = **43.9%** ✓ |
| When the 50% cap binds (greedy, 30 per lab) | Turn 1: headroom 149 against 150 requested, so it binds by 1 unit. Turn 2: 75 of 150 granted pro rata. Turn 3: 75. So it binds from turn 1, materially from turn 2. "Starts slack, binds around turns 2–3" (D1) is right in substance. |
| xAI affordability | Fails at turn 4 under greedy play (4.6 < 5): **must-fix 1** |
| Influence seeds, spot-checked | OpenAI: 0.4·0.675 + 0.3·0.610 + 0.2·0.389 + 0.1·1.0 = 0.631 → 30 + 37.85 = **68** ✓. xAI: 0.3·0.610 ($3.0M) + 0.1·0.106 = 0.194 → 41.6 = **42** ✓. Anthropic: 0.808 → 78.5 → 78 ✓. GDM: 0.598 → 65.9 → 66 ✓. Meta: 0.423 → 55.4 → 55 ✓. The lobbying log rule reproduces each component (Anthropic 0.618). |

## 3. Economy re-run with the final seeds and constants

Capability at the event (turn 4, greedy pre-play, shock off): Ant 167.79, OAI 166.48, GDM 158.47, Meta 158.63, xAI 158.37.

Each entry is the expected Prosperity change from intruding. The EV is the §5.3 formula plus the Capital fee at 0.15/30 per Capital. r is the probability that a tip holder files an upheld report.

| Seat | Alone, best target (gain) r=0 | Alone r=1 | Best pair (partner → target) r=0 | Pair r=1 |
|---|---|---|---|---|
| Anthropic | −0.54 (→ any, minimum 0.35) | −0.83 | −0.42 | −0.57 |
| OpenAI | −0.52 (→ Ant, 0.39) | −0.81 | −0.39 | −0.55 |
| GDM | **+0.64** (→ Ant, 2.80) | +0.17 | **+1.05** (with xAI → Ant) | +0.79 |
| Meta | **+0.62** (→ Ant, 2.75) | +0.15 | **+1.03** (with GDM → Ant) | +0.76 |
| xAI | **+0.65** (→ Ant, 2.83) ‡ | +0.18 | **+1.07** (with GDM → Ant) ‡ | +0.81 |

‡ A greedy xAI cannot pay the fee at turn 4 (must-fix 1). With idle pre-play xAI's EVs are +0.74 alone and +1.20 in a pair.

- A trailing lab hitting both top labs alone gets +0.72 to +0.76 (r=0).
- Idle pre-play is within 0.1 of the greedy figures everywhere.

**Over 2,000 shock seeds (greedy pre-play):**
- P(solo EV > 0): GDM 99.9%, Meta 99.6%, xAI 99.7%, OpenAI 4.2%, Anthropic 0.3%.
- The lowest seat profits in **99.95%** of seeds.
- Both top labs lose in 95.6% of seeds.
- The bottom pair both profit against the leader in 100%.
- Idle pre-play gives the same picture: 100% / 96.4% / 100%.

**§5.3 claim: true**, including under always-filed tips.

**build_influence at 30 Capital per point.** Every Capital a seat holds at turn 4 (greedy pre-play), converted to Influence:

| Seat | PS from build_influence | Intrusion EV |
|---|---|---|
| GDM | 0.21 | 0.64 |
| Meta | 0.09 | 0.62 |
| xAI | 0.02 | 0.65 |

- Under idle pre-play GDM's 63 Capital gives 0.32, still below 0.63.
- Adding turn 5–8 income still keeps GDM below: 0.21 + 0.11.
- The stated rationale holds for trailing seats. For the top labs idle Capital beats intrusion, which is intended.

**Frontier pace:**

| Run | Pace (ECI per turn) |
|---|---|
| No event, 12 turns, greedy | 1.23 |
| Greedy, turns 2–3 | 1.24 (turn 1: 1.46, the purchase jump) |
| After the freeze | 1.05 |
| 8-turn run with freeze | 1.15 average |
| Idle | 1.05 |

"About 1.2 per turn" (L63) is right for ordinary play.

## 4. UPS terms

End state of an 8-turn greedy run, shock off, equal weights of 0.2 on the four terms shown.

| Run | World capability | Concentration | Equity | Integrity |
|---|---|---|---|---|
| t=0 | 0.5756 | 0.79900 | 0.9364 | undefined (0/0) |
| No intrusion, t8 | 0.6535 | 0.79896 | 0.9377 | 1.000 |
| xAI solo → Ant, kept | +0.0057 | +0.0002 | **−0.0065** | **−0.068** |
| GDM + xAI → Ant, kept | +0.0112 | +0.0004 | −0.0062 | −0.126 |
| OpenAI → Ant (top lab), kept | +0.0008 | 0.0000 | −0.0011 | −0.010 |
| All three trailing → Ant, kept | +0.0167 | +0.0005 | +0.0188 | −0.177 |

The intrusion rows give changes against the no-intrusion run.

- **Integrity now dominates.** Every kept intrusion lowers UPS: a solo copy changes the four-term sum by −0.014. The old sign problem is fixed.
- **Concentration is inert** (should-fix 4).
- **Equity is near-inert** and can move either way (should-fix 2).
- **World capability:** pace alone moves it by 0.078 over a run, so intrusions are small next to it.
- **Norms:** not simulated. At most ±1 per value per turn, so at most 0.08 over 8 turns, driven by the MacroJury.
- **Forfeited copies:** a forfeited or upheld copy restores every term exactly.

## 5. Source fidelity (§4)

Faithful to the sources:
- ECI values, release dates and the Gemini 3.5 Pro status;
- about 37 benchmarks and the 9 Oct snapshot;
- 1.65 (1,964 → 3,246), Colossus 1 at 27.6, the Colossus 2 slice of 22 (13–28), OpenAI 310;
- 2.04 and 2.1/yr against 3.4/yr; 53 of 231 and 1.67;
- 46% (32–67) and 52% (33–81); 44%;
- Fortune 27 Aug; Brockman, May 2026; 26–35;
- the Menlo, Sensor Tower and Ramp dates;
- 75% US share; "to rounding" (1,473 and 107 against 1,500 and 110);
- price −3%; 14/yr (Feb 2026) and 15.5/yr (April 2026);
- 11% a month.

One caveat: the 15.5/yr figure is known only through a secondary summary (strawman_data §1). That is acceptable, but cite it as such if challenged.

Not faithful: the items in should-fixes 5, 6, 7 and 9.
