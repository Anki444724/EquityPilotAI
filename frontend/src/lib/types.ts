/** API contracts — mirrors the backend Pydantic schemas. */

export interface LiveMarket {
  live_price: number | null;
  current_price: number | null;
  price_source: string | null;
  last_updated: string | null;
  market_status: string;
  change: number | null;
  change_percent: number | null;
  volume: number | null;
}

export interface CompanySummary {
  id: string;
  name: string;
  ticker: string;
  exchange: string;
  sector: string | null;
  industry: string | null;
  market_cap: number | null;
  current_price: number | null;
  market: LiveMarket | null;
}

export interface CompanyDetail extends CompanySummary {
  isin: string | null;
  description: string | null;
  website: string | null;
  incorporated_year: number | null;
  shares_outstanding: number | null;
  data_version: number;
}

export interface DataCoverage {
  has_data: boolean;
  coverage: number;
  fiscal_years: number[];
  items_total: number;
  items_populated: number;
}

export interface CompanyProfile {
  company: CompanyDetail;
  coverage: DataCoverage;
  latest_fiscal_year: number | null;
  revenue: number | null;
  ebitda: number | null;
  pat: number | null;
  eps: number | null;
  ebitda_margin: number | null;
  pat_margin: number | null;
  net_debt: number | null;
  total_assets: number | null;
  balance_sheet_ties: boolean | null;
}

export interface SearchResponse {
  query: string;
  total: number;
  results: CompanySummary[];
}

export interface PaginatedCompanies {
  total: number;
  page: number;
  page_size: number;
  results: CompanySummary[];
}

export interface SessionUser {
  id: string;
  email: string;
  name: string;
  role: "viewer" | "analyst" | "admin";
  is_dev_identity: boolean;
}

export interface CoverageStats {
  companies: number;
  companies_with_financials: number;
  sectors: number;
  fact_rows: number;
  fiscal_years: number[];
}

export interface SectorBreakdown {
  sector: string;
  count: number;
  market_cap: number;
}

export interface DashboardOverview {
  coverage: CoverageStats;
  sectors: SectorBreakdown[];
  largest: CompanySummary[];
  recently_added: CompanySummary[];
}

/* ---------------------------------------------------------------- Module 2 */

export interface MetricRow {
  key: string;
  label: string;
  unit: string;
  values: (number | null)[];
  is_subtotal: boolean;
  is_header: boolean;
  indent: number;
  note: string | null;
}

export interface MetricSection {
  key: string;
  title: string;
  rows: MetricRow[];
}

export interface PeriodMeta {
  fiscal_years: number[];
  labels: string[];
  latest_fiscal_year: number | null;
  currency: string;
  unit: string;
}

export interface CompanyRef {
  id: string;
  name: string;
  ticker: string;
  exchange: string;
  sector: string | null;
}

export interface Flag {
  key: string;
  label: string;
  triggered: boolean;
  severity: "info" | "warn" | "alert";
  detail: string | null;
}

export interface AnalysisResponse {
  company: CompanyRef;
  periods: PeriodMeta;
  sections: MetricSection[];
  has_data: boolean;
  warnings: string[];
}

export interface StatementResponse extends AnalysisResponse {
  statement: string;
}

export interface RatioResponse extends AnalysisResponse {
  wacc_assumption: number | null;
}

export interface WorkingCapitalResponse extends AnalysisResponse {
  flags: Flag[];
  cost_of_debt_assumption: number | null;
}

export type CapexResponse = AnalysisResponse;

export interface DebtInstrumentRow {
  instrument: string;
  lender: string | null;
  security: string;
  rate_type: string;
  amount: number;
  share_of_debt: number | null;
  interest_rate: number | null;
  maturity_year: number | null;
  currency: string;
}

export interface MaturityBucket {
  year: number;
  amount: number;
  share_of_debt: number | null;
  cumulative: number;
  ebitda_coverage: number | null;
}

export interface CovenantRow {
  key: string;
  label: string;
  threshold: number;
  actual: number | null;
  direction: string;
  unit: string;
  compliant: boolean | null;
  headroom: number | null;
}

export interface DebtReconciliation {
  instrument_total: number;
  balance_sheet_gross_debt: number;
  difference: number;
  reconciled: boolean;
}

export interface DebtResponse extends AnalysisResponse {
  instruments: DebtInstrumentRow[];
  maturity_ladder: MaturityBucket[];
  covenants: CovenantRow[];
  reconciliation: DebtReconciliation | null;
  blended_rate: number | null;
  floating_rate_share: number | null;
  foreign_currency_share: number | null;
  flags: Flag[];
}

export interface OwnershipSignal {
  signal: string;
  score: number | null;
  detail: string | null;
}

export interface ShareholdingResponse extends AnalysisResponse {
  signal: OwnershipSignal | null;
  flags: Flag[];
}

export interface StatementSummary {
  fiscal_year: number;
  revenue: number | null;
  ebitda: number | null;
  ebitda_margin: number | null;
  pat: number | null;
  pat_margin: number | null;
  eps: number | null;
  cfo: number | null;
  free_cash_flow: number | null;
  net_debt: number | null;
  total_assets: number | null;
  roe: number | null;
  roce: number | null;
  balance_sheet_ties: boolean;
}

export interface FinancialsOverview {
  company: CompanyRef;
  periods: PeriodMeta;
  summary: StatementSummary[];
  revenue_cagr_5y: number | null;
  revenue_cagr_full: number | null;
  has_data: boolean;
  warnings: string[];
}

/* ---------------------------------------------------------------- Module 3 */

export type ScenarioName = "bear" | "base" | "bull";
export type RevenueMethodName = "cagr" | "volume_price" | "segment" | "organic_acquisition";

export interface DriverOut {
  name: string;
  label: string;
  value: number;
  unit: string;
  group: string;
  source: string;
  citation: string | null;
  note: string | null;
  by_year: Record<number, number>;
}

export interface AssumptionSet {
  scenario: ScenarioName;
  horizon_years: number;
  revenue_method: RevenueMethodName;
  drivers: DriverOut[];
  provenance: Record<string, number>;
}

