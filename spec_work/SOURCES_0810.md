# SOURCES_0810: seed values for a 1 August 2026 start (research for C4–C6, C7 juror)

Research done 9 Oct 2026. Every value describes the world on 1 Aug 2026 (holdings at about 31 Jul 2026, models released by 31 Jul 2026). Epoch datasets were downloaded the same day from epoch.ai/data: ECI from `benchmark_data.zip` (README dated Oct 2026), chip users from `ai_chip_users.zip` (generated 9 Sep 2026), chip sales from `ai_chip_sales.zip` (generated 27 Aug–1 Oct 2026), chip owners from `ai_chip_owners.zip` (May 2026), and frontier data centres from `data_centers/*.csv`.

---

## Proposed §4 table

| Lab | Talent (%) | Compute (10k H100e) | Capital ($B) | Influence | Capability (ECI) | K = C − 3.6·ln(compute) |
|---|---|---|---|---|---|---|
| Anthropic | 23.7 | 224 | 26 | 78 | 162.8 (Claude Opus 5) | 143.3 |
| OpenAI | 21.1 | 288 | 50 | 68 | 162.1 (GPT-5.5 Pro) | 141.7 |
| Google DeepMind | 16.6 | 261 | 45 | 66 | 154.8 (Gemini 3.1 Pro) | 134.7 |
| Meta | 18.6 | 164 | 28 | 55 | 154.2 (Muse Spark 1.1) | 135.9 |
| xAI | 20.0 | 121 ‡ | 21 ‡ | 40 † | 153.9 (Grok 4.5) | 136.7 ‡ |

