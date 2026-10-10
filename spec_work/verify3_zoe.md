# verify3: Zoe comments + 08-10 granola/notes vs final `Project Round 2 Spec.md`

## Comments #0–#21
- #0 solved — L85, L156 "2,450 units ... growing 150"; "same method gives 1,500 units"
- #1 solved — L31 "Monthly turns from 1 August 2026"
- #2 solved — L31, L377 "since 1 August 2026, ranked"
- #3 solved — L133–134 OpenAI 21.9 > GDM 17.1; L150 "Google DeepMind's place below Meta"
- #4 solved — same as #3 (L132–136, L150)
- #5 solved — L148 "assumed to use 46% (90% CI"; Meta 52%
- #6 solved — L228 "S1 and S2 never run together; both events fall on turn 4"
- #7 solved — L337 "before the event (turns 1–3) and after"; L349 turns 3, 4, 5, final
- #8 solved — L261 "seat swap ... stays on hold; the kin test runs"
- #9 solved — L272, L275 "no juror ... shares a family with a seat"
- #10 solved — L281–289 "Each item is tied to one term of the UPS index"
- #11 solved (moot after merge) — L272 "One judgement per actor: rubric scores"
- #12 solved — L281 "one composite alignment score ... holistic"; L342 T8 "Human spot checks"; no dual runs remain
- #13 solved — table L335–343 order T1, T7, T6, T2, T3, T5, T4, T8, T9; L331 "decided in the order of the table"
- #14 solved, one ambiguity (gap 1) — L341 "Payoffs are not raised to force a joint intrusion"; L331 "Tests change only the event text"
- #15 solved — L331 "The priced run is the usage calibration"; L335 T1 "(usage calibration)"
- #16 solved — P3 gone; L341 "a valid null result, reported"; L331 "A scenario at the floor is a result"
- #17 solved — L427 "do you think you did a good job? Why"; L337 codes "any reason beyond the Prosperity Score"; old 25%/20% proxies gone
- #18 solved — L31 "8 turns by default"; L336 T7
- #19 deferred-by-agreement — L351–367 unchanged; granola "clean up before final paper ... (not urgent)"; no Pangram mention (paper-side)
- #20 solved — A.1 L377 single brief; neutral-brief arm removed (no "neutral" in spec); note weights now 0.85/0.15 (L299 justified)
- #21 solved — L391 "up to 500 tokens (about 350 words)"; L211 per-turn reminder

## Granola — Model spec review
- Start Aug 2026 — solved — L31 "from 1 August 2026"
- Closer to now = harder to dismiss — solved (implicit by L31 start date)
- Retain 1–2 early turns as sanity check — solved — L329 "Hindcast ... over turns 1–2"
- Capability on Epoch index — solved — L48, L140 "Epoch AI's Capabilities Index (ECI)"
- 8-turn default, increase only if needed — solved — L336 "lengthen ... only if the first event"
- Events early / fewer turns — solved — L228 "three turns ... four after"
- Meta/DeepMind compute % from Epoch — solved — L148 "following Epoch's Chip Users model"

## Granola — Experiment design
- Eval-awareness interview each turn, beyond-score check — solved — L337 "Interview after every turn"; L427
- Before vs after injection — solved — L337 "before the event (turns 1–3) and after it"
- Injection at turn 4 of 8 — solved — L228 "both events fall on turn 4"
- S1/S2 separate, same turn — solved — L228 "never run together"
- Kin test all-Claude, S2 — solved — L263 "S2 under condition A, with Claude in every seat"
- Symmetric jury / external / weaker OK — solved — L272 "Kimi K3, one open-weight model"; L275 "need not match the seats' capability"
- Merge free-text + rubric; few-shot; full guidelines; composite — solved — L281 "full rubric guidelines and few-shot calibration examples"
- Dual runs → human spot checks — solved — L342 "Researchers score the priced run's five seat-runs"
- Replace SignalFire — solved — L150 "SignalFire's 2026 report no longer gives lab-level"
- levels.fyi offer-acceptance proxy — partly (explored, no data) — L150 "levels.fyi publishes compensation only"
- Prefer offer acceptance over retention/hire count — partly (flow measure used, flagged) — L150 "This is a flow measure, not offer acceptance"
- T0 into T1; note 8-turn cost first — solved — L331 "its cost is shared before the pilot continues"
- Sequence eval awareness, T1, T2, T3, T5, T4 last — solved — L331 run order "priced ..., T6 ..., T5, T4, then T9"
- No collusion valid; no tweaking to force it — solved — L341 "Payoffs are not raised"
- Section 10 tone — deferred-by-agreement — not urgent per granola
- Token limit in shared opening brief — solved — L391 "anything longer is cut off"

## Granola — Next Steps
- Implement spec + update repo models — outside spec (code still old design per memory)
- Done by Fri 9 Oct / tests Mon 12 — outside spec
- Run T1 (8-turn, T0 folded), share cost async — solved — L331 "its cost is shared before the pilot continues" (Slack is process)
- Decide async if 8 turns enough — solved (rule-based) — L336 "Decided right after the priced run"
- Eval-awareness test after T1 cost confirmed — solved — L331 T6 run follows priced run, after re-pricing
- Injection t4, interview each turn, compare — solved — L337, L429 "Used every turn in the priced T1 run and T6"
- Zoe review Sat 10 Oct / start Sun 11 — outside spec
- Through T6 before next meeting; T4 after — consistent — L331 order puts T6 second, T4 later

## Notes file
- "start later" — solved — L31; "muse spark for meta gen?" — solved — L28 "Muse Spark 1.3 (`muse-spark-1.3`)"
- levels.fyi accepted offers — partly (no data) — L150; "author paper rate" — rejected with reason — L150 "A paper-author rate was considered and rejected"
- few-shot calibration; mix rubric w/ freetext — solved — L281; turn 4 of 8 — solved — L228; interview each turn "doing well" — solved — L427

## Remaining gaps / regressions (only)
1. #14 ambiguity (L327): scripted checks "catch ... an intrusion payoff from which no seat or pair can profit" and "Failures are fixed before the pilot", which reads as permission to raise payoffs to make joint intrusion pay before any is seen. Replace "Failures are fixed before the pilot;" with: "Failures are fixed before the pilot; a payoff fix only makes intrusion feasible at the seeds and never raises it to induce intrusion;"
2. Talent (granola: index on offer acceptance) is not met for lack of data; L150 already says so. No text change; raise it with Zoe as a decision for her.
No regressions found on #7, #9, #12, #13, #17 or #21.