export interface ForecastYearOut {
  period: number;
  fiscal_year: number;
  revenue: number;
  revenue_growth: number | null;
  ebitda: number;
  ebitda_margin: number;
  depreciation: number;
  ebit: number;
  ebit_margin: number | null;
  other_income: number;
  interest_expense: number;
  pbt: number;
  tax_expense: number;
  effective_tax_rate: number;
  pat: number;
  pat_margin: number | null;
  eps: number | null;
  net_working_capital: number;
  change_in_nwc: number;
  capex: number;
  net_block: number;
  gross_debt: number;
  cash: number;
  net_debt: number;
  equity: number;
  cfo: number;
  cfi: number;
  cff: number;
  fcff: number;
  fcfe: number;
  free_cash_flow: number;
  roe: number | null;
  roce: number | null;
  roic: number | null;
  net_debt_ebitda: number | null;
  interest_coverage: number | null;
  reconciled: boolean;
}

export interface HistoricalYearOut {
  fiscal_year: number;
  revenue: number;
  ebitda: number;
  ebitda_margin: number | null;
  pat: number;
  eps: number | null;
  free_cash_flow: number;
}

export interface ForecastSummary {
  revenue_cagr: number | null;
  ebitda_cagr: number | null;
  terminal_revenue: number | null;
  terminal_ebitda: number | null;
  terminal_eps: number | null;
  terminal_fcff: number | null;
  debt_converged: boolean;
  debt_iterations: number;
  all_reconciled: boolean;
}

export interface ForecastResponse {
  company: CompanyRef;
  forecast_id: string | null;
  name: string | null;
  scenario: ScenarioName;
  periods: PeriodMeta;
  base_fiscal_year: number;
  years: ForecastYearOut[];
  history: HistoricalYearOut[];
  assumptions: AssumptionSet;
  summary: ForecastSummary;
  sections: MetricSection[];
  warnings: string[];
}

export interface ScenarioOutcomeOut {
  scenario: ScenarioName;
  probability: number;
  terminal_revenue: number;
  terminal_ebitda: number;
  terminal_eps: number | null;
  revenue_cagr: number | null;
  terminal_fcff: number;
  value_per_share: number | null;
  upside: number | null;
}

export interface ScenarioComparisonRow {
  key: string;
  label: string;
  unit: string;
  bear: (number | null)[];
  base: (number | null)[];
  bull: (number | null)[];
}

export interface ScenarioResponse {
  company: CompanyRef;
  forecast_id: string | null;
  periods: PeriodMeta;
  outcomes: ScenarioOutcomeOut[];
  comparison: ScenarioComparisonRow[];
  expected_value: number | null;
  expected_upside: number | null;
  bull_upside: number | null;
  bear_downside: number | null;
  risk_reward: number | null;
  standard_deviation: number | null;
  coefficient_of_variation: number | null;
  verdict: string;
  current_price: number | null;
}

export interface AssumptionUpdateRequest {
  drivers: Record<string, number>;
  scenario?: ScenarioName | null;
  by_year?: Record<string, Record<number, number>>;
  source?: string;
  citation?: string | null;
  requires_review?: boolean;
  horizon_years?: number;
  revenue_method?: RevenueMethodName;
}

/* ---------------------------------------------------------------- Module 4 */

export type ConventionName = "mid_year" | "year_end";
export type TerminalMethodName = "perpetual_growth" | "exit_multiple";

export interface WACCOut {
  risk_free_rate: number; total_erp: number; unlevered_beta: number;
  levered_beta: number; regression_beta: number | null; beta_used: number;
  beta_source: string; size_premium: number; specific_premium: number;
  cost_of_equity: number; pre_tax_cost_of_debt: number; marginal_tax_rate: number;
  after_tax_cost_of_debt: number; market_value_equity: number;
  market_value_debt: number; total_capital: number; weight_equity: number;
  weight_debt: number; debt_to_equity: number; wacc: number; bounded: boolean;
}

export interface WACCScheduleRow {
  period: number; debt_to_equity: number; levered_beta: number;
  cost_of_equity: number; wacc: number;
}

export interface DCFYearOut {
  period: number; cash_flow: number; discount_period: number;
  discount_rate: number; discount_factor: number; present_value: number;
}

export interface DCFOut {
  model: string; convention: string; terminal_method: string;
  years: DCFYearOut[]; sum_pv_explicit: number; terminal_value: number;
  pv_terminal_value: number; terminal_value_pct: number | null;
  enterprise_value: number; net_debt: number | null; equity_value: number;
  shares_outstanding: number; intrinsic_value_per_share: number | null;
  current_price: number | null; upside: number | null; margin_of_safety: number;
  maximum_buy_price: number | null; in_buy_zone: boolean | null;
  discount_rate: number; terminal_growth: number;
  implied_exit_multiple: number | null; implied_perpetual_growth: number | null;
  warnings: string[];
}

export interface MultipleSetOut {
  label: string; pe: number | null; pb: number | null; ev_ebitda: number | null;
  ev_sales: number | null; ev_ebit: number | null; p_fcfe: number | null;
  dividend_yield?: number | null; peg?: number | null;
}

export interface TargetPriceOut {
  key: string; label: string; basis: string; target_multiple: number | null;
  metric: number | null; metric_label: string; implied_value: number | null;
  target_price: number | null; weight: number; rationale: string;
}

export interface JustifiedMultipleOut {
  key: string; label: string; formula: string; justified: number | null;
  actual: number | null; premium_discount: number | null; verdict: string;
}

export interface RelativeOut {
  current: MultipleSetOut; forward: MultipleSetOut[];
  methods: TargetPriceOut[]; justified: JustifiedMultipleOut[];
  blended_target_price: number | null; simple_average_target: number | null;
  median_target: number | null; target_low: number | null;
  target_high: number | null; upside: number | null;
  current_price: number | null; warnings: string[];
}

export interface DDMOut {
  variant: string; value_per_share: number | null; terminal_value: number | null;
  pv_explicit: number | null; implied_dividend_yield: number | null;
  upside: number | null; warnings: string[];
}

export interface ReplacementOut {
  net_block: number; inflation_adjustment: number; adjusted_fixed_assets: number;
  net_working_capital: number; intangible_replacement: number;
  total_replacement_cost: number; net_debt: number;
  equity_replacement_value: number; value_per_share: number | null;
  tobins_q: number | null; upside: number | null; warnings: string[];
}

