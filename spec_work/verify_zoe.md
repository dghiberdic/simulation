# Verify: Zoe comments + 8 Oct meeting vs final spec (line numbers = `Project Round 2 Spec.md`)

## Zoe comments
- #0 compute numbers justified: **addressed**. Now 2,450 / +150, derived in L85, L156; old 1,500/110 is reconciled in L156.
- #1 start June or later: **addressed**. Start is 1 Aug 2026 (L31, L128, L377).
- #2/#3 DeepMind talent above OpenAI: **addressed**. OpenAI 21.9 > GDM 17.1 (L133-134); reason given in L150.
- #4 specify shared %: **addressed**. GDM 46% (32-67), Meta 52% (33-81) (L148).
- #5 S1/S2 turn shift, both in every run?: **addressed**. Both events at turn 4, never in the same run (L227, L231, L237).
- #6 defer swap, run kin: **addressed**. L260, L262, L264.
- #7 jury imbalance: **addressed**. A single external juror, Kimi K3, judges every seat (L271, L274, L276).
- #8 rubric maps to UPS: **addressed**. Item-to-term table (L280-288, L310). Item 5 maps to "world capability" only loosely, and the spec says so.
- #9 free-text vs rubric: **addressed**. Merged into one composite after five items (L271, L280, L314).
- #10 fold T0 into T1: **addressed**. L334.
- #11 P3 / no collusion is valid: **addressed**. L337: "valid null result... Payoffs are not raised".
- #12 eval-awareness interview: **addressed**. T6 (L339) and A.7 (L427-429).
- #13 start with 8 turns: **addressed**. L31, L340.
- #14 §10 reads as AI-speak: **partly**. §10 is now short and full of citations (L351-367), but it still contains shorthand like "hence B ≈ A" (see the readability list). No Pangram check is recorded, which belongs to the paper, not the spec.
- #15 keep this brief and add the interview: **addressed**. A.1 is kept (L377). The weights moved to 0.85/0.15 with a reason (L298), and the neutral-brief arm is gone.
- #16 A2A token limit in brief: **addressed**. L391, L210.

## Meeting bullets (granola + notes)
- Start Aug 2026, keep 1-2 early turns as a sanity check: **addressed**. Hindcast in L31, L328.
- Base capability on the Epoch index: **addressed**. L48, L140.
- 8-turn default, lengthen only if needed: **addressed**. L340.
- Check Meta/GDM compute % against Epoch: **addressed**. L148.
- Interview each turn, compare before/after the event, event at turn 4 of 8: **addressed**. L339, L227.
- S1/S2 run separately, same event turn: **addressed**. L227.
- Kin test, all-Claude, S2: **addressed**. L262.
- Symmetric jury, external weaker model is OK: **addressed**. L274.
- Merge judging, few-shot examples, full guidelines, composite score: **addressed**. L280.
- Replace dual comparison runs with human spot checks: **addressed**. T8a/T8b (L341-342).
- Talent: drop SignalFire, try levels.fyi acceptance: **partly**. SignalFire is dropped. levels.fyi and paper-author rate were explored and rejected with reasons; Zeki hires-to-exits is used instead (L150). Zoe needs to sign off on the substitution.
- T0 folded into T1, cost reported: **addressed**. L334: "its cost is shared and the pilot re-priced".
- Test order "eval awareness first ... T4 last": **partly**. L330 runs the priced T1 first and T6 second, which matches the meeting's Next Steps but not the "suggested sequence" bullet. The spec does not explain the choice.
- No collusion is valid, don't tune to force it: **addressed**. L337. The T1 gain-share rung (L334) is about S1 single intrusion, not collusion.
- §10 cleanup (not urgent): **partly**. Same as #14.
- Token reminder in brief: **addressed**. L391.
- Muse Spark for Meta: **addressed**. L28.

## Remaining gaps / regressions
1. Test order is not explained (L330). After "...then T8a." insert: "This follows the 8 October next steps (T1 priced first, then eval awareness); the priced run already carries the every-turn interview, so eval-awareness data starts there."
2. T5 dates are inconsistent. In T5 the freeze falls at turn 2 (September), but the A.4 notices are hard-dated 2 and 5 November 2026 (L401-405). At the end of L373 add: "In T5 the A.4 items are dated 2 and 5 September 2026."
3. The talent substitution needs Zoe's explicit sign-off. No text change is needed; raise it in the review message.

## Readability / AI-filler (replacements)
1. L330: "The pilot has $50, the budget its plan was fitted to on 7 October, and a guard halts it at $48 of measured spend." -> "The pilot budget is $50; a guard halts it at $48 of measured spend."
2. L359: "...hence B ≈ A." -> "...so we expect condition B to differ little from A."
3. L361: "since stated rules helped there, B ≈ A is a real test." -> "since stated rules did help there, that expectation is a real test."
4. L21: "the model playing it is the seat's *actor* ("actor" alone means a lab actor), and it can change (the kin test, §6.5)." -> "the model playing it is the seat's *actor* (\"actor\" alone means a lab actor); the kin test (§6.5) changes which model that is."
5. L31: "they play their labs as of 1 August 2026, a limitation." -> "they play their labs as of 1 August 2026, which is a limitation."
