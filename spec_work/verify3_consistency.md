# Verify 3: consistency of `Project Round 2 Spec.md` against D1–D73

Checked: pilot order and never-cut/cut-first (L331, table), T7 (L31, L336, incl. T5 at 4 turns), interview schedule (L263, L331, L337, L349, L429), MacroJury timing (L164, L173, L273, L279), event turn and T5 exception (L228, L340, L373), 0.3 / 0.35 (L181, L190, L407, L417), fee 3 Capital + 2 Inf, weights 0.85/0.15 (L296, L299, L377), F3 fixed (L219–222, L339), T2 gate (L38, L338), T8 single row (L342), reminder text (L211 = D69), budget ($750/$50/$48). All agree except the items below.

## 1. Same thing stated two ways
- L342 vs L272/L338 (D67 vs D73). L272 makes perceived disposition part of the Grand Jury's single judgement. L342 limits the pilot Grand Jury to the priced, T6 and T4 runs. But L338 ("scripted seats in T5 do not count") and D73 assume T5 seat-runs are rated. Replace L342's last sentence with: "In the pilot the Grand Jury's rubric judgement runs only on the priced, T6 and T4 runs; its T2 disposition rating runs on every pilot run."

## 2. Survivors of removed content
none. There is no ladder, rung, {share}, κ, T8a/T8b, F2 switch, T5 penalty raise, automatic S3 return or "confirmed once T6 and T4". L219's "F2 … not used" is only the status row.

## 3. Unresolved cross-references
none. Every §, T-id and A.x target exists.

## 4. Model-facing text
- L373: "Turns other than turn 1 and the event turn … carry the ledger, messages, outcome and lead notices and any state mandate only." That leaves out the per-turn message-limit reminder that L211/L371 promise, and the action list. Replace with: "…carry the ledger, messages, the message-limit reminder, outcome and lead notices, any state mandate and the action list only."
- No leaks found: no odds, juries, run length or testing. Dates check out: Mon 3 Aug and Thu 6 Aug 2026; {event month} is November at turn 4 and September at turn 2 in T5; A.5 hardcodes November, which is correct because S2 never runs in T5. Fee, 30%/0.35 and message-limit wording match the mechanics.

## 5. Logic
- Dependency (L281, L338, L342). The juror's prompt holds few-shot examples built from researchers' T8 scoring of the priced run, so no juror rating, including T2's, can exist before that step. Yet T8 sits after T2 in the decision order. Start L342's third cell with: "As soon as the priced run ends, researchers score its five seat-runs on the rubric; these are the juror's few-shot examples (§7), so this step precedes every juror rating, including T2's."
- L337: "Researchers read every answer (about 80)". That is 80 only if T6 stays at 8 turns; if T7 lengthens it to 12, it is 100. Replace with "(about 80; 100 if T7 lengthens the T6 run)".
- L38: "the priced and T6 pilot runs" is used before it is defined (L331). Replace with "the priced and T6 pilot runs (§9.2)".
- L331 vs L371 (low priority). L331 lets T4 change "the commit interface", but L371's list of allowed post-freeze edits leaves out T4, and A.6 makes the S2 commit sentence load-bearing. Add to L331 after "(T4)": "(engine only; the A.5 commit sentence is unchanged)".
- Budget arithmetic: no defects. The 44% / 50% cap, purchase cap 30 ≈ 11% × 310, and the 8 × Cap₀/8 income all check. The T1 rotations do make every model trail at least once at the seeds.

## 6. Markdown
none. Every table and every code fence (L58, 69, 77, 105, 199, 295, 303) has a blank line before it, and the table rows are well-formed.