export interface MethodValueOut {
  key: string; label: string; value_per_share: number | null;
  upside: number | null; weight: number; applicable: boolean; note: string | null;
}

export interface SummaryOut {
  methods: MethodValueOut[]; weighted_value: number | null;
  median_value: number | null; low: number | null; high: number | null;
  current_price: number | null; upside: number | null; margin_of_safety: number;
  maximum_buy_price: number | null; in_buy_zone: boolean | null;
  recommendation: string;
}

export interface QualityIssueOut {
  key: string; message: string; severity: "info" | "warn" | "critical";
  detail: string | null;
}

export interface DataQualityOut {
  grade: string; is_illustrative: boolean; disclosure: string | null;
  headline: string; issues: QualityIssueOut[]; coverage: number | null;
  history_years: number | null; synthetic_sources: string[];
}

export interface ValuationResponse {
  company: CompanyRef; scenario: ScenarioName; horizon_years: number;
  convention: ConventionName; terminal_method: TerminalMethodName;
  wacc: WACCOut; wacc_schedule: WACCScheduleRow[];
  dcf_fcff: DCFOut; dcf_fcfe: DCFOut; relative: RelativeOut; ddm: DDMOut;
  replacement: ReplacementOut; summary: SummaryOut; quality: DataQualityOut;
  scenario_values: Record<string, number | null>; warnings: string[];
}

export interface SensitivityOut {
  company: CompanyRef; row_key: string; row_label: string; row_unit: string;
  row_values: number[]; col_key: string; col_label: string; col_unit: string;
  col_values: number[]; cells: (number | null)[][];
  upside_cells: (number | null)[][]; base_row: number; base_col: number;
  base_value: number | null; minimum: number | null; maximum: number | null;
  current_price: number | null; quality: DataQualityOut;
}

export interface HistogramBucket { lower: number; upper: number; count: number }

export interface SimulationOut {
  company: CompanyRef; trials: number; failed_trials: number;
  mean_value: number | null; median_value: number | null; std_dev: number | null;
  percentiles: Record<string, number>; probability_above_price: number | null;
  current_price: number | null; histogram: HistogramBucket[];
  quality: DataQualityOut;
}

/* ---------------------------------------------------------------- Module 5 */

export interface MetricScoreOut {
  key: string; label: string; score: number; weight: number; origin: string;
  confidence: number; value: number | null; unit: string;
  explanation: string; source: string;
}

export interface ConfidenceOut {
  confidence: number; label: string; verified_pct: number;
  estimated_pct: number; analyst_pct: number; missing_pct: number;
  metrics_total: number; metrics_missing: number;
}

export interface CategoryScoreOut {
  key: string; label: string; raw_score: number; weighted_score: number;
  weight: number; score_pct: number; grade_hint: string;
  confidence: ConfidenceOut; explanation: string; data_sources: string[];
  metrics: MetricScoreOut[];
}

export interface ScoreResponse {
  company: CompanyRef; overall_score: number; grade: string;
  grade_description: string; stars: number; recommendation: string;
  recommendation_rationale: string; conviction: string;
  profile_key: string; profile_label: string; confidence: ConfidenceOut;
  categories: CategoryScoreOut[]; strongest: string[]; weakest: string[];
  warnings: string[]; summary: string;
}

export interface ExplanationItem {
  category: string; category_label: string; metric: string | null;
  metric_label: string | null; score: number; weight: number;
  origin: string; explanation: string; source: string;
}

export interface ExplanationResponse {
  company: CompanyRef; overall_score: number; grade: string;
  recommendation: string; summary: string; recommendation_rationale: string;
  categories: ExplanationItem[]; metrics: ExplanationItem[];
  key_positives: ExplanationItem[]; key_negatives: ExplanationItem[];
  data_gaps: ExplanationItem[]; warnings: string[];
}

export interface HistoryPoint {
  as_of: string; overall_score: number; grade: string; stars: number;
  recommendation: string; confidence: number;
  category_scores: Record<string, number>;
}

export interface ScoreHistoryResponse {
  company: CompanyRef; profile_key: string; points: HistoryPoint[];
  score_change: number | null; trend: string;
}

export interface WeightProfileOut {
  key: string; label: string; description: string; is_builtin: boolean;
  weights: Record<string, number>; top_categories: string[];
}

export interface WeightProfileListResponse {
  profiles: WeightProfileOut[]; active: string;
}

export interface PeerScoreRow {
  company: CompanyRef; overall_score: number; grade: string; stars: number;
  recommendation: string; confidence: number;
  category_scores: Record<string, number>;
}

export interface PeerComparisonResponse {
  profile_key: string; peers: PeerScoreRow[];
  category_medians: Record<string, number>;
}

/* ---------------------------------------------------------------- Module 6 */

export interface CitationOut {
  key: string; label: string; kind: string;
  value: number | string | null; unit: string; source: string;
  fiscal_year: number | null;
}

export interface ClaimBlockOut {
  text: string; claim_type: string; has_citation: boolean; hedged: boolean;
}

export interface GuardrailOut {
  passed: boolean; violations: string[]; disclosure: string;
  composition: Record<string, number>; blocks: ClaimBlockOut[];
}

export interface CitationAuditOut {
  resolved_count: number; unknown_keys: string[]; uncited_numbers: string[];
  coverage: number; is_supported: boolean; summary: string;
}

export interface AIAnalysisResponse {
  company: CompanyRef; capability: string; content: string;
  display_content: string; provider: string; model: string;
  prompt_key: string; prompt_version: number;
  citations: CitationOut[]; citation_audit: CitationAuditOut | null;
  guardrails: GuardrailOut | null;
  prompt_tokens: number; completion_tokens: number; total_tokens: number;
  cost_usd: number; latency_ms: number; cached: boolean;
  fell_back_from: string | null; providers_attempted?: string[];
  warnings: string[];
  /** Present only when the response went through the Language Adapter,
   *  i.e. when a non-English language was requested or detected. Absent on
   *  the English path, which keeps existing payloads unchanged. */
  language?: LanguageBlock | null;
}

export interface AIChatResponse extends AIAnalysisResponse {
  session_id: string; turn_count: number; session_state: string;
}

/* --- Multilingual AI Response Engine --------------------------------- */