‡ xAI's compute is an upper bound. Anthropic's lease of Colossus 2 GB200 capacity is not subtracted because its size is unknown (Escalate #1). Capital and K follow from compute.
† xAI's lobbying input is a proxy (Escalate #4). With no lobbying at all, the seed would be 30.

## Proposed constants (approach (a): C = ECI, absolute-C constants × 0.73)

| Constant | Old (C = 10·ln(METR min) + 20) | New (C = ECI) | Why |
|---|---|---|---|
| Capability scale | METR horizon, seeds 45–79 | Epoch Capabilities Index, seeds 153.9–162.8 | Zoe/meeting: one index with data for every lab |
| Frontier pace | 1.65 C/turn | **1.2 C/turn** (Epoch: 14 ECI/yr = 1.17/month; own fit 1.21) | Epoch's measured frontier ECI trend |
| Scale factor k | — | 1.2 / 1.65 = **0.727** | Every absolute-C constant is multiplied by k |
| a in C = a·ln(compute) + K | 5 (checks 3–8) | **3.6** (checks 2–6) | 5 × 0.727 |
| ΔK per turn | 1.2·(talent ÷ 20%) | **0.85·(talent ÷ 20%)** | Gives the 1.2/turn pace (shown below) |
| Know-how shock SD | ≈1 C/turn | **0.7 C/turn** | × k |
| Intrusion gain | 0.2 × gap, min 0.5 C | 0.2 × gap, **min 0.35 C** | The share is scale-free; the minimum is × k |
| T1 ladder rung (minimum gain) | 0.5 → 1.5 C | **0.35 → 1.1 C** | × k |
| Exposure q | min(0.45, (0.10 + 0.03 × gain) ÷ committers) | min(0.45, (0.10 + **0.04** × gain) ÷ committers) | 0.03 ÷ k = 0.041, so the same real copy gives the same odds |
| Ratio formulas (income √(C·Inf / C₀·Inf₀); UPS) | raw C | use **C̃ = C − 100** (capability above ECI 100 ≈ LLaMA-13B / Llama 2-7B, 2023) | Raw ECI ≈ 160 makes ratios inert (see below) |
| UPS world-capability term | mean C ÷ 100 | **mean C̃ ÷ 100** (≈ 0.58 at t=0) | Same role and similar level to the old ≈ 0.69 |
| UPS concentration / equity | HHI of C shares; min C ÷ mean C | HHI of C̃ shares; min C̃ ÷ mean C̃ | Same offset |
| US compute stock | 1,500 units, +110/turn | **2,450 units, +150/turn** | Derivation in §4 |
| China compute stock | ≈200, +10/turn | **≈260, +11/turn** | Derivation in §4 |
| Purchase price | 0.25 Capital/unit × SCR factor | **0.25, unchanged** | Epoch chip cost per H100e fell only about 3% (§4) |
| accelerate_infrastructure | +15 units/turn | +15 (or **+20** to keep 14% of base growth; design call) | Optional |
| Per-turn acquisition cap | 20 units | 20 (see Escalate #7) | It is now 7% of OpenAI's fleet, against 11% before |

Not changed: the 0.2 gap share, success p, lead 0.15, Prosperity weights 0.8/0.2 (but see Escalate #8), and Influence costs.

---

## 1. Capability: Epoch Capabilities Index (ECI)

Source: Epoch AI, *Capabilities & benchmarking* hub, `epoch_capabilities_index/eci_scores.csv` (downloaded 9 Oct 2026). The ECI is a single scale stitched from about 37 benchmarks. Anchors: Claude 3.5 Sonnet = 130, GPT-5 = 150. Brackets are Epoch's 90% bootstrap CI.

| Lab | Best model released by 31 Jul 2026 | Release | ECI [CI] | Runner-up by 31 Jul (ECI) |
|---|---|---|---|---|
| Anthropic | Claude Opus 5 | 24 Jul 2026 | 162.78 [160.12, 166.47] | Claude Fable 5, 9 Jun (162.06) |
| OpenAI | GPT-5.5 Pro | 23 Apr 2026 | 162.07 [158.98, 165.82] | GPT-5.6 Sol, 9 Jul (161.66) |
| Google DeepMind | Gemini 3.1 Pro | 19 Feb 2026 | 154.77 [152.44, 157.27] | Gemini 3.5 Flash, 19 May (154.46) |
| Meta | Muse Spark 1.1 | 9 Jul 2026 | 154.21 [151.96, 156.81] | Muse Spark, 8 Apr (152.04) |
| xAI | Grok 4.5 | 8 Jul 2026 | 153.92 [152.21, 155.63] | Grok 4.20, 17 Feb (151.98) |

- **Meta:** the notes' guess is confirmed. Meta's best model is from the Muse Spark family, and the best version by 31 Jul is **Muse Spark 1.1**, not the April original. Muse Spark 1.2 (5 Aug, 154.87) and 1.3 (2 Sep, 156.75) came after the start date. No Llama model comes close.
- **Google DeepMind:** Gemini 3.5 Pro was announced at I/O (19 May) but had not shipped by mid-August: it was delayed and in partner testing ([jetstream 22 Jul 2026](https://jetstream.blog/2026/07/22/gemini-3-5-pro-partner-testing-phase/)). Gemini 3.1 Pro is therefore the right seed.
- **Ties:** the top two (Anthropic and OpenAI, 0.7 apart) are tied within CI. So are the bottom three (153.9–154.8). The clear gap is about 8 ECI between those two groups.
- **Post-start releases** for the hindcast check on turns 1–2: GPT-6 Astra (3 Sep, 166.45), Claude Opus 5.5 (22 Sep, 167.33), Gemini 3.7 Flash (13 Aug, 157.27), Grok 4.6 (12 Aug, 156.44), Muse Spark 1.2 and 1.3.

**Frontier pace.** Epoch Trends dashboard: "The ECI frontier has advanced by 14 points per year since the introduction of reasoning models" (90% CI 12–17; dashboard dated 5 Feb 2026), i.e. **1.17 ECI/month**. Epoch's Dec 2025 post gives about 15 points/yr after April 2024, against 8 before ([Epoch, 23 Dec 2025](https://epochai.substack.com/p/frontier-ai-capabilities-accelerated)). As a check, I fitted a line to the running-maximum ECI in the downloaded data. Sep 2024–Jul 2026 gives 1.21/month; Jul 2025–Jul 2026 gives 1.26/month; Sep 2024–Sep 2026 gives 1.23/month. **Use 1.2 ECI/turn.**

Confidence: **high** for the scores (one source, one method, all five labs). **Medium** for the ranking within the two tied groups. ECI values get revised as Epoch adds benchmarks, so freeze the snapshot date in the spec.

## 2. C-scale plan: recommendation (a), use ECI directly

**Recommendation: (a) C = ECI. Multiply every absolute-C constant by k = 0.727, and use C̃ = C − 100 wherever C enters a ratio.**

Why (a) and not (b):
- **(b) adds a second invented transform.** It would map ECI linearly onto the old scale, e.g. C = 1.4·(ECI − 110). The paper would then need to justify that map's offset and slope, and briefs would show numbers that match no published index.
- **(a) is simpler to explain.** Seeds are Epoch's published numbers, and the pace is Epoch's published trend. The change itself is one multiplier.
- **The slope is fixed either way.** With (b), the slope (1.65 ÷ 1.2 = 1.375) is forced by the pace anyway. The only free choice is the offset, and (a) needs an offset too, in the C − 100 rule for ratios. So (b) saves nothing.

**Pace check.** Old: 1.65 C/turn = ΔK 1.2 + compute term ≈ 0.45 (= a · Δln compute = 5 × 0.09, i.e. holdings growing about 9%/month).
New: frontier ΔC = 3.6 × 0.09 (≈ 0.33) + 0.85 × (20% ÷ 20%) = **1.18 ECI/turn ≈ Epoch's 1.17**.
- In play, holdings grow about 5–9% per turn: US stock +150 on 2,450 is 6%, and a 20-unit buy is 7% of OpenAI's fleet. That puts the compute term at about 0.2–0.33, so the pace lands at 1.05–1.2. If the scripted checks show it running low, raise the ΔK coefficient to 0.9.
- With these talent seeds, Anthropic (23.7%) gets ΔK ≈ 1.01/turn and Google DeepMind (16.6%) gets 0.71.

**Why ratios need C − 100.**
- With raw ECI, the old equity term min C ÷ mean C becomes 0.97. The capability-share HHI is about 0.200, which is the floor for five labs, so both terms barely move.
- In the income formula, √(C/C₀) moves only about 3% over a run, against about 9% before.
- Using C̃ = C − 100 restores sensitivity close to the old scale. Over 8 turns ΔC ≈ 9.6 on C̃₀ ≈ 62 is about 15%, against 13/69 = 19% before.
- ECI 100 is where 2023 small open models sit (LLaMA-13B 100.6, Llama 2-7B 99.1), a defensible "baseline LLM".
- Differences need no offset: Prosperity ΔC, the intrusion gap, and the gain.

**Effect of the 0.727 factor on the scale.** ECI gaps are small: the largest seed gap is 8.9 ECI. The largest intrusion gain at t=0 is therefore 0.2 × 8.9 = 1.8 ECI. That is 2.4 old-C, against 6.8 old-C before, because Meta is no longer far behind. Trailing labs gain less in real terms than in the old seeds. This is the real state of the field, but it matters for T1 (Escalate #9).

Seeds: see the §4 table (K column). K = C − 3.6·ln(compute units).

## 3. Compute (10k H100e) per lab, about 31 Jul 2026

Base: Epoch AI **Chip Users Explorer**, end-2025 medians in units, with 90% CI. It launched 9 Sep 2026, and the CSV was generated 9 Sep 2026. It is the only source covering all five labs as compute *used* (owned plus rented). Epoch publishes year-end values only, so I projected to 31 Jul 2026 as follows:

- **Growth factor 1.65.** The non-Chinese AI-chip stock grew 1.65× from 31 Dec 2025 to 31 Jul 2026, by the same Epoch chip-sales data used for the US stock (1,964 → 3,246 units cumulative). Using the same factor keeps lab holdings in the same proportion to the US stock as the Jan 2026 seeds, so the 50% national cap behaves as designed: labs hold 43% of US stock, against 41% before.
- **One discrete adjustment, the Colossus 1 lease.** The Colossus 1 cluster (27.6 units per Epoch's data-centre timeline) has been used by Anthropic since about 1 Jun 2026 ([x.ai 6 May 2026](https://x.ai/news/anthropic-compute-partnership); Epoch timeline note: "We assume that Anthropic has started using Colossus 1 now"). It is moved from xAI to Anthropic.

| Lab | Epoch end-2025 median [90% CI] | × 1.65 | Adjustment | **31 Jul 2026** | Confidence |
|---|---|---|---|---|---|
| OpenAI | 174.3 [125.1, 218.5] | 288 | — | **288** | medium-high (power-disclosure based) |
| Anthropic | 119.0 [84.2, 171.7] | 196 | + Colossus 1 (27.6) | **224** (+ unknown Colossus 2 slice) | medium |
| Google DeepMind | 158.3 [100.8, 254.9] | 261 | — | **261** | low (allocation share) |
| Meta (MSL) | 99.6 [60.6, 163.8] | 164 | — | **164** | low (allocation share) |
| xAI (SpaceXAI) | 63.5 [58.7, 78.7] | — | Colossus 2 (111.2, Epoch timeline from 15 Jun 2026) + other sites 6.1 × 1.65 (≈ 10); Colossus 1 to Anthropic | **≤ 121** | medium-low |

**The shares Zoe asked about (#4).** These are Epoch's model inputs, medians with 90% CI, from `intermediates_by_lab.csv`.

- **Google DeepMind: 46.2% of Google's operational AI fleet** [32.1%, 67.1%].
  - Google's owned fleet (TPU plus Nvidia) at end-2025 was 498 units. Of that, **68.9% is operational** [55%, 88%], i.e. 342 units.
  - Google ML compute splits about 50/50 between Cloud and internal use (cloud share 49.9% [45%, 55%]). DeepMind gets 34.9% of the cloud half [20%, 61%] and 56.7% of the internal half [40%, 81%].
  - Net: 46.2% of the operational fleet, which is **≈ 32% of Google's owned fleet**.
- **Meta Superintelligence Labs: 52.0% of Meta's operational fleet** [33.4%, 81.2%] ("MSL share vs core-business recommenders").
  - Meta's owned fleet (Nvidia plus AMD) at end-2025 was 231 units. Of that, **74.4% is operational** [62%, 91%], i.e. 172 units.
  - MSL also rents about 9 units of cloud ($1.06B/yr at $1.34/H100e-hr).
  - Net: **≈ 39% of Meta's owned fleet**, plus rentals.
- Suggested spec wording: "Google DeepMind is assumed to use 46% (90% CI 32–67%) of Google's operational AI fleet, and Meta Superintelligence Labs 52% (33–81%) of Meta's, following Epoch's Chip Users model."

**Cross-checks**, from Epoch Frontier Data Centers timelines, Dec 2025 → Jul 2026:
- OpenAI-attributed sites grew 2.10×.
- Anthropic sites excluding Colossus grew 1.65×.
- Google-owned frontier sites grew 1.58×.
- Meta-owned frontier sites grew 3.9×, from a small base (Prometheus, Hyperion-class sites coming online).
- So the 1.65 factor is about right for Anthropic and Google DeepMind, may understate OpenAI by up to about 25%, and may understate Meta materially (Escalate #2).
- Using the alternative, Epoch's 3.4×/yr long-run stock trend (3.4^(7/12) = 2.04×), every value is about 24% higher (OpenAI 356, GDM 323, Meta 203). Labs would then hold 53% of US stock, so the national cap would bind at t=0.

## 4. US and China compute stock and growth per turn (answers Zoe #0)

**Method, the same as the old spec but re-dated.** US stock = US-company share × Epoch cumulative AI-chip sales of non-Chinese designs (Nvidia, Google TPU, AMD, Amazon Trainium, counted since 2022). US growth = the same share × the latest quarterly sales rate ÷ 3.

1. **US-company share = 75%.**
   - Source: Epoch AI Chip Owners, end-2025 medians. Owners are Google 505, Microsoft 342, Amazon 245, Meta 230, Oracle 114, CoreWeave 83 and xAI 55 units, totalling **1,575 of 2,093 units = 75.2%**. China (legal plus smuggled) is 182 units (8.7%); Other is 336 (16%).
   - Caveat: this counts US companies' chips worldwide, not chips on US soil.
2. **Re-check of the old numbers.** End-2025 cumulative non-Chinese sales were Nvidia 1,366 + TPU 371 + AMD 125 + Trainium 102 = 1,964. × 0.75 = **1,473 ≈ 1,500** ✓. The Q4 2025 increment was 428 units/quarter = 143/month; × 0.75 = **107 ≈ 110** ✓. So the old figures were exactly this method.
3. **1 Aug 2026.**
   - End-Jun 2026 cumulative: Nvidia 2,099 + TPU 626 + AMD 174 + Trainium ≈ 147 = 3,045. Trainium is extrapolated at its late-2025 rate of +22/quarter because Epoch has no 2026 Trainium rows yet.
   - Q2 2026 sales were 604 units/quarter = **201/month**.
   - Adding July gives 3,246 at end-July, × 0.75 = **2,435 → 2,450 units**.
   - Growth: 0.75 × 201 = **151 → +150 units/turn**.
4. **China.**
   - End-2025: 182 units (legal Nvidia 40.2, AMD 1.1, Huawei 70.8, Cambricon 3.5, smuggled Nvidia 66.2 [29, 159]).
   - Jan–Jul 2026 additions: Huawei +47.5, from Epoch's H1 2026 rate of 6.8/month (Huawei cumulative 111.5 at end-Jun). Cambricon +2.9. Smuggled +29, at its 2025 rate of 4.2/month. Legal Nvidia +0, flat in Epoch's Q1 2026 data.
   - Result: **≈ 260 units at t=0**. Growth is 6.8 + 0.4 + 4.2 = **+11/turn**. Confidence is low; the smuggled CI is very wide.
5. **Purchase price.**
   - Epoch's chip-cost data (`timelines_by_chip.csv`) gives the sales-weighted chip cost per H100e: **$13.6k in Q4 2025 and $13.2k in Q2 2026** (−3%).
   - The old 0.25 Capital/unit ($25k per H100e all-in, 1.84× chip cost for servers, networking and facilities) therefore stays. Keep **0.25**.

Spec wording for Zoe #0: "US stock is the US-company share (75%, Epoch AI Chip Owners, end-2025) of Epoch's cumulative AI-chip sales to 31 Jul 2026 (3,246 units). Growth is that share of the Q2 2026 sales rate (201 units/month)."

Confidence: **medium** for the US (Epoch medians; the Trainium 2026 extrapolation is under 2% of the total). **Low** for China.

## 5. Talent

**levels.fyi has no usable acceptance rate.**
- The company pages show compensation only (median TC, per-level pay, vesting) and negotiation marketing. There is no offer acceptance, decline or competing-offer win statistic (checked [levels.fyi/companies/anthropic/salaries](https://www.levels.fyi/companies/anthropic/salaries), updated 9 Oct 2026).
- No third-party analysis of levels.fyi offer outcomes per lab was found.
- **SignalFire's 2026 report (22 Jun 2026)** no longer gives lab-level retention or flows. Labs are pooled (OpenAI, Anthropic, GDM, xAI, Mistral, Cohere), and Meta AI is excluded.

**Proposed fallback: Zeki "arrival share", the closest measurable analogue of offer acceptance.**
- Definition: of all research and advanced-engineering moves into or out of a lab, the share that are arrivals, h ÷ (h + 1), where h is Zeki's hires-to-exits ratio. Like an acceptance rate, it measures how often the lab wins a contested move. It is not raw hiring.
- Source: Zeki Data, reported by [Fortune, 27 Aug 2026](https://fortune.com/2026/08/27/google-deepmind-losing-talent-to-rival-ai-labs-startups-new-data-show/). The sample is 20,900 R&E staff at 10 companies; managers, executives, interns and non-technical staff are excluded.
- Each lab's share = its arrival share ÷ the sum over the five labs × 100.

| Lab | Zeki hires : exits | Arrival share | **Talent share** |
|---|---|---|---|
| Anthropic | 22 : 1 (2025) | 0.957 | **23.7** |
| OpenAI | 5.7 : 1 (2025) | 0.851 | **21.1** |
| Google DeepMind | 2 : 1 (Q3 2026) | 0.667 | **16.6** |
| Meta | 3 : 1 (2025) | 0.750 | **18.6** |
| xAI | not in Zeki's sample | 0.805 (proxy) | **20.0** (proxy) |

- **xAI proxy:** OpenAI's arrival share × (xAI ÷ OpenAI Metix visible retention, 69.6% ÷ 73.6%). Source: [Metix, 11 Jun 2026](https://metix.ai/reports/mapping/frontier-ai-labs-talent-2026).
  - Alternative: set xAI equal to the lowest lab (GDM), which gives A 24.6 / O 21.9 / G 17.1 / M 19.3 / X 17.1. That is defensible given the 2026 exodus: all 11 non-Musk co-founders have left ([Fast Company](https://www.fastcompany.com/91531084/inside-the-xai-exodus)).
- **OpenAI now ranks above DeepMind,** which resolves Zoe #2/#3. The ordering agrees with SignalFire's 2025 flows (OpenAI→Anthropic 8:1, DeepMind→Anthropic 11:1). DeepMind's exits went 25% to Anthropic, 21% to Meta and 14% to OpenAI (Zeki).
- **Paper-author rate ("author paper rate" in the notes): rejected.**
  - No per-lab ICML/ICLR 2026 affiliation counts are published. Google reports "130+ ICML 2026 papers" for Google Research and DeepMind combined.
  - It would also reward publishing labs (GDM, Meta) and penalise closed ones (OpenAI, Anthropic, xAI), the bias Zoe flagged.

Confidence: **medium-low**. The periods are mixed (2025 against Q3 2026), xAI is a proxy, and Meta's figure is company-wide.

## 6. Capital and Influence

**Capital** (annual AI compute-spending capacity, $B, at Aug 2026). The method is unchanged: own disclosed compute spend where available; otherwise scaled from OpenAI by compute (50 × units ÷ 288).

| Lab | Value | Basis | Confidence |
|---|---|---|---|
| OpenAI | **50** | Brockman testimony, Musk v. OpenAI, 5 May 2026: about $50B on compute in 2026 ([Bloomberg Law](https://news.bloomberglaw.com/ip-law/openai-to-spend-50-billion-on-computing-in-2026-brockman-says)) | medium |
| Anthropic | **26** | Q2 2026 run-rate × 4. Compute cost per revenue dollar was $0.56 (PitchBook estimate) × Q2 revenue of $11.5B (Bloomberg, 14 Aug) = $6.4B/quarter. Q1 was 0.71 × 4.8 = $3.4B. 2025 actual: $7.33B (leaked S-1, [SiliconANGLE 29 Sep 2026](https://siliconangle.com/2026/09/29/leaked-anthropic-ipo-filing-reveals-8b-operating-loss-rapid-revenue-growth/)). The SpaceX lease adds $1.25B/month from mid-2026. The H1-annualised alternative is 20 (the old value). | low-medium |
| Google DeepMind | **45** | 50 × 261/288 | low |
| Meta | **28** | 50 × 164/288 | low |
| xAI | **21** | 50 × 121/288 (upper bound, follows compute) | low |

**Influence** = 30 + 0.6 × index (index 0–100). Each component is taken relative to the leading lab on that component, then weighted.
- **Lobbying log scale:** log10(spend ÷ $0.1M) ÷ log10(max ÷ $0.1M). This floor rule reproduces the old Anthropic value (≈ 0.6). Put it in the spec explicitly.

| Lab | API (40%) Menlo Dec 2025 | Lobbying 2025 (30%) | Coding (20%) Menlo Dec 2025 | Consumer MAU (10%) Sensor Tower May 2026 | Index | **Seed** |
|---|---|---|---|---|---|---|
| Anthropic | 40% → 1.00 | $3.13M → 0.62 | 54% → 1.00 | 245M → 0.22 | 80.8 | **78** |
| OpenAI | 27% → 0.68 | $2.99M → 0.61 | 21% → 0.39 | 1,100M → 1.00 | 63.1 | **68** |
| Google DeepMind | 21% → 0.53 | Alphabet $13.10M → 0.88 | ≈17.5%* → 0.32 | 662M → 0.60 | 59.7 | **66** |
| Meta | 9%* → 0.23 | $26.29M → 1.00 | ≈7.5%* → 0.14 | 61M* → 0.06 | 42.3 | **55** |
| xAI | not listed → 0 | ≈$2.1M† → 0.55 | not listed → 0 | 50M* → 0.05 | 16.9 | **40** |

\* Not reported directly:
- Meta API: Menlo's Jul 2025 Llama figure (9%). Dec 2025 only says "the remaining 12% spread across Meta's Llama, Cohere, Mistral…".
- Coding for Google and Meta: Menlo gives only Anthropic 54 / OpenAI 21. The residual 25% is split by API share (21:9); xAI is set to 0.
- Meta and Grok users: Sensor Tower app-only MAU (61M, 50M). TechCrunch puts Meta AI, Grok, Perplexity and DeepSeek together at under 5%. Meta claims 1B+ MAU including embedded use, which would raise Meta's seed by about 5.

† X Corp (owned by xAI, now SpaceXAI) spent $0.53M in Q3 2025 (Issue One), annualised. No xAI filing was found.

- **Mid-2026 checks.** No Menlo enterprise update exists for 2026; the Dec 2025 report is the latest. The Ramp AI Index for Aug 2026 shows 43.8% of US businesses paying Anthropic against 39.8% paying OpenAI, which supports Anthropic's lead. H1 2026 lobbying keeps the same order: Meta ≈ $6M in Q2 (≈ $7.1M in Q1), Alphabet $5.3M in Q2, Anthropic $1.56M + $1.97M, OpenAI ≈ $1.0–1.5M + $1.2M ([Issue One, 21 Jul 2026](https://issueone.org/articles/lobbying-disclosures-reveal-big-tech-spends-more-than-226000-per-day-to-buy-influence/)).
- **Caveat:** Menlo Ventures is an Anthropic investor.
- **Change from the old seeds:** OpenAI +1 and Google DeepMind +1, both from consumer users. Anthropic +1. Meta −5, because Sensor Tower counts are used instead of claims. xAI +3.

Confidence: **low**; this is still the weakest column.

## 7. Juror pick: **Kimi K3 (`kimi-k3`, Moonshot API)**

| Candidate (open weights, latest) | ECI [CI] | Context | 1st-party $/Mtok in/out | Notes |
|---|---|---|---|---|
| **Kimi K3** (Moonshot, 16 Jul 2026) | **157.45** [155.15, 160.03] | **1,048,576** | **$3.00 / $15.00** (cache hit $0.30) | Highest-ECI open-weight model in Epoch's data |
| DeepSeek V4 Pro 0813 (`deepseek-v4-pro`) | 155.31 [153.48, 157.45] | 1M (output ≤ 384K) | $1.32 / $3.96 peak; $0.66 / $1.98 off-peak | 4× cheaper. DeepSeek re-points model names (legacy flash IDs now route to V4.1 Flash), a reproducibility risk |
| DeepSeek V4.1 Flash (`deepseek-flash`) | 154.90 [148.49, 157.33] | 1M | $0.30 / $1.20 peak | Wide CI |
| Qwen 3.8 Max | 156.41 | 262K native (1M with YaRN) | ≈ $2 / $6 (third-party report) | Epoch classes it **closed weights**. The open 2.4T checkpoint's status and pricing are unverified; excluded |
| Qwen 3.8 27B (open) | 149.38 | 262K | not verified | Clearly weaker |

**Recommendation: Kimi K3, model id `kimi-k3`** (endpoint `https://api.moonshot.ai/v1`, OpenAI-compatible; pricing page now at platform.kimi.ai/docs/pricing/chat).
- It has the highest capability of the open models: about 2 ECI above DeepSeek V4 Pro, with overlapping CIs. Its level is close to the seeded labs (153.9–154.8).
- Its 1M context fits whole run logs.
- Cost is trivial at this scale: an assumed ~0.4M-token log plus 10K output costs about $1.35 per rating, against about $0.57 for DeepSeek (peak).
- **Fallback:** `deepseek-v4-pro` if Moonshot access or rate limits fail.
- Pin the exact version string and the temperature in the spec, and log the response model id.
- Neither model shares a family with any seat, which fixes Zoe #7's imbalance.

---

## Sources by category

| Category | Source (URL) | Date | What it measures |
|---|---|---|---|
| Capability | Epoch AI Benchmarking hub, `benchmark_data.zip` → `epoch_capabilities_index/eci_scores.csv` (https://epoch.ai/benchmarks; https://epoch.ai/data/benchmark_data.zip) | downloaded 9 Oct 2026 | ECI per model, 90% CI, release date |
| Pace | Epoch Trends (https://epoch.ai/trends) | 5 Feb 2026 | Frontier ECI +14/yr since reasoning models (CI 12–17) |
| Pace | Epoch, "Frontier AI capabilities accelerated" (https://epochai.substack.com/p/frontier-ai-capabilities-accelerated) | 23 Dec 2025 | ~8 → ~15 ECI/yr after Apr 2024 |
| Gemini 3.5 Pro status | https://jetstream.blog/2026/07/22/gemini-3-5-pro-partner-testing-phase/ | 22 Jul 2026 | Not released by start date |
| Compute | Epoch AI Chip Users, `ai_chip_users.zip` (https://epoch.ai/data/ai-chip-users; https://epoch.ai/latest/introducing-the-ai-chip-users-explorer) | generated 9 Sep 2026 | End-2025 H100e used per lab; GDM/MSL share inputs |
| Compute | Epoch Frontier Data Centers, `data_centers.csv`, `data_center_timelines.csv` (https://epoch.ai/data/data-centers) | downloaded 9 Oct 2026 | Per-site H100e over time; Colossus 1/2, users |
| Compute | SpaceXAI–Anthropic Colossus deal (https://x.ai/news/anthropic-compute-partnership) | 6 May 2026 (+ 20 May expansion to Colossus 2 GB200) | Lease of Colossus 1 to Anthropic |
| Stock / growth / price | Epoch AI Chip Sales, `ai_chip_sales.zip` (https://epoch.ai/data/ai-chip-sales) | generated 27 Aug–1 Oct 2026 | Cumulative and quarterly H100e sold by designer; chip cost |
| US share, China | Epoch AI Chip Owners, `ai_chip_owners.zip` (https://epoch.ai/data/ai-chip-owners) | May 2026 (data to Q4 2025, partial Q1 2026) | Holdings by owner incl. China legal and smuggled |
| Talent | Fortune on Zeki Data (https://fortune.com/2026/08/27/google-deepmind-losing-talent-to-rival-ai-labs-startups-new-data-show/) | 27 Aug 2026 | Hires-to-exits ratios; DeepMind exit destinations |
| Talent | Metix, Frontier AI Lab Talent Landscape (https://metix.ai/reports/mapping/frontier-ai-labs-talent-2026) | 11 Jun 2026 | Visible retention incl. xAI; technical pool sizes |
| Talent (negative) | levels.fyi company page (https://www.levels.fyi/companies/anthropic/salaries); SignalFire State of Talent 2026 (https://www.signalfire.com/blog/signalfire-state-of-talent-report-2026) | 9 Oct 2026; 22 Jun 2026 | No acceptance rate; no lab-level data |
| Capital | Bloomberg Law, Brockman testimony (https://news.bloomberglaw.com/ip-law/openai-to-spend-50-billion-on-computing-in-2026-brockman-says) | 5 May 2026 | OpenAI 2026 compute ≈ $50B |
| Capital | SiliconANGLE on leaked Anthropic S-1 (https://siliconangle.com/2026/09/29/leaked-anthropic-ipo-filing-reveals-8b-operating-loss-rapid-revenue-growth/); PitchBook compute-per-revenue estimates via secondary reports | 29 Sep 2026 | Anthropic 2025 compute $7.33B; Q2 2026 revenue $11.5B |
| Influence | Menlo Ventures, 2025 State of GenAI in the Enterprise (https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) | 9 Dec 2025 | API spend share 40/27/21; coding 54/21 |
| Influence | TechCrunch on Sensor Tower (https://techcrunch.com/2026/06/16/chatgpts-market-share-slips-below-50-for-first-time/) | 16 Jun 2026 (May data) | Assistant MAU: ChatGPT 1.1B, Gemini 662M, Claude 245M |
| Influence | Issue One, Q2 2026 lobbying (https://issueone.org/articles/lobbying-disclosures-reveal-big-tech-spends-more-than-226000-per-day-to-buy-influence/); everything-pr / Forbes 2025 totals (https://everything-pr.com/who-is-lobbying-on-ai-regulation-and-what-they-actually-want; https://www.forbes.com/sites/phoebeliu/2026/02/20/ais-biggest-builders-openai-anthropic-among-biggest-government-lobbyists/) | 21 Jul 2026; Feb 2026 | Federal lobbying: Meta $26.29M, Alphabet $13.10M, Anthropic $3.13M, OpenAI $2.99M (2025) |
| Juror | Moonshot pricing (https://platform.kimi.ai/docs/pricing/chat); DeepSeek pricing (https://api-docs.deepseek.com/quick_start/pricing) | read 9 Oct 2026 | Context, $/Mtok |

OpenSecrets (opensecrets.org) and the Senate LDA API (lda.gov) could not be fetched from this environment (blocked or 403). The lobbying figures above are from secondary reports of LDA filings.

---

## Escalate to David

1. **Anthropic's Colossus 2 slice.** Anthropic was scaling GB200 capacity in Colossus 2 "throughout June" (Tom Brown, 20 May). Epoch lists Anthropic as a Colossus 2 user, but no size is given. xAI's 121 is an upper bound and Anthropic's 224 a lower bound. Need: a figure, or a decision, e.g. split Colossus 2's 111 units 1/3 to Anthropic (xAI 84, Anthropic 261).
2. **Lab compute projection.** Epoch publishes only year-end lab estimates; there is no mid-2026 per-lab figure. I used the stock-matched 1.65×. Lab-specific evidence suggests OpenAI could be up to about 25% higher and Meta much higher (Meta-owned frontier sites 3.9×). Decide: 1.65× (keeps the national cap slack) or 2.04× (Epoch's trend; labs would hold 53% of US stock and the 50% cap would bind at t=0).
3. **Talent.** No levels.fyi acceptance data exists; the Zeki arrival-share fallback needs sign-off. Problems: periods are mixed (Anthropic, OpenAI and Meta from 2025; GDM Q3 2026), and xAI is not in Zeki's sample, so its 20.0 is a Metix-retention proxy (alternative 17.1). A paid Zeki pull of Q2 2026 ratios for all five labs would settle it.
4. **xAI lobbying.** No xAI filing was found, so the proxy is X Corp's Q3 2025 $0.53M annualised. SpaceX-level lobbying (parent basis, as used for Alphabet and Meta) was not retrieved because OpenSecrets and LDA were blocked. Influence ranges from 30 (no lobbying) to about 40.
5. **Influence components not reported:**
   - Meta's API share (Menlo Dec 2025 does not break out Llama; 9% is from Jul 2025).
   - Google, Meta and xAI coding shares (split from the residual).
   - Meta and Grok consumer users (app-only Sensor Tower against Meta's 1B+ claim).
   - No 2026 Menlo enterprise report exists.
6. **Anthropic capital.** The PitchBook compute-per-revenue figures are unaudited estimates. The H1 2026 compute total in the S-1 was not found. 26 (Q2 run-rate) against 20 (H1-annualised) is a judgement call.
7. **Per-turn acquisition cap.** 20 units is now 7% of OpenAI's fleet, against 11% at Jan seeds. Raise it to 30 to keep the old ratio? (Design call.)
8. **Prosperity balance.** With C in ECI, a turn of frontier progress is worth 0.8 × 1.2 = 0.96 Prosperity, against 1.32 before. Influence (unchanged scale) therefore weighs about 1.4× more relative to capability. Keep 0.8/0.2, or move to about 0.85/0.15 to preserve the old balance?
9. **T1 intrusion floor.** Real seed gaps are small (max 8.9 ECI), so the largest copy at t=0 is 1.8 ECI, and Google DeepMind, Meta and xAI gain only about 1.4–1.8 from the top two. T1 may need the ladder sooner. The 0.35 minimum might be set to 0.5 for salience; flag for the scripted checks.
10. **ECI snapshot.** Epoch revises ECI as benchmarks are added. Values here come from the 9 Oct 2026 download, and the spec should state that date.
11. **Qwen open-weight flagship.** The open Qwen3.8 2.4T-A95B checkpoint (reported 12 Aug) has no ECI entry under open weights, and its first-party price is unverified. It is excluded from the juror choice; this does not change the Kimi recommendation.
