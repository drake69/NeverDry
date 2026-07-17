# Evidence & Methodology — Load-Bearing Verification of the model & sensor notes

This document makes the scientific claims in `scientific-model.md` and
`soil-sensors.md` **auditable**. It records (a) the reproducible protocol used
to check each load-bearing claim against a primary source, and (b) the resulting
evidence table with verdicts, sources, and required corrections.

The original references were assembled informally, without a tracked
literature-review trail, so their relevance could not be audited retrospectively.
Rather than reconstruct that, this document establishes **independent
reproducibility**: each load-bearing claim is checked against a primary source by
a documented method any reviewer can re-run.

**Outcome of the verification (2026-06-30):** the cited works were confirmed
real (DOIs/URLs resolve with matching metadata); the load-bearing claims were
checked against datasheets and peer-reviewed sources; two factual errors and one
soil constant were corrected; unsourced figures were flagged as illustrative. A
small set of residual `scientific-model.md` references not tied to a load-bearing claim is still
to be reviewed (tracked as DA-4). Where a primary full-text was paywalled, the
verdict rests on the abstract/publisher record or a secondary source, and says so.

---

## 1. Method (reproducible by hand)

1. **Decompose** each document into atomic, load-bearing claims (a number, a
   spec, or a causal statement that the design relies on).
2. **Classify** each: `math` (self-checkable), `datasheet` (vendor spec),
   `literature` (peer-reviewed), or `estimate` (engineering rule-of-thumb).
3. **Search** the primary source. Tools: general web search + direct page fetch.
   Query pattern: `<author/vendor> <year> <subject> <specific figure/unit>`.
   Prefer the official datasheet (vendor PDF) or the cited paper.
4. **Acceptance rule (no fabrication):** record a source only if the *exact
   figure/statement* is found in it. A DOI that merely resolves is **not**
   sufficient — the claim must actually appear. If not found → mark the claim
   `illustrative / unsourced`, do **not** attach a weak citation.
5. **Grade confidence:**
   - 🟢 **High** — math recomputed, or exact value read from official datasheet.
   - 🟡 **Medium** — supported by a peer-reviewed source (possibly via abstract).
   - 🔴 **Illustrative** — no primary source found; engineering estimate.
   - ⛔ **Contradicted** — the source gives a materially different value.

**To re-run:** take any row below, issue the listed query on Crossref / Google
Scholar / the vendor site, open the source, and confirm the figure from it;
never invent the value.

**Honest limits:** full texts of Topp 1980, Robinson 2008, Vaz 2013, Iwata 2017
were paywalled; those rows rest on abstracts/secondary sources. Retail prices
were not checked against live listings.

---

## 2. Evidence table — sensor specs (soil-sensors.md)

| ID | Claim | Verdict | Source / finding |
|---|---|---|---|
| S1 | TEROS 12: 3.6 mA typ (max 16) during 25 ms, 0.03 mA sleep, 4–15 VDC | 🟢 CONFIRMED (exact) | METER TEROS 11/12 manual, Electrical/Timing table. Note: 25 ms is the *minimum* measurement duration (max 150 ms). |
| S2 | TEROS 12: ~1 L influence volume; ±3% generic / ±2% soil-specific; ~10-yr life | 🟢/🟡 MOSTLY | Volume 1010 mL ✓ (brochure, not spec table). Generic ±0.03 m³/m³ ✓. Soil-specific is ±0.01–0.02 (so ±2% is worst case). "10 years" is **marketing copy, not a spec/warranty** — soften wording. |
| S3 | TEROS 21: matric potential, range **0 to −100 kPa**, SDI-12, factory cal. | ⛔ CONTRADICTED | Actual range **−9 to −100,000 kPa**; −100 kPa is only the *accurate-band* limit. Matric + SDI-12 ✓. **Fixed in soil-sensors.md.** |
| S4 | WATERMARK 200SS: granular matrix, >5 yr buried, not for sand/potting | 🟢 CONFIRMED | IRROMETER FAQ verbatim: 5+ yr; "sand or potting mixes do not present good conditions." |
| S5 | VH400: ~10 mA continuous, 0–3 V, 3–5 yr life | 🟡 PARTIAL | 0–3 V ✓. Current spec is **<13 mA** (not 10). **Lifespan 3–5 yr NOT on any official page** — drop or flag. |
| S6 | Prices (~250/320/450/30/30/4 EUR) | 🟡 NOT VERIFIED | Order-of-magnitude plausible; not checked vs live listings. Keep as "approx, indicative." |
| S7 | TEROS 12 energy ≈ **2.7 mAh/day ≈ 13.5 mWh/day** @1/min, 5 V | ⛔ CONTRADICTED (math error) | Inputs ✓ but result wrong by 3.6×. Correct: **≈ 0.76 mAh/day ≈ 3.8 mWh/day** (the intermediate 2720 mA·s was ÷1000 instead of ÷3600). Sanity: 0.03 mA sleep alone over 24 h = 0.72 mAh/day. **Fixed in soil-sensors.md.** |

## 3. Evidence table — soil-science / model (scientific-model + soil-sensors)