export interface LanguageDetection {
  language: string; confidence: number; script: string; reason: string;
  is_mixed: boolean; ambiguous_with: string[];
}

export interface LanguageTranslation {
  language: string; translated: boolean; provider: string; detail: string;
  latency_ms: number; cost_usd: number; integrity_problems: string[];
}

/** Present only when a response went through the Language Adapter. */
export interface LanguageBlock {
  language: string; label: string; native_label: string; script: string;
  bcp47: string; resolved_from: string;
  detected: LanguageDetection; translation: LanguageTranslation;
  latency_ms: number;
}

export interface LanguageSpecOut {
  code: string; label: string; native_label: string; script: string;
  status: "supported" | "planned"; bcp47: string; keeps_english_terms: boolean;
}

export interface LanguageListResponse {
  languages: LanguageSpecOut[]; default: string; canonical: string;
  supported: string[]; planned: string[];
  glossary: Record<string, number>; notes: string[];
}

export interface DetectResponse {
  detected: LanguageDetection; normalised_query: string;
  rewritten: boolean; mapped_terms: { from: string; to: string }[];
}

export interface CapabilityOut {
  key: string; label: string; description: string;
  evidence_kinds: string[]; style: string; version: number;
}

export interface CapabilityListResponse {
  capabilities: CapabilityOut[]; providers_available: string[]; ai_enabled: boolean;
}

export interface ProviderOut {
  name: string; payload_shape: string; default_model: string;
  configured: boolean; endpoint: string;
}

export interface ProviderListResponse {
  providers: ProviderOut[]; preferred: string | null; ai_enabled: boolean;
}

export interface ReportSection {
  capability: string; label: string; content: string;
  citations: CitationOut[]; is_supported: boolean; warnings: string[];
}

export interface AIReportResponse {
  company: CompanyRef; sections: ReportSection[];
  total_tokens: number; total_cost_usd: number;
  generated_with: string; disclosure: string;
}

export interface PromptOut {
  key: string; version: number; label: string; description: string | null;
  task: string; template: string; evidence: string[]; style: string;
  max_tokens: number; temperature: number; is_active: boolean; is_builtin: boolean;
}

export interface AIContextResponse {
  company: { ticker: string; name: string };
  citation_count: number; unavailable: string[];
  citations: (CitationOut & { rendered: string })[];
}

export interface AIUsageResponse {
  persisted: Record<string, number>;
  session: Record<string, unknown>;
  providers_available: string[];
}

/* ------------------------------------------------------------------ *
 * Module 7 — Document Intelligence
 * ------------------------------------------------------------------ */
export interface DocumentSummary {
  id: number;
  company_id: string;
  filename: string;
  title: string | null;
  doc_type: string;
  file_format: string;
  size_bytes: number;
  version: number;
  superseded_by: number | null;
  period: string | null;
  fiscal_year: number | null;
  status: string;
  stage: string;
  progress: number;
  error: string | null;
  page_count: number;
  chunk_count: number;
  table_count: number;
  entity_count: number;
  fact_count: number;
  used_ocr: boolean;
  ocr_pages: number;
  coverage: number;
  avg_confidence: number;
  duplicate_ratio: number;
  processing_ms: number;
  processed_at: string | null;
  created_at: string | null;
}

export interface DocSection {
  kind: string; title: string; start_page: number; end_page: number; confidence: number;
}
export interface DocPage {
  page_number: number; text_source: string; ocr_confidence: number | null; char_count: number;
}
export interface DocumentDetail extends DocumentSummary {
  sections: DocSection[];
  pages: DocPage[];
  metadata: Record<string, string>;
}

export interface UploadResponse {
  document: DocumentSummary;
  action: "created" | "duplicate" | "new_version";
  duplicate_of: number | null;
  superseded: number | null;
  message: string;
}

export interface DocChunk {
  id: number; document_id: number; chunk_index: number; text: string;
  page: number; paragraph: number; section: string; section_title: string | null;
  token_estimate: number; fingerprint: string;
}

export interface DocTable {
  id: number; document_id: number; page: number; table_index: number;
  caption: string | null; unit: string; header: string[]; rows: string[][];
  merged: number[][]; n_rows: number; n_cols: number; confidence: number;
}

export interface DocEntity {
  id: number; document_id: number; kind: string; name: string; normalised: string;
  page: number; context: string | null; confidence: number; mentions: number;
}

export interface DocFact {
  id: number; document_id: number; category: string; field_key: string; label: string;
  value: number | null; text_value: string | null; unit: string;
  period: string | null; fiscal_year: number | null; page: number;
  section: string; confidence: number; evidence: string | null;
}

export interface DocSearchHit {
  chunk_id: number; document_id: number; document_title: string;
  page: number; paragraph: number; section: string; text: string;
  score: number; lexical_score: number; semantic_score: number;
}
export interface DocCitation {
  document_id: number; document_title: string; page: number; section: string;
  paragraph: number; chunk_id: number; quote: string; reference: string;
}
export interface DocSearchResponse {
  query: string; answer: string; confidence: number;
  unavailable_reason: string | null;
  hits: DocSearchHit[]; citations: DocCitation[];
  citation_audit: {
    cited_pages: number[]; available_pages: number[];
    unsupported_pages: number[]; verified: boolean; coverage: number;
  };
  took_ms: number;
}

export interface GraphNode {
  key: string; kind: string; label: string; weight: number; degree: number;
  attributes: Record<string, string>;
}
export interface GraphEdge {
  source: string; target: string; relation: string; weight: number;
  pages: number[]; confidence: number;
}
export interface GraphResponse {
  company: { id: string; name: string; ticker: string; subject_key: string };
  nodes: GraphNode[]; edges: GraphEdge[];
  stats: { nodes: number; edges: number; relations: Record<string, number> };
}

export interface CategoryCoverage {
  category: string; defined: number; extracted: number;
  coverage: number; avg_confidence: number; missing: string[];
}
export interface CoverageResponse {
  company_id: string; fields_defined: number; fields_extracted: number;
  coverage: number; avg_confidence: number; documents: number;
  documents_ready: number; categories: CategoryCoverage[];
}

