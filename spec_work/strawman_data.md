# strawman_data: adversarial check of SOURCES_0810 (9 Oct 2026)

Re-downloaded Epoch `benchmark_data.zip`, `ai_chip_users.zip`, `ai_chip_sales.zip`, `data_centers/data_centers.csv` and `data_center_timelines.csv` today and recomputed. Also used the SpaceX S-1 (SEC EDGAR, full text), the Meta Q2 2026 call transcript, and the Hugging Face API.

## Summary

| Item | Proposed (SOURCES_0810) | Verdict | Conf. |
|---|---|---|---|
| ECI seeds (5 labs) | 162.78 / 162.07 / 154.77 / 154.21 / 153.92 | **Keep.** All five values and dates match today's download exactly. No public model was missed. | high |
| Gemini 3.5 Pro by 31 Jul | not released | **Keep.** Still not generally available as of 20 Sep. | high |
| Frontier pace | 1.2 ECI/turn | **Keep 1.2.** State 1.2–1.3 as the sensitivity range. | medium |
| Anthropic Colossus 2 slice | unknown (Esc. #1) | **Change: ≈22 units (≈90k GB200), range 13–28** | low-medium |
| Anthropic compute | 224 (+?) | **Change to 246** (196 + C1 27.6 + C2 22) | medium |
| xAI compute | ≤121 | **Change to 95** (range 85–110) | medium-low |
| OpenAI compute | 288 | **Change to 310** (range 288–320) | medium-low |
| GDM / Meta compute | 261 / 164 | **Keep** | low |
| Uniform 1.65× vs 2.04× (Esc. #2) | 1.65× | **Keep 1.65× as the base and reject 2.04×.** Add only lab-specific adjustments that are documented. | medium |
| Talent | A23.7 O21.1 G16.6 M18.6 X20.0 | **Change xAI to 17.1:** A 24.6 / O 21.9 / G 17.1 / M 19.3 / X 17.1 | low-medium |
| Capital: OpenAI | 50 | **Keep** | medium |
| Capital: Anthropic | 26 | **Change to 28** (calendar-2026 basis, range 26–35) | low-medium |
| Capital: GDM/Meta/xAI | 45 / 28 / 21 | **Rescale with the new compute: 42 / 26 / 15** (anchor OpenAI 310) | low |
| Influence: xAI | 40 (proxy) | **Change to 42** (SpaceX parent lobbying, Grok MAU 117M from S-1) | low |
| Influence: others | 78 / 68 / 66 / 55 | **Keep.** No 2026 Menlo data exists. | low |
| US stock / growth | 2,450 / +150 | **Keep.** Independently re-derived as 2,438. +150 is slightly conservative. | medium |
| China stock / growth | 260 / +11 | **Keep** | low |
| Juror | Kimi K3 `kimi-k3` | **Keep**, and pin the HF revision and log the model id | medium-high |

---

## 1. Capability (ECI)

- **Values.** Every seed and runner-up value in SOURCES_0810 §1 matches `eci_scores.csv` downloaded today (9 Oct 2026). The post-start values match as well.
- **Release dates checked against the press:**
  - Claude Opus 5: 24 Jul. Most outlets give 24 Jul; one gives 25 Jul, a time-zone difference ([Android Authority](https://www.androidauthority.com/anthropic-launches-claude-opus-5-3691255/)).
  - Gemini 3.5 Pro: still not GA on 20 Sep 2026. Only 3.5 Flash shipped; 3.6 Flash became the default on 21 Jul ([AIToolsReview, Oct 2026](https://aitoolsreview.co.uk/insights/gemini-3-5-pro); [evolink](https://evolink.ai/blog/gemini-3-5-pro-api-release-watch)).
  - Grok 5: not released. Musk said on SpaceX's Aug call that it is planned before end-2026 ([CometAPI, 8 Aug 2026](https://www.cometapi.com/what-is-grok-5-expected-specs-features-performance/)).
- **Missed models?** None among publicly available models. Epoch's `model_metadata.csv` has three non-public frontier releases from these labs before 31 Jul, and none has an ECI:
  - Claude Mythos Preview (7 Apr, "Limited access")
  - Gemini 3 Deep Think (12 Feb, "Hosted access (no API)")
  - GPT-5.4-Cyber (14 Apr, limited)
  - **Add the rule to the spec:** "best *publicly available* model with an Epoch ECI score". Mythos may otherwise be raised against the Anthropic seed, and Deep Think against GDM's.
- **Pace.**
  - The Epoch Trends dashboard still reads "14 points per year since the introduction of reasoning models" (90% CI 12–17). It was last updated 5 Feb 2026 ([epoch.ai/trends](https://epoch.ai/trends), fetched today).
  - Epoch's April 2026 piecewise analysis is reported at **15.5/yr (13–18)** after Apr 2024 (secondary summary: [Longterm Wiki](https://www.longtermwiki.com/resources/b029bfc231e620cc)). That is 1.29/month.
  - SOURCES' own fit gives 1.21–1.26.
  - Verdict: keep **1.2**, cite both Epoch figures, and run a sensitivity check at 1.3.

## 2. Anthropic's slice of Colossus 2

The size of the slice has not been disclosed. The facts that bound it:
- **SpaceX S-1** ([EDGAR](https://www.sec.gov/Archives/edgar/data/0001181412/000162828026036936/spaceexplorationtechnologi.htm), read in full today):
  - The Cloud Services Agreements with Anthropic cover "access to compute capacity across COLOSSUS and COLOSSUS II". Anthropic pays **$1.25B/month through May 2029**, with "capacity ramping in May and June 2026 at a reduced fee". Either side can terminate on 90 days' notice.
  - Colossus II's first cluster is "approximately 110,000 GB200 processors, approximately 210 megawatts". The second cluster is 110,000 GB300 and 220 MW. Grok 5 is "currently being trained at COLOSSUS II".
- **Epoch.** The 110k-GB200 cluster is **27.8 units** in Epoch's timeline (2.53 H100e per GB200). Epoch puts Colossus 2 at 111.2 units from 15 Jun 2026 and lists users as "Anthropic, Cursor, SpaceXAI".
- **Price anchor.** Google pays SpaceX **$920M/month for ≈110,000 Nvidia GPUs**, about $8.4k per GPU-month. Payments start Oct 2026, and access ramps "through September" ([TechCrunch, 5 Jun 2026](https://techcrunch.com/2026/06/05/google-will-pay-spacex-920m-per-month-for-compute/)). Musk said Colossus 1 is all Anthropic's, so Google's GPUs are most plausibly in Colossus 2.
- **The estimate:**
  - An analyst puts Colossus 1's rent at about $6B/yr ([Freda Duan](https://x.com/FredaDuan/status/2057526882694987837)). That leaves about $9B/yr for Colossus 2.
  - At Google's rate, $9B/yr buys about 90k GB200, which is **≈ 22 units**.
  - Pricing Colossus 1's Hopper capacity at Google's per-H100e rate instead gives about 13 units.
  - The ceiling is the whole GB200 cluster, 27.8 units.
  - **Use 22 (range 13–28).** The SOURCES suggestion of a ⅓ split (37 units) exceeds the entire GB200 cluster and is not supported.

## 3. Lab compute at 31 Jul 2026

**Projection method.** Keep the stock-matched 1.65×; reject 2.04×.
- Epoch's own chip-sales data gives the current growth of the non-Chinese stock as about **20% per quarter, ≈2.1×/yr**: Q2 2026 sales of 604 units on a stock of 3,045.
- The 3.4×/yr on the Trends page is a long-run historical rate, so 2.04× over 7 months has no current support.

Per-lab checks from Epoch's Frontier Data Centers timelines, Dec 2025 → 31 Jul 2026, computed today:

| Lab | Evidence | Preferred | Range |
|---|---|---|---|
| OpenAI | OpenAI-attributed sites +145 units (Fairwater Atlanta/Wisconsin, Abilene 25.5 → 50.9, CoreWeave). 174 + 145 = 319 if all of it is OpenAI's. Fairwater is tagged "OpenAI #likely, Microsoft #likely". 1.65× gives 288. Friar: 1.9 GW at end-2025 (≈ Epoch's 174); no mid-2026 GW figure has been published ([DCD](https://www.datacenterdynamics.com/en/news/openai-cfo-says-company-ended-2025-with-19gw-of-compute-scaled-revenue-at-same-speed/)). | **310** | 288–320 |
| Anthropic | Non-Colossus sites 1.65× (New Carlisle 47 → 69, Ridgeland, etc.) → 196. Plus Colossus 1 (27.6, Epoch: in use from 1 Jun). Plus Colossus 2 slice ≈22. Google TPU ("well over a GW in 2026") and Rainier (~500k Trn2) have no mid-2026 online counts. | **246** | 237–252 |
| Google DeepMind | Google-owned frontier sites 1.58×. TPU cumulative sales 371 → 698 by 2 Aug (Epoch). Much of that goes to Anthropic and external customers. The SpaceX rental is for Gemini Enterprise and mostly after 31 Jul. | **261** | 230–290 |
| Meta (MSL) | The 3.9× growth in Meta's sites is a small-base artefact: Epoch's frontier sites cover only 53 of the 231 units Meta owned at end-2025. The +154 units added on Meta sites raise Meta's owned fleet ×1.67, which **matches 1.65×**. | **164** | 150–200 |
| xAI | Colossus 2 at 111.2 − Anthropic 22 − Google ramp (0–15; payments start Oct, so take about 5) + off-site cloud (end-2025 63.5 − 56.5 on sites = 7, × 1.65 ≈ 11) ≈ **95**. Colossus 1 goes to Anthropic. | **95** | 85–110 |

Labs then hold 1,076 units, **44%** of US stock, so the 50% cap stays slack. With C = ECI and a = 3.6, K becomes A 143.0, O 141.4, G 134.7, M 135.9, X 137.5.

## 4. Talent

- **Matched-period per-lab data for all five labs does not exist publicly.** The Zeki numbers in Fortune (27 Aug 2026) are confirmed: GDM 2:1 in Q3 2026, Meta 3:1 in 2025, and OpenAI 5.7:1 and Anthropic 22:1 with no period given. **xAI is not covered** ([Fortune](https://fortune.com/2026/08/27/google-deepmind-losing-talent-to-rival-ai-labs-startups-new-data-show/)).
- **Metix in/out flows are unusable.** The table is garbled. Its in/out counts contradict its own retention column; for Anthropic, 87.5% retention implies about 390 leavers, while the table shows 1,233. Its xAI outflow of 40 contradicts the documented exodus ([Metix, 11 Jun 2026](https://metix.ai/reports/mapping/frontier-ai-labs-talent-2026)).
- **Offer acceptance.** The only figure found is **Anthropic 88% of technical offers** (SignalFire, via [HeroHunt](https://www.herohunt.ai/blog/what-openai-and-anthropic-pay-engineers-2026/)). Nothing exists for the other four labs. levels.fyi, Blind, Rora, Candor and Harnham publish compensation and negotiation data, not acceptance rates. This cannot be used across labs.
- **Is the arrival-share construction defensible?** Yes, as an *ordinal* proxy with stated caveats. The ordering A > O > M > G agrees with SignalFire's 2025 flows and with Zeki's DeepMind exit destinations.
- **xAI: use 17.1, not 20.0.**
  - 20.0 rests on Metix's visible retention, which by Metix's own admission does not reflect the 2026 exodus.
  - Evidence of net shrinkage in 2026:
    - All 11 non-Musk co-founders have gone.
    - The Grok research staff fell from about 200.
    - Meta hired at least 11 people from xAI and Thinking Machines at least 7 ([summary of reporting](https://www.metaintro.com/blog/xai-exodus-ai-talent-wars-2026); [TNW](https://thenextweb.com/news/xai-all-cofounders-departed-musk-spacex-rebuild)).
  - Net shrinkage implies a hires-to-exits ratio below 1, i.e. an arrival share below 0.5, and a talent share near 12. Setting xAI equal to the lowest measured lab (17.1) is the conservative and defensible choice.
  - Result: **A 24.6 / O 21.9 / G 17.1 / M 19.3 / X 17.1.**

## 5. Capital

- **OpenAI: $50B.** Keep. It is confirmed by Brockman's testimony (Bloomberg Law, 5 May 2026). No later revision was found.
- **Anthropic.**
  - 2025 compute: $7.33B (S-1, via [PYMNTS](https://www.pymnts.com/news/artificial-intelligence/2026/anthropic-prospectus-shows-what-2-trillion-dollar-ai-company-costs-run/)). Q1 2026: $0.71 of compute per revenue dollar. Q2 2026 revenue: $11.5B.
  - The prospectus gives no H1 2026 compute total. It lists $518B in commitments: Google ≥ $111B, Amazon $110B, Microsoft $31B, Broadcom-related $161B, and SpaceX up to $84.5B (the only cancellable item).
  - The SpaceX fee reaches the full $1.25B/month from July. Q2 was billed at a reduced fee.
  - Build-up: Q1 3.4 + Q2 6.4 + H2 ≈ 2 × (6.4 + ≈2.3 SpaceX increment) ≈ **$27–28B for calendar 2026**. The August run-rate is about $35B/yr.
  - **Use 28.** It is on the same calendar-2026 basis as OpenAI's $50B. Note 26–35 as the range.
- **GDM / Meta / xAI.**
  - Their disclosures are still capex only:
    - Meta's 2026 capex guidance is **$130–145B**, narrowed on the Q2 call of 29 Jul 2026.
    - SpaceX's AI segment capex was **$7.7B in Q1 2026** and $12.7B in 2025 (S-1).
  - xAI's capex is offset by about $26B/yr of rental income: Anthropic $15B and Google $11B.
  - None of this is a compute-spend plan that beats scaling from OpenAI by compute. Keep the rule.
  - With OpenAI at 310: **GDM 42, Meta 26, xAI 15**. If OpenAI stays at 288, use 45 / 28 / 16.5.

## 6. Influence

- **xAI lobbying, on a parent basis.** OpenSecrets and the LDA site and API return 403 here, as before.
  - SpaceX reported **$750k in Q1 2026** in-house ([Legis1](https://legis1.com/news/nasa-budget-hearing-committee-decides-nasas-lunar); secondary, so verify it on LDA).
  - 2025 outside-firm filings visible in LDA search snippets: American Defense International $90k/quarter, Harbinger $50k/quarter, J.A. Green $50k/quarter, and Squire Patton Boggs $80k/quarter until 30 Jun.
  - Use about **$3.0M/yr**, which scores 0.61 on the log scale (the proxy scored 0.55).
- **Grok MAU.** The SpaceX S-1 reports "approximately 117 million MAUs that used Grok's AI features as of March 31, 2026", primary and dated. It replaces Sensor Tower's 50M and scores 0.106.
  - **xAI index 19.4, giving a seed of 42.**
- **Menlo.** No 2026 enterprise or mid-year report exists; Dec 2025 remains the latest. Ramp AI Index Aug 2026 is the freshest cross-check.
- **Meta.** Meta launched a public Muse Spark API in Q2 2026 (per the Q2 call), so the July 2025 Llama figure of 9% is stale. No replacement exists; keep it and caveat it.
- **Meta AI MAU.** There is no official 2026 figure. The Q2 call says only "+60% in people interacting with the assistant each day". Keep Sensor Tower and the caveat.
- **Coding shares for Google, Meta and xAI.** No 2026 enterprise data exists.
  - OpenRouter is the only per-model usage source. Its shares are dominated by Chinese open models and exclude first-party tools, so it is not usable.
  - The xAI = 0 coding share is attackable: Grok Code Fast was heavily used on OpenRouter in late 2025. Note this as a limitation.

## 7. US and China stock

- **Re-derived independently from Epoch's partial-quarter rows:**
  - Nvidia cumulative to 26 Jul 2026 (its fiscal quarter end) is 2,216.
  - TPU cumulative to 2 Aug (Broadcom's quarter end) is 698.
  - AMD is 174 + 9 and Trainium 147 + 7.
  - Total **≈3,251**, × 0.75 = **2,438 → 2,450 ✓**.
- **Nvidia Q2 FY27.** Data-centre revenue was $89.0B (+18% QoQ, +117% YoY), and Q3 total revenue is guided at $108B ([Storage Newsletter](https://www.storagenewsletter.com/2026/08/27/nvidia-fiscal-2q27-financial-results/)).
  - The implied $/H100e is consistent with Epoch's $13.2k chip cost plus systems and networking.
  - The sales rate is still rising, so +150/turn will be low by the later turns. Keep 150 for simplicity; +160 is defensible as an Aug–Mar average.
- **China.** Epoch's Huawei H1 2026 update gives 70.8 → 111.5 units (6.8/month) ✓. Keep 260 / +11; confidence low.

## 8. Juror

- **Kimi K3 is open weights.**
  - HF repo `moonshotai/Kimi-K3`, initial commit **27 Jul 2026** (before t=0), ungated, license "kimi-k3" (custom).
  - Later commits changed `encoding_k3.py` (20 Aug) and added eval results (2 Sep). The current sha is `f831ab66814297da540d832a5235f8e904f29d06`.
  - The weights are about 1.56 TB, so self-hosting is impractical.
- **API.** Model id `kimi-k3`, context **1,048,576**, **$3.00 / $15.00 per Mtok**, cache hit $0.30, cache write $3 (5-minute) / $6 (1-hour) ([platform.kimi.ai pricing](https://platform.kimi.ai/docs/pricing/chat), fetched today). **There is no dated snapshot id.**
- **Alternatives:**
  - DeepSeek V4 Pro 0813: ECI 155.31, cheaper, but DeepSeek re-points its aliases.
  - Qwen3.8-2.4T-A95B: open, on HF since 8–12 Aug, sha `207bd685…`, custom license, text-only. Epoch scores only the closed Max (156.41), so the open checkpoint is ≤ that.
  - **Kimi stays first.**
- **Reproducibility is acceptable only with mitigations:**
  - pin `kimi-k3`, the temperature and the seed;
  - log the response `model` field and the date;
  - record the HF revision sha as the reference weights;
  - name a fallback that can serve pinned weights (an HF inference provider at that revision);
  - state that the hosted alias may be updated.

## Recommended changes

1. Set Anthropic's Colossus 2 slice to **22 units**, giving Anthropic **246** and xAI **95**.
2. Raise OpenAI to **310**, from Epoch's site additions. Keep GDM at 261 and Meta at 164. Keep 1.65× as the base and drop the 2.04× option.
3. Set xAI talent to **17.1**, giving **24.6 / 21.9 / 17.1 / 19.3 / 17.1**.
4. Set Anthropic capital to **28**, on a calendar-2026 basis. Rescale GDM, Meta and xAI capital to **42 / 26 / 15**.
5. Set xAI influence to **42**, using SpaceX parent lobbying (≈$3M) and the S-1's 117M Grok MAU.
6. Add to the spec: the "publicly available model" rule (Mythos Preview and Deep Think are excluded), the ECI snapshot date, and the Kimi K3 HF revision sha.
7. Keep: the ECI seeds, the 1.2 pace, US 2,450 / +150, China 260 / +11, the 0.25 price, and the Kimi K3 juror.
