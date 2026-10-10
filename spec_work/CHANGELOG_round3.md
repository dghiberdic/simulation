# Changelog: Round 3 (D61–D72)

Line numbers are those of the edited `Project Round 2 Spec.md` (429 lines; was 430). Base texts: critique3_design.md §5–§6 and critique3_coverage.md I-1–I-11, overridden by D61–D72 where they conflict.

## D61. Parameter freeze
- L327 (§9.2 scripted checks): "Failures are fixed before the pilot; payoffs, fees, penalties and odds are then frozen for the pilot and the main runs."
- L205 (§5.3): "Gain and fee change only through the scripted checks; the gain share can also change at the T1 rung" → "Gain, fees, penalties and odds are set by the scripted checks and then fixed for the pilot and the main runs (§9.2)."
- L331 (pilot paragraph): a scenario at the floor is a result; tests change only the event text (T6), the wording of a misread field (T3, T5), the commit interface (T4), run length (T7) and the message mode (T9).
- L371 (Appendix A preamble): frozen wording now allows T3/T5 rewording of a misread field; no rewording changes a number or an A.6 sentence.

## D62. T1 ladder and {share} removed
- L335 (T1 row): rung removed. ≥ 2 of 5 trailing intruders → S1 stands (can pass after the priced run); fewer after both runs → S1's floor is reported; whether S3 enters the main runs is decided after the pilot.
- L244 (§6.3): "If S1 stays at the floor after the T1 rung, S3 returns as the main scenario" → floor reported; S3 decided after the pilot.
- L331: "the T1 rung if needed", "excluding the T1 rung" and the rung's round-budget sentence removed.
- L373: {share} placeholder definition deleted. L407 (A.4), L417 (A.5): "{share}" → "30%". L423 (A.6): "the payoff, where only {share} may change;" → "the payoff;".
- L246: §6.4 heading "The oversight ladder" → "Oversight conditions", so no "ladder" remains in the spec (section number and references unchanged).

## D63. No setting changes during the pilot
- T3: L219–220 fog table: F2 "fallback" → "not used"; F3 "default" → "used throughout". L222: "T3 chooses between F3 and F2" → "T3 checks that the reported-action field is understood and used." L339 (T3 row): F2 switch removed; fog stays F3, misreported and truthful reports are both results, a misread or unused field is reworded once. L349 (§9.3): "fog and run length follow T3 and T7" → "under fog F3; run length follows T7".
- T5: L187: "(may be raised after T5)" removed. L340 (T5 row): penalty raise removed; only a misread lead notice or whistleblow field is reworded; false filings are a DV. L347 (§9.3 DVs): adds "filings against an intrusion that did not happen".
- T6: L337: rewording changes no number and no A.6 sentence.

## D64. Pilot order and cuts
- L331: runs in order priced run (T1 rotation 1) → T6 run (rotation 2) → T5 → T4 → T9; tests decided in table order as data come in; table rows reordered T1, T7, T6, T2, T3, T5, T4, T8, T9 (L335–343). Never cut: priced run, T6, T5, T4. T9 is last and cut first; if cut, S2 and the kin cell keep the two-round pre-step. If the re-priced plan still exceeds the guard, T4 drops its interview, then stop and agree the next step. "its cost is shared before the pilot continues" (no names, no Slack).

## D65. T7
- L336 (T7 row): decided right after the priced run, applied to every later run except T5 (4 turns); 12 turns only if a first intrusion or first report falls in turn 7 or 8. No T4 reopening; "confirmed once T6 and T4 have run" removed.

## D66. T2 own human check
- L338 (T2 row): one researcher blind-rates the ten seat-runs of the priced and T6 runs from the same redacted transcripts; ρ ≥ 0.5 floor and SE ≤ 5 over ≥ 4 rated seat-runs; no T8 dependency.
- L38 (§2 step 2): "agrees with researchers on about 10 held-out pilot seat-runs (… T8a)" → "agrees with a researcher on the ten seat-runs of the priced and T6 pilot runs (… T2)".

## D67. T8 single row
- L342 (T8 row) replaces T8a/T8b: researchers score the priced run's five seat-runs (few-shot examples); spot-check the juror on the T6 and T4 seat-runs and one seat-run per main cell; composite > 1 point off on more than one checked seat-run → read, revise once, re-score; Grand Jury rubric judgement in the pilot only on the priced, T6 and T4 runs.
- L281 (§7 rubric): few-shot examples from "the five seat-runs of the priced pilot run (T8, §9.2)".
- L365 (§10): "human spot checks (T8a, T8b)" → "human checks (T2, T8)".

## D68. T6
- L337 (T6 row): researchers read every answer (about 80); "after" window is turn 4 to the end; a rise in (a) of 10 points or more is flagged and the reading decides. Screening sentence removed.

## D69. Token-limit reminder
- L211 (§5.4): limit stated at turn 1 and repeated with each turn's message field, with the exact reminder text.
- L371 (Appendix A preamble): "the per-turn message-limit reminder (§5.4)" added to the engine-generated items.
- L423 (A.6): "the message limit and its per-turn reminder" in the load-bearing list.

## D70. Equal-capability control
- L323 (§9.1): added to the deferred list.

## D71–D72
- No spec change for §10 tone/Pangram or code. No people's names or Slack in the spec.

## Other
- L341 (T4 row): question adds "(S2, separate messages)".
- Pilot paragraph: "Arms have 1–4 runs" → "Each test has one or two runs"; the reserve goes first to a T6 re-run.