export interface DocStatistics {
  documents: number; current_documents: number; superseded: number;
  pages: number; chunks: number; tables: number; entities: number; facts: number;
  ocr_documents: number;
  by_type: Record<string, number>; by_status: Record<string, number>;
  queue: Record<string, number>;
  embedding: { provider: string; model: string; dimension: number };
  ocr: { available: boolean; engine: string; version: string | null; language: string };
  supported_formats: string[];
}

export interface DocCapabilities {
  document_types: string[]; file_formats: string[]; sections: string[];
  entity_kinds: string[]; relation_kinds: string[]; pipeline_stages: string[];
  fields: { key: string; label: string; category: string; unit: string; target: string | null }[];
  field_count: number;
  ocr: Record<string, unknown>;
  embedding: { provider: string; model: string; dimension: number };
}

/* ------------------------------------------------------------------ *
 * Module 8 — Portfolio Intelligence
 * ------------------------------------------------------------------ */
export interface Portfolio {
  id: number; owner_id: string; name: string; description: string | null;
  base_currency: string; cost_basis: string; benchmark: string;
  max_position_size: number; max_sector_weight: number; margin_of_safety: number;
  risk_free_rate: number; target_positions: number;
  inception_date: string | null; is_active: boolean; created_at: string | null;
}

export interface Holding {
  ticker: string; company_id: string | null; name: string;
  sector: string | null; industry: string | null;
  quantity: number; average_cost: number | null; cost: number;
  current_price: number | null; price_source: string | null;
  last_updated: string | null; market_status: string | null;
  market_value: number | null;
  unrealised_pnl: number | null; unrealised_return: number | null;
  realised_pnl: number; dividends: number; total_pnl: number | null;
  weight: number; target_weight: number | null; drift: number | null;
  max_position_size: number; is_oversized: boolean;
  score: number | null; rating: string | null; risk_score: number | null;
  intrinsic_value: number | null; target_price: number | null;
  upside: number | null; expected_cagr: number | null;
  liquidity_days: number | null; holding_days: number | null;
  first_bought: string | null;
}

export interface RealisedTrade {
  ticker: string; sell_date: string; buy_date: string; quantity: number;
  cost_per_unit: number; sale_per_unit: number; cost: number; proceeds: number;
  pnl: number; return_pct: number | null; holding_days: number; is_long_term: boolean;
}

export interface CashLedger {
  balance: number; deposits: number; withdrawals: number; buys: number;
  sells: number; dividends: number; fees: number; taxes: number;
  interest: number; net_invested: number;
}

export interface AllocationSlice {
  key: string; label: string; market_value: number; weight: number;
  position_count: number; target_weight: number | null; drift: number | null;
  unrealised_pnl: number | null;
}
export interface Allocation {
  dimension: string; slices: AllocationSlice[]; unclassified_value: number;
  herfindahl: number; effective_count: number;
}

export interface RiskProfile {
  observations: number;
  annualised_return: number | null; annualised_volatility: number | null;
  sharpe: number | null; sortino: number | null;
  max_drawdown: number | null; drawdown_recovered: boolean | null;
  var_95: number | null; cvar_95: number | null; var_99: number | null;
  beta: number | null; alpha: number | null;
  tracking_error: number | null; information_ratio: number | null;
  up_capture: number | null; down_capture: number | null;
  herfindahl: number | null; effective_positions: number | null;
  top_5_concentration: number | null; diversification_score: number | null;
  largest_position_weight: number | null; illiquid_positions: number;
  unavailable: string[];
}

export interface SeriesPoint { as_of: string; value: number; net_flow: number }
export interface Performance {
  twr: number | null; twr_annualised: number | null; mwr: number | null;
  benchmark_return: number | null; active_return: number | null;
  series: SeriesPoint[];
  rolling: { as_of: string; value: number }[];
  underwater: { as_of: string; value: number }[];
  contributions: {
    ticker: string; name: string; weight: number;
    position_return: number; contribution: number;
  }[];
}

export interface RebalanceTrade {
  ticker: string; name: string; action: string;
  current_weight: number; target_weight: number; drift: number;
  value_delta: number; shares: number | null; reason: string;
}

export interface PortfolioSummary {
  portfolio_id: number; name: string; benchmark: string; as_of: string;
  market_value: number; cost_basis: number; cash: number; total_value: number;
  unrealised_pnl: number; realised_pnl: number; dividends: number;
  total_pnl: number; total_return: number | null; position_count: number;
  cash_weight: number | null; unpriced: string[];
  analytics_errors: Record<string, string>;
}

export interface PortfolioView {
  summary: PortfolioSummary;
  holdings: Holding[];
  cash: CashLedger;
  allocations: Record<string, Allocation>;
  risk: RiskProfile;
  performance: Performance;
  rebalance: RebalanceTrade[];
  realised: RealisedTrade[];
  metrics: Record<string, number | null>;
}

export interface AlertEvaluation {
  key: string; label: string; category: string; severity: string;
  status: string; condition: string; action: string;
  observed: number | string | null; threshold: number | string | null;
  ticker: string | null; company_id: string | null; detail: string;
}
export interface AlertSummary {
  counts: Record<string, number>;
  evaluations: AlertEvaluation[];
}
export interface AlertEvent {
  id: number; portfolio_id: number | null; rule_key: string;
  ticker: string | null; label: string; category: string; severity: string;
  status: string; condition: string | null; action: string | null;
  observed: string | null; threshold: string | null; detail: string | null;
  occurrences: number; first_seen: string | null; last_seen: string | null;
}

export interface AttributionRow {
  key: string; label: string; portfolio_weight: number; benchmark_weight: number;
  active_weight: number; portfolio_return: number; benchmark_return: number;
  allocation: number; selection: number; interaction: number; total: number;
}
export interface Attribution {
  rows: AttributionRow[]; portfolio_return: number; benchmark_return: number;
  active_return: number; total_allocation: number; total_selection: number;
  total_interaction: number; residual: number;
}

export interface WatchlistRow {
  id: number; ticker: string; company_id: string | null; name: string;
  sector: string | null; price: number | null; price_source: string | null;
  last_updated: string | null; market_status: string | null;
  buy_below: number | null;
  target_price: number | null; upside: number | null; score: number | null;
  rating: string | null; status: string; note: string | null;
  conviction: string | null; added_on: string | null;
}
export interface WatchlistMeta {
  id: number; owner_id: string; name: string; description: string | null;
}

