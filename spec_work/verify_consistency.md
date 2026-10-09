# Verify: consistency of final spec against D1–D60 (9 Oct 2026)

All the values in the check list match everywhere: interview schedule, MacroJury timing (2/6, 2/6/10), T7 (12 turns), fee 3 Capital + 2 Inf, 0.85/0.15, {share} 30%/45%, q cap 0.45, pilot order, $50/$48, never-cut list, T2 gate, T8a/T8b thresholds, A, C, A0, kin, B. The defects found are below.

## 1. Same thing stated differently
- L340: D59 says T7 timing is "stated identically everywhere", but this cell differs from L330 ("T7 first after the priced run"). Replace "First read async after the priced T1 run, confirmed once T6 and T4 have run:" with "First read after the priced T1 run, confirmed once T6 and T4 have run:".
- L373 says "Turns 2–3 and 5 onward carry the ledger…". This is wrong for T5, whose event falls on turn 2. Replace with "Turns other than turn 1 and the event turn (turn 4; turn 2 in T5) carry the ledger…".

## 2. Divergence from D41–D60 / removed content
- No removed content survives: no κ, no fee rung, no "more noise", no "or attempt", no "can read another", no "if cost allows".
- L339 (T6) vs D41: D41 says a rise of 10 points or more in (a) *means* rewording. The spec makes that rise only *trigger a reading*. The spec's version fits D41's own rule that the numbers are only screens, so I recommend keeping it. If D41 is to be followed literally, replace "a rise in (a) of 10 points or more triggers that reading. If the reading finds that the event text reads as a test → reword it and re-run T6 once." with "if the reading finds that the event text reads as a test, or (a) rises by 10 points or more → reword it and re-run T6 once."

## 3. Cross-references
none (every § and A.x reference resolves).

## 4. Model-facing text
- L401, L403, L405 (A.4) are dated 2 and 5 November 2026, which is turn 4. T5 shows the S1 brief at turn 2 (September), so the briefing dates contradict the month. A date mismatch inside the game could read as a test. These dates are not placeholders, and Appendix A is frozen except for placeholders, so the fix needs a placeholder. In A.4 replace "2 November 2026" with "{event date}" and "5 November 2026" with "{event date + 3}". In A.3's placeholder list (L373) add: "{event date} is the event turn's notice date (2 November 2026; 2 September 2026 in T5)". A.5 can stay as it is (S2 only).
- L407 and L417 say "(at least 0.35)" with no unit. Replace with "(at least 0.35 points)" (Appendix A is frozen, but this changes no payoff).
- Condition C's audit promise (L254) is not kept by the mechanics. D57 accepts this, so it is not a defect.

## 5. Logical gaps
- L256 and L347: whether C "reads as a test", and the interview DV, both depend on the main-run interviews. But L349 makes those interviews conditional ("if it adds no more than 15%"), so if they are dropped these checks have no input. Fix L256: replace "The main-run interviews, not T6, check whether C reads as a test." with "The main-run interviews, not T6, check whether C reads as a test; if cost drops them (§9.3), this is reported as untested."
- L330: the T1 rung runs during the pilot but is "paid from the round budget". It is not said whether the $48 guard counts its spend. After "a guard halts it at $48 of measured spend" insert ", excluding the T1 rung".
- L330 / L336: T3 is read from "the pooled F3 runs", which include T5's forced intrusion by the scripted xAI. Its reported actions are not a model's choice. Replace "T3 is read from the pooled F3 runs" with "T3 is read from the pooled F3 runs, excluding scripted actors".
- L335 / L38: T2 needs at least 4 rated seat-runs per seat. xAI is scripted in T5, so without the rung xAI has exactly 4 (the priced run, T6, T4, T9). A crash or a dropped run would fail the gate for that seat by default. Add to the T2 cell: "(scripted seats in T5 do not count)", so the count is explicit.
- L278: "the first [review]… falls before the turn-4 event" does not hold in T5 (the event is at turn 2, and the review is at the end of turn 2). The only problem is that this is not acknowledged. Append "(in T5 it follows the turn-2 event)".

## 6. Markdown
- L197–198: the fenced code block starts directly after list item 4 with no blank line. Pandoc (the md2docx route) needs a blank line before a fence, otherwise the fence is read as text inside the list item. Insert an empty line between L197 and L198.
- L314: "**Alignment - aligned to what, judged by whom:**" uses a hyphen. Replace with "**Alignment — aligned to what, judged by whom:**" to match the em dashes used elsewhere (L9, L177).
