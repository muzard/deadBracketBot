# Trust-decay formula for early readings — 2026-07-02

Fitted on 13,300 obs pairs from the 12 half-hourly stations (Jun 3 – Jul 2). Validated against direct hold-rate counts.

## The formula

A feed reading `T̃ = T + m` (whole-degree target T, tenths margin m) taken `τ` minutes before the next METAR sampling instant, in local-hour bucket `h`:

```
p_next(τ, m, h) = Φ( (m + 0.5 + c(h)·τ/60) / (σ₃₀(h)·(τ/30)^H(h)) )

p_hold = 1 − (1 − p_next)·(1 − ρ(h))          # rescue by any later ob that day
p_trust = p_hold × 0.995                       # resolver-ingestion factor (prev. report)
```

Φ = standard normal CDF. The `+0.5` is the METAR round-to-nearest buffer. For co-located-but-different sensors (Tier 2 feeds), replace σ² → σ(τ)² + σ_sensor² with σ_sensor calibrated from paired logs.

## Fitted parameters (your data, rounding-variance removed)

| Local hours | σ₃₀ (°C) | H | drift c (°C/h) | rescue ρ | n |
|---|---|---|---|---|---|
| 08–12 | 0.61 | 0.60 | **+1.15** | ~0.8 | 3,621 |
| 12–17 | 0.73 | 0.46 | +0.33 | 0.63 | 5,054 |
| 17–21 | 0.72 | 0.66 | **−0.97** | ~0.2 | 3,938 |
| night | 0.76 | 0.75 | −0.22 | ~0.3 | 10,955 |

Validation: model predicts midday P(next ob ≥ T | m=0, τ=30) = 0.82; directly counted = 0.853 (n=2,650). Rest-of-day hold = 0.946 counted, matching ρ=0.63.

## Literature anchoring

- **Scaling form σ(τ) ∝ τ^H** is the structure function of near-surface temperature. Turbulence theory (Kolmogorov–Obukhov–Corrsin passive-scalar scaling) gives H = 1/3 as the short-lag floor; a random walk gives 0.5; persistent (long-memory) surface-temperature anomalies push it higher (cf. Koscielny-Bunde et al., PRL 1998, at daily scales). Your midday fit H≈0.46 ≈ Brownian; morning/evening H≈0.6–0.75 reflects the strong diurnal trend regime.
- σ₆₀ ≈ 0.9–1.3 °C matches the standard ~1 °C persistence-nowcast RMSE at 1 h used as the baseline in observation-nowcasting literature.
- Drift term = diurnal cycle derivative; morning +1.2 °C/h and evening −1 °C/h are textbook mid-latitude summer values.

## What it says (m = 0.4 °C margin, midday params unless noted)

| Lead τ | p_next | p_trust | comment |
|---|---|---|---|
| 6 min (Météo-France) | 0.996 | ~0.99 | σ(6)≈0.35 °C — near-certain |
| 10 min (KNMI/FMI/JMA) | 0.99 | ~0.985 | |
| 20 min | 0.96 | ~0.98 | rescue does real work now |
| 30 min | 0.93 | ~0.97 | ≈ your current METAR-to-METAR edge |
| 60 min | 0.85 | ~0.94 | hourly-only stations, full-interval lead |
| 30 min, **evening** | 0.80 | ~0.81 | drift −1 °C/h, rescue ≈ dead |

So yes — trust decays with lead, roughly as `Φ(k/τ^H)` once drift is small, and the decay is *asymmetric by time of day*: a morning reading at 30-min lead is nearly as good as a midday 10-min one (+drift bails you out), while an evening spike at any lead is the weakest signal exactly when dead-bracket asks look juiciest. Do not chase 17:00+ feed spikes at 0.70 asks with the midday p.

## Drop-in function

```python
from math import erf, sqrt
PAR = {  # bucket: (sigma30, H, drift_C_per_h, rescue)
  'morn':  (0.61, 0.60,  1.15, 0.80),
  'mid':   (0.73, 0.46,  0.33, 0.63),
  'eve':   (0.72, 0.66, -0.97, 0.20),
  'night': (0.76, 0.75, -0.22, 0.30)}

def p_trust(tau_min, margin_c, hour_local, sensor_sigma=0.0, resolver=0.995):
    b = 'morn' if 8<=hour_local<12 else 'mid' if 12<=hour_local<17 \
        else 'eve' if 17<=hour_local<21 else 'night'
    s30, H, c, rho = PAR[b]
    sig = sqrt((s30*(tau_min/30)**H)**2 + sensor_sigma**2)
    z = (margin_c + 0.5 + c*tau_min/60) / sig
    p_next = 0.5*(1+erf(z/sqrt(2)))
    return (1-(1-p_next)*(1-rho)) * resolver
```

## Limitations (read before sizing)

1. **Gaussian tails are optimistic in convection.** Thunderstorm cold pools drop 3–5 °C in minutes and are underrepresented in a 1-month Jun–Jul European sample. When CB/TCU/TS is in the METAR or radar shows cells nearby, cap p at ~0.95 regardless of the formula.
2. Parameters are Europe-summer. Refit per station-month from the same 3-line analysis; tropical stations (Singapore, KL, Manila) will show different σ and much weaker drift.
3. ρ (rescue) is coarse — it's the biggest lever at long leads and deserves a per-hour fit once you have more days.
4. Feed-vs-METAR sensor identity (Tier 1 should give σ_sensor ≈ 0.1–0.2; Tier 2 unknown) — measure from paired logs before trusting m at face value.
5. Kelly input: use p_trust with these caps, quarter-Kelly as before. The formula's main practical payoff is the evening cutoff and the τ-dependent margin: to keep p_trust ≥ 0.99 you need m ≈ 0.4 at τ=10 but m ≈ 1.2 at τ=30 midday.