export interface PortfolioCommentary {
  portfolio_id: number; provider: string;
  sections: { key: string; title: string; body: string }[];
  citations: {
    key: string; label: string; kind: string;
    value: number | string | null; unit: string; source: string;
  }[];
  disclosure: string;
}

export interface PortfolioCapabilities {
  transaction_types: string[]; allocation_dimensions: string[];
  alert_categories: string[]; alert_severities: string[];
  cost_basis_methods: string[];
  rules: {
    key: string; label: string; condition: string; metric: string;
    comparator: string; threshold: number | string | null; severity: string;
    category: string; action: string; scope: string; enabled: boolean;
  }[];
  rating_position_limits: Record<string, number>;
  cache: Record<string, number>;
}

/* ------------------------------------------------------------------ *
 * Module 9 — Report Generator
 * ------------------------------------------------------------------ */
export interface ReportArtifact {
  id: number; fmt: string; filename: string; media_type: string;
  size_bytes: number; page_count: number | null; render_ms: number;
}

export interface ReportSummary {
  id: number; company_id: string; ticker: string; company_name: string;
  owner_id: string; report_type: string; title: string; theme: string;
  version: number; superseded_by: number | null; status: string;
  error: string | null; analyst: string | null; portfolio_id: number | null;
  section_count: number; insufficient_count: number; block_count: number;
  chart_count: number; table_count: number; evidence_count: number;
  word_count: number; citation_coverage: number; citation_clean: boolean;
  audit: Record<string, unknown> | null;
  provenance: Record<string, string> | null;
  build_ms: number; generated_at: string | null; created_at: string | null;
  artifacts: ReportArtifact[];
}

export interface ReportSection {
  key: string; title: string; sufficient: boolean; reason: string;
  block_count: number; chart_count: number; table_count: number;
  evidence_count: number; word_count: number;
}

export interface ReportDetail extends ReportSummary {
  sections: ReportSection[];
  document: Record<string, unknown> | null;
}

export interface GenerateResponse {
  report: ReportSummary; cached: boolean;
  timings: Record<string, number>;
  errors: Record<string, string>;
  message: string;
}

export interface ReportJob {
  id: number; report_id: number; owner_id: string; status: string;
  stage: string; progress: number; attempts: number; error: string | null;
  duration_ms: number; timings: Record<string, number> | null;
}

export interface ReportStatistics {
  reports: number; current: number; superseded: number; artifacts: number;
  bytes_stored: number; by_type: Record<string, number>;
  by_format: Record<string, number>; citation_clean: number;
  mean_coverage: number; mean_build_ms: number;
}

export interface ReportCapabilities {
  report_types: {
    key: string; label: string; sections: string[]; narratives: string[];
  }[];
  formats: { key: string; media_type: string; extension: string }[];
  sections: string[]; chart_kinds: string[]; themes: string[];
  evidence_sources: string[]; block_kinds: string[];
}

/* ------------------------------------------------------------------ *
 * Module 10 — Commercial SaaS platform layer
 * ------------------------------------------------------------------ */

export interface Page<T> {
  items: T[]; total: number; page: number; page_size: number;
}

export interface AuthConfig {
  provider: string; auth_enabled: boolean; native_auth: boolean;
  self_signup: boolean; oauth_providers: string[]; magic_link: boolean;
  email_configured: boolean; password_min_length: number;
  publishable_key: string | null;
}

export interface SessionUserFull {
  id: string; email: string; name: string; role: string;
  is_dev_identity: boolean; avatar_url: string | null;
  tenant_id: number | null; tenant_slug: string | null;
  tenant_name: string | null; permissions: string[]; provider: string;
  email_verified: boolean; mfa_enabled: boolean;
}

export interface TokenResponse {
  access_token: string; token_type: string; expires_in: number;
  csrf_token: string; refresh_token: string | null;
}

export interface PlatformTenant {
  id: number; slug: string; name: string; status: string;
  industry: string | null; country: string; timezone: string;
  base_currency: string; logo_url: string | null;
  primary_colour: string | null; report_disclaimer: string | null;
  storage_bytes: number; member_count: number;
  trial_ends_at: string | null; created_at: string;
}

export interface TenantDetail extends PlatformTenant {
  settings: Record<string, unknown>;
  subscription: Subscription | null;
  storage: Record<string, number>;
}

export interface PlatformUser {
  id: string; email: string; name: string; role: string; status: string;
  avatar_url: string | null; tenant_id: number | null;
  email_verified_at: string | null; last_login_at: string | null;
  last_seen_at: string | null; mfa_method: string; created_at: string;
}

export interface PlatformUserDetail extends PlatformUser {
  permissions: string[]; active_sessions: number; identities: string[];
  failed_login_count: number; locked_until: string | null;
}

export interface Plan {
  id: number; tier: string; name: string; tagline: string | null;
  price_monthly_inr: number; price_annual_inr: number; features: string[];
  quotas: Record<string, number>; limits: Record<string, number>;
  trial_days: number; is_public: boolean; sort_order: number;
}

export interface Subscription {
  id: number; tenant_id: number; plan_tier: string; status: string;
  billing_period: string; period_start: string; period_end: string;
  trial_ends_at: string | null; cancel_at_period_end: boolean;
  cancelled_at: string | null; provider: string | null;
}

export interface QuotaUsage {
  quota: string; label: string; unit: string; used: number;
  allowance: number; remaining: number; utilisation: number;
  unlimited: boolean; exhausted: boolean;
}

export interface LimitUsage {
  limit: string; label: string; used: number; allowance: number;
  unlimited: boolean;
}

export interface Entitlements {
  tenant_id: number; plan_tier: string; plan_name: string; status: string;
  period_start: string; period_end: string; days_remaining: number;
  trial_ends_at: string | null; cancel_at_period_end: boolean;
  read_only: boolean; blocked: boolean; features: string[];
  all_features: { key: string; label: string; included: boolean }[];
  quotas: QuotaUsage[]; limits: LimitUsage[]; warnings: string[];
}

export interface ApiKey {
  id: number; name: string; key_id: string; prefix: string; role: string;
  masked: string; expires_at: string | null; revoked_at: string | null;
  last_used_at: string | null; last_used_ip: string | null;
  call_count: number; created_by: string; created_at: string;
}

