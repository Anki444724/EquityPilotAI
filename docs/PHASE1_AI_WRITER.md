# Phase 1 — Production AI Writer Integration

Offline template composer vs OpenRouter, same evidence, same routing.

## Summary

| Ticker | Latency before | Latency after | Tokens | Cost USD | Prose chars before | after | Drift |
|---|---|---|---|---|---|---|---|
| TCS | 57 ms | 45,901 ms | 27,419 | 0.00554 | 13,779 | 18,483 | 0 |
| RELIANCE | 77 ms | 40,612 ms | 27,185 | 0.00533 | 13,812 | 16,411 | 0 |

Skipped (outside the coverage universe): AAPL, MSFT.

## TCS — Tata Consultancy Services Ltd

| Section | Evidence provider | Conf | Cites | Written by | Tokens |
|---|---|---|---|---|---|
| Executive Summary | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2680 |
| Business Model | Financial Database | 0.85 | 8 | OpenRouter | 1978 |
| Revenue Segments | Financial Database | 0.85 | 8 | OpenRouter | 1718 |
| Financial Performance | Financial Database | 0.85 | 8 | OpenRouter | 2051 |
| Valuation | Valuation Engine | 0.8 | 8 | OpenRouter | 1470 |
| Bull Thesis | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2584 |
| Bear Thesis | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2673 |
| Risks | Financial Database | 0.85 | 8 | OpenRouter | 2024 |
| Catalysts | Market Data (Finnhub/FMP) | 0.44 | 2 | OpenRouter | 1925 |
| Management Commentary | — | 0.0 | 0 | none | 0 |
| Latest News | Market Data (Finnhub/FMP) | 0.44 | 2 | OpenRouter | 1012 |
| Quality Scores | Scoring Engine | 0.75 | 8 | OpenRouter | 1522 |
| Risk Scores | Scoring Engine | 0.75 | 8 | OpenRouter | 1604 |
| Institutional Score | Scoring Engine | 0.75 | 8 | OpenRouter | 1427 |
| Investment Verdict | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2751 |

Declined for want of relevant evidence (expected — the writer refused to fabricate): revenue_segments.

## RELIANCE — Reliance Industries Ltd

| Section | Evidence provider | Conf | Cites | Written by | Tokens |
|---|---|---|---|---|---|
| Executive Summary | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2726 |
| Business Model | Financial Database | 0.85 | 8 | OpenRouter | 1978 |
| Revenue Segments | Financial Database | 0.85 | 8 | OpenRouter | 1732 |
| Financial Performance | Financial Database | 0.85 | 8 | OpenRouter | 2031 |
| Valuation | Valuation Engine | 0.8 | 8 | OpenRouter | 1493 |
| Bull Thesis | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2724 |
| Bear Thesis | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2698 |
| Risks | Financial Database | 0.85 | 8 | OpenRouter | 1733 |
| Catalysts | Market Data (Finnhub/FMP) | 0.44 | 2 | OpenRouter | 1734 |
| Management Commentary | — | 0.0 | 0 | none | 0 |
| Latest News | Market Data (Finnhub/FMP) | 0.44 | 2 | OpenRouter | 1020 |
| Quality Scores | Scoring Engine | 0.75 | 8 | OpenRouter | 1533 |
| Risk Scores | Scoring Engine | 0.75 | 8 | OpenRouter | 1565 |
| Institutional Score | Scoring Engine | 0.75 | 8 | OpenRouter | 1446 |
| Investment Verdict | Synthesis of retrieved evidence | 0.7 | 8 | OpenRouter | 2772 |

Declined for want of relevant evidence (expected — the writer refused to fabricate): revenue_segments.

## Prose sample — TCS

### Financial Performance

**Before — Offline template composer** (provider `Financial Database`, confidence 0.85, 8 citations)