| ID | Claim | Verdict | Source / finding |
|---|---|---|---|
| L1 | Clay loam θ_FC ≈ 0.32, θ_PWP ≈ 0.14 m³/m³ | 🟡/⛔ | FC ≈ 0.32 ✓ (Saxton-Rawls give ~0.33–0.35). **PWP 0.14 is too low** — Saxton-Rawls & standard tables give ~0.18–0.21 for clay loam; 0.14 implies a *loam*. **Corrected in soil-sensors.md** (the example still works with ~0.20). |
| L2 | Dielectric permittivity → VWC (basis of FDR/TDR) | 🟢 SUPPORTED | Topp et al. 1980 — exactly the paper's central result (Ka↔θ independent of soil type). |
| L3 | Low-cost capacitive ~20–50 mL vs pro FDR ~1 L; small vol → more local noise | 🟡 PARTIAL | ~1 L ✓ (10HS) and the principle ✓. The specific **"20–50 mL" figure is not in the cited refs** — mark as estimate. |
| L4 | Temp-only ET: **40–60 % of variance**; Hargreaves RMSE ~1 vs ~2–3 mm/day simple | 🟡/⛔ | RMSE ~1 mm/day for Hargreaves ✓ (Droogers & Allen: RMSD 0.81, ~0.93 w/ errors). **"40–60 % variance" CONTRADICTED** — R² ≈ 0.90 (~90 %). **"2–3 mm/day for simple temp models" NOT FOUND.** **Corrected in scientific-model.md** (the model is *less* uncertain than stated, not more). |

## 4. Evidence table — engineering estimates (soil-sensors.md)

| ID | Claim | Verdict | Note |
|---|---|---|---|
| E1 | Capacitive corrosion 4-phase timeline (wks→>12 mo) | 🔴 ILLUSTRATIVE | Phenomenon documented (Scargill teardown; MDPI Agronomy 15:2788: "unreliable within a single growing season"), but the **phase boundaries are unsourced**. |
| E2 | VWC variability <15 % over ~50 m² under drip | 🔴 ILLUSTRATIVE | Measured CVs land near 16–21 %; the "<15 % / 50 m²" pairing is not in any source. |
| E3 | Temp-ET within ~20–40 % of demand (residential) | 🟡/🔴 PARTIAL | ~18–23 % is literature-grounded (MDPI Agriculture 11:124); the 40 % upper bound + "residential" framing are illustrative. |
| E4 | Sizing rule **N = ⌈A/30 m²⌉** | 🔴 ILLUSTRATIVE / counter-evidenced | UF/IFAS HS1222 explicitly **rejects** per-area formulas (use variogram range / management zones). Keep only as a crude heuristic, flagged. |
| E5 | Representativeness ~20–50 m² (drip) / ~5–15 m² (sprinkler) | 🔴 ILLUSTRATIVE | Concept grounded (correlation lengths 30–60 m), exact area brackets unsourced. |
| E6 | Depth 15–25 cm; 15–25 cm from emitter; **2 mm air gap → 10–20 % VWC** | 🟡/🔴 PARTIAL | Placement geometry ✓ (vineyard/extension guides). The **"2 mm → 10–20 %" figure is unconfirmed** (qualitatively right — air ε≈1 vs water ε≈80; Iwata 2017 EJSS paywalled). Mark the % as illustrative. |

---

## 5. Corrections applied to the notes (2026-06-30)

The verification above surfaced the following; all have been **corrected** in
`scientific-model.md` and `soil-sensors.md`.

**Material (factual errors — fixed):**
1. **soil-sensors.md §2.1 (S7)** — energy budget: "≈ 2.7 mAh/day ≈ 13.5 mWh/day"
   → **"≈ 0.76 mAh/day ≈ 3.8 mWh/day"** (arithmetic error inherited from the spec).
2. **soil-sensors.md §2 (S3)** — TEROS 21 range: "range 0 to −100 kPa" → **"range
   −9 to −100,000 kPa; accurate band −9 to −100 kPa."**
3. **scientific-model.md §2 / soil-sensors.md §3.1 (L4)** — removed "ET from T
   explains 40–60 % of variance" and the "~2–3 mm/day" comparison. Supported
   statement: *Hargreaves-type temperature ET reaches RMSE ≈ 0.8–1 mm/day vs
   Penman-Monteith (Droogers & Allen 2002); simple mean-T linear models are less
   accurate but a specific RMSE band is not well established.* (This makes the ET
   model look **better**, not worse.)
4. **soil-sensors.md §3 (L1)** — clay-loam PWP: 0.14 → **~0.20 m³/m³** (FC 0.32 stays).

**Softened / flagged as illustrative (not errors, but unsourced):**
5. **soil-sensors.md** — E1, E2, E4, E5, and the "2 mm→10–20 %" of E6 flagged as
   *illustrative engineering estimates*, with the IFAS counter-point added to E4.
6. **soil-sensors.md §2** — TEROS 12 "10 years" → "up to ~10 years (vendor)";
   VH400 current "~10 mA" → "<13 mA", unverified "3–5 yr" lifespan dropped.

**Verdict:** of ~15 load-bearing claims, **3 fully confirmed** (S1, S4, L2),
**~5 partially confirmed**, **2 contradicted by hard sources** (S3, S7),
**1 wrong value** (L1 PWP), **1 over-pessimistic** (L4 variance), and **~5 are
illustrative estimates**. The exercise found two real factual errors and one
wrong soil constant that had been carried verbatim from the original spec.

---

## Revision history

| Date | Change |
|---|---|
| 2026-06 | Initial — load-bearing verification of the model & sensor notes. |