export interface IssuedApiKey {
  key: ApiKey; plaintext: string; warning: string;
}

export interface AuditRow {
  id: number; tenant_id: number | null; action: string; category: string;
  severity: string; outcome: string; actor_id: string | null;
  actor_email: string | null; actor_role: string | null;
  resource_type: string | null; resource_id: string | null;
  summary: string; ip_address: string | null; request_id: string | null;
  meta: Record<string, unknown> | null; occurred_at: string;
}

export interface AuditSummary {
  days: number; total: number; failures: number;
  by_category: Record<string, number>; by_severity: Record<string, number>;
  by_action: Record<string, number>;
  daily: { date: string; count: number }[];
}

export interface UsageSeries {
  quota: string; label: string; unit: string;
  points: { date: string; value: number }[]; total: number;
}

export interface UsageOverview {
  tenant_id: number | null; period_start: string; period_end: string;
  quotas: QuotaUsage[]; series: UsageSeries[];
  top_users: { user_id: string; name: string; units: number; events: number }[];
  cost_usd: number;
}

export interface BackgroundJob {
  id: number; tenant_id: number | null; kind: string; status: string;
  priority: number; attempts: number; max_attempts: number;
  progress: number; stage: string | null; error: string | null;
  resource_type: string | null; resource_id: string | null;
  run_after: string | null; started_at: string | null;
  finished_at: string | null; duration_ms: number;
  result: Record<string, unknown> | null; created_at: string;
}

export interface QueueDepth {
  queued: number; running: number; failed: number; dead_letter: number;
  succeeded_24h: number; backlog: number; oldest_queued_seconds: number;
  p50_duration_ms: number; p95_duration_ms: number;
  by_kind: Record<string, number>; healthy: boolean;
}

export interface Schedule {
  kind: string; enabled: boolean; every_seconds: number;
  last_run_at: string | null; next_run_at: string | null;
  last_status: string | null; run_count: number; description: string;
}

export interface PlatformOverview {
  tenants: number; tenants_active: number; tenants_trial: number;
  tenants_past_due: number; users: number; users_active: number;
  mrr_inr: number; arr_inr: number;
  tier_distribution: Record<string, number>; storage_bytes: number;
  documents: number; reports: number; portfolios: number;
  ai_calls_30d: number; requests_24h: number; error_rate: number;
  queue: QueueDepth; health: string;
}

export interface TenantOverview {
  tenant: PlatformTenant; plan: string; plan_tier: string; status: string;
  period_end: string; days_remaining: number; members: number;
  members_active: number; documents: number; reports: number;
  portfolios: number; storage: Record<string, number>;
  api_keys: Record<string, number>; quotas: QuotaUsage[];
  nearing_limit: QuotaUsage[]; audit_7d: AuditSummary;
}

export interface MetricsOverview {
  window_minutes: number; requests: number; errors: number;
  error_rate: number; avg_ms: number; p50_ms: number; p95_ms: number;
  p99_ms: number; max_ms: number; rpm: number;
}

export interface RouteMetric {
  route: string; method: string; count: number; errors: number;
  error_rate: number; avg_ms: number; p95_ms: number; max_ms: number;
}

export interface TrackedError {
  id: number; fingerprint: string; exc_type: string; message: string;
  route: string | null; method: string | null; count: number;
  first_seen_at: string; last_seen_at: string; tenant_id: number | null;
  last_request_id: string | null; resolved_at: string | null;
  stack: string | null;
}

export interface HealthReport {
  status: string; version: string; environment: string;
  uptime_seconds: number; ready: boolean;
  checks: { name: string; ok: boolean; detail: string; duration_ms: number; critical: boolean }[];
}

export interface BackupRecord {
  id: number; kind: string; location: string; size_bytes: number;
  checksum: string | null; table_count: number; row_count: number;
  duration_ms: number; status: string; verified_at: string | null;
  error: string | null; finished_at: string | null;
}

export interface BackupStatus {
  configured: boolean; directory: string; backup_count: number;
  latest_at: string | null; latest_size_bytes: number;
  latest_verified_at: string | null; age_hours: number | null;
  stale: boolean; retention_count: number;
}

export interface RbacMatrix {
  roles: { key: string; label: string; description: string; seniority: number; permission_count: number }[];
  permissions: { key: string; resource: string; verb: string }[];
  matrix: Record<string, string[]>;
}

export interface StorageReport {
  breakdown: Record<string, number>; total_bytes: number; total_mb: number;
  allowance_mb: number; unlimited: boolean; utilisation: number;
}

/* ------------------------------------------------------------------ *
 * Enterprise Admin Panel — Phase 1
 * ------------------------------------------------------------------ */
export interface RecycleBinEntry {
  id: number;
  resource_type: string;
  resource_id: string;
  display_name: string | null;
  payload: Record<string, unknown> | null;
  deleted_by: string | null;
  deleted_by_email: string | null;
  deleted_at: string;
  restored_by: string | null;
  restored_at: string | null;
  purged_by: string | null;
  purged_at: string | null;
  is_active: boolean;
}

export interface SystemComponentStatus {
  name: string;
  status: "ok" | "degraded" | "down" | "disabled";
  detail: string | null;
}

export interface SystemStatus {
  components: SystemComponentStatus[];
  companies: number;
  users: number;
  api_calls: number;
  market_open: "open" | "closed" | "weekend" | "unknown";
  generated_at: string;
}

/* ------------------------------------------------------------------ *
 * Phase 2 — Company Management
 * ------------------------------------------------------------------ */
export interface CompanyAdmin extends CompanyDetail {
  bse_code: string | null;
  listing_status: string;
  index_membership: string | null;
  currency: string;
  reporting_scale: string;
  face_value: number | null;
  listing_date: string | null;
  ceo: string | null;
  employees: number | null;
  headquarters: string | null;
  logo_url: string | null;
  favicon_url: string | null;
  deleted_at: string | null;
}

export interface CompanyVersionOut {
  id: number;
  company_id: string;
  version: number;
  actor_id: string | null;
  actor_email: string | null;
  changes: Record<string, { from: unknown; to: unknown }> | null;
  change_type: string;
  summary: string;
  created_at: string;
}