```
**Answer the analyst's question**

The platform's figures put revenue (fy26) at 267,021.00 ₹ cr [Revenue]. That is the anchor for the assessment below.

Supporting evidence:

- Cash flow from operations (FY26) of 56,241.00 ₹ cr [Cash flow from operations], per 08 Historical CF.
- Free cash flow (FY26) of 56,241.00 ₹ cr [Free cash flow], per 08 Historical CF.
- Revenue history of FY22 191,754, FY23 225,458, FY24 240,893, FY25 255,324, FY26 267,021 ₹ cr [Revenue history], per 06 Historical IS.
- Forecast revenue CAGR of 8.91 % [Forecast revenue CAGR], per Forecast engine.

**Interpretation.** Read together, these figures suggest the position is best judged on the balance between the strongest and weakest measures above rather than on any single metric. This is the platform's reading of its own outputs, not a forecast of market behaviour.

**Caveat.** This is analysis, not investment advice
```

**After — OpenRouter `openai/gpt-4o-mini`** (provider `Financial Database`, confidence 0.85, 8 citations, 1985 tokens)

```
**Revenue and Growth**  
Tata Consultancy Services Ltd (TCS) reported a revenue of 267,021 ₹ cr for FY26 [Revenue]. This reflects a consistent upward trend in revenue, increasing from 191,754 ₹ cr in FY22 to 255,324 ₹ cr in FY25, indicating a robust growth trajectory.

**Margins and Profitability**  
The EBITDA for FY26 was 72,398 ₹ cr, resulting in an EBITDA margin of 27.11% [EBITDA][EBITDA margin]. The EBIT stood at 66,838 ₹ cr, contributing to a profit after tax (PAT) of 49,454 ₹ cr, which corresponds to a net margin of 18.52% [EBIT][Profit after tax][Net margin]. These figures suggest that TCS maintains healthy profitability levels, although the margins have not been explicitly detailed over the years.

**Cash Generation**  
TCS generated a cash flow from operations of 56,241 ₹ cr in FY26, with free cash flow also reported at 56,241 ₹ cr [Cash flow from operations][Free cash flow]. T
```

### Investment Verdict

**Before — Offline template composer** (provider `Synthesis of retrieved evidence`, confidence 0.7, 8 citations)

```
**Answer the analyst's question**

The platform's figures put weighted intrinsic value at 1,785.92 ₹ [Weighted intrinsic value]. That is the anchor for the assessment below.

Supporting evidence:

- Current market price of 2,432.00 ₹ [Current market price], per Market data.
- Market capitalisation of 880,206.00 ₹ cr [Market capitalisation], per Market data.
- Revenue (FY26) of 267,021.00 ₹ cr [Revenue], per 06 Historical IS.
- EBITDA (FY26) of 72,398.00 ₹ cr [EBITDA], per 06 Historical IS.

**Interpretation.** Read together, these figures suggest the position is best judged on the balance between the strongest and weakest measures above rather than on any single metric. This is the platform's reading of its own outputs, not a forecast of market behaviour.

**Caveat.** This is analysis, not investment advice, and it rests only on the evidence cited. Figures the platform does not hold are 
```

**After — OpenRouter `openai/gpt-4o-mini`** (provider `Synthesis of retrieved evidence`, confidence 0.7, 8 citations, 2715 tokens)

```
**Current Valuation and Market Position**  
Tata Consultancy Services Ltd (TCS) is currently trading at a market price of 2,432.00 ₹ [Current market price]. The company's market capitalisation stands at 880,206.00 ₹ cr [Market capitalisation]. The revenue for FY26 is reported at 267,021.00 ₹ cr [Revenue], with an EBITDA of 72,398.00 ₹ cr [EBITDA] and an EBITDA margin of 27.11% [EBITDA margin]. The profit after tax for the same fiscal year is 49,454.00 ₹ cr [Profit after tax], resulting in a net margin of 18.52% [Net margin]. 

**Financial Performance and Ratios**  
The financial metrics indicate a strong operational performance, with a return on equity of 48.97% [Return on equity] and a return on capital employed of 60.03% [Return on capital employed]. The company maintains a healthy interest coverage ratio of 54.47x [Interest coverage], suggesting robust earnings relative to interest ob
```