export interface CompanyBulkEditResult {
  updated: number;
  created: number;
  errors: { row: string; error: string }[];
}

export interface ImportResult {
  imported: number;
  updated: number;
  skipped: number;
  errors: { row: string; error: string }[];
}

export interface MergeResult {
  kept_id: string;
  kept_ticker: string;
  merged_ids: string[];
  removed_count: number;
}

export interface PaginatedCompaniesAdmin {
  total: number;
  page: number;
  page_size: number;
  results: CompanyAdmin[];
}

/* ------------------------------------------------------------------ *
 * Phase 3 — Financial Statements
 * ------------------------------------------------------------------ */
export interface FinancialStatements {
  years: number[];
  statements: Record<number, {
    income: Record<string, number | null>;
    balance: Record<string, number | null>;
    cashflow: Record<string, number | null>;
  }>;
  ratios: Record<string, Array<{ key: string; label: string; unit: string; values: (number | null)[] }>>;
  fiscal_years: number[];
}

export interface FinancialBulkResult {
  updated: number;
  created: number;
  errors: { row: string; error: string }[];
}

export interface FinancialVersion {
  id: number;
  company_id: string;
  version: number;
  actor_email: string | null;
  change_type: string;
  summary: string;
  created_at: string;
}

/* ------------------------------------------------------------------ *
 * Phase 4 — Market Operations Center
 * ------------------------------------------------------------------ */
export interface ProviderInfo {
  name: string; priority: number; configured: boolean; implemented: boolean;
  available: boolean; latency_ms: number | null; last_success: string | null;
  calls: number; rate_limit_remaining: number | null; status: string;
}

export interface ProviderHealth {
  name: string; status: string; configured: boolean; available: boolean;
  latency_ms: number | null; last_success: string | null; calls: number;
  rate_limit_remaining: number | null; priority: number;
}

export interface MarketOverride {
  id: number; company_id: string; ticker: string;
  manual_price: number | null; manual_volume: number | null;
  manual_market_cap: number | null; manual_pe: number | null; manual_pb: number | null;
  reason: string | null; expires_at: string | null; auto_revert: boolean;
  created_by_email: string | null; created_at: string; is_active: boolean;
}

export interface MarketDashboard {
  connected_symbols: number; cache_size: number; cache_hit_rate: number;
  ttl_seconds: number; memory_bytes: number;
  redis: { configured: boolean; backend: string };
  last_refresh: string; active_overrides: number; providers_available: number;
  market_status: string; errors: number; api_calls: number;
}

/* ------------------------------------------------------------------ *
 * Phase 5 — AI Operations Center
 * ------------------------------------------------------------------ */
export interface AIOverride {
  id: number; company_id: string; ticker: string; mode: string;
  manual_score: number | null; manual_confidence: number | null;
  manual_risk: number | null; manual_summary: string | null;
  manual_bull_case: string | null; manual_bear_case: string | null;
  manual_recommendation: string | null; reason: string | null;
  expires_at: string | null; created_by_email: string | null;
  created_at: string; is_active: boolean;
}

export interface AIModelInfo {
  name: string; priority: number; configured: boolean; status: string;
}

export interface AIPromptInfo {
  key: string; version: number; label: string; task: string;
  template: string; max_tokens: number; temperature: number;
  is_active: boolean; is_builtin: boolean; edited_by: string | null;
}

export interface AICostDashboard {
  days: number; total_tokens: number; requests: number;
  avg_latency_ms: number; total_cost_usd: number; daily_cost_usd: number;
  by_provider: Record<string, { tokens: number; cost: number; requests: number }>;
}

/* ------------------------------------------------------------------ *
 * Phase 6 — Document Intelligence Center
 * ------------------------------------------------------------------ */
export interface DocAdmin {
  id: number; company_id: string; filename: string; title: string | null;
  doc_type: string; file_format: string; size_bytes: number; version: number;
  status: string; approval_status: string; approval_reviewer: string | null;
  approved_at: string | null; approval_note: string | null;
  page_count: number; chunk_count: number; fact_count: number;
  used_ocr: boolean; processed_at: string | null;
}

export interface DocAdminPage {
  total: number; page: number; page_size: number; items: DocAdmin[];
}

export interface DocCompareResult {
  old: { id: number; version: number; filename: string; processed_at: string | null };
  new: { id: number; version: number; filename: string; processed_at: string | null };
  changed_fields: { field: string; old: unknown; new: unknown }[];
  changed_count: number; old_fact_count: number; new_fact_count: number;
}

export interface RAGStats {
  documents: number; chunks: number; embeddings: number; vector_count: number;
}

export interface DocSearchResult {
  document_id: number; title: string; chunk_id: number; page: number;
  text: string; score: number;
}

/* ------------------------------------------------------------------ *
 * Phase 7 — User Management & Subscription Center
 * ------------------------------------------------------------------ */
export interface AdminUser {
  id: string; email: string; name: string; role: string; status: string;
  avatar_url: string | null; tenant_id: number | null;
  email_verified_at: string | null; last_login_at: string | null;
  last_seen_at: string | null; mfa_method: string; created_at: string;
  permissions?: string[]; active_sessions?: number;
  failed_login_count?: number; locked_until?: string | null;
}

export interface AdminUserPage {
  total: number; page: number; page_size: number; items: AdminUser[];
}

export interface UserSession {
  session_id: string; ip_address: string | null; user_agent: string | null;
  issued_at: string; expires_at: string;
}

export interface UserSubscription {
  id: number; tenant_id: number; plan_tier: string; status: string;
  billing_period: string; period_start: string; period_end: string;
  trial_ends_at: string | null; cancel_at_period_end: boolean;
  cancelled_at: string | null; provider: string | null;
}

export interface UserInvoice {
  id: number; tenant_id: number; number: string; plan_tier: string;
  period_start: string | null; period_end: string | null;
  subtotal_paise: number; tax_paise: number; total_paise: number;
  currency: string; status: string; issued_at: string | null; paid_at: string | null;
}

export interface UserAnalytics {
  days: number; total_users: number; active_users: number; new_users: number;
  premium_users: number; free_users: number; revenue_inr: number;
  tenants: number; retention_pct: number;
}

export interface RoleInfo {
  key: string; label: string; permissions: string[];
}
