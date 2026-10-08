// ---------------------------------------------------------------------------
// Shared API response types for the Claude Code Proxy admin dashboard.
// Every shape here mirrors a backend endpoint payload exactly.
// ---------------------------------------------------------------------------

// Auth -----------------------------------------------------------------------

export interface LoginResponse {
    token: string;
    token_type: string;
}

export interface AuthProfile {
    username: string;
    permissions: string[];
    root: boolean;
}

export interface DashboardMember {
    id: string;
    username: string;
    permissions: string[];
    active: boolean;
    created_at: string;
    last_login_at: string | null;
}

export interface UserLookup {
    id: string;
    name: string;
}

// Overview stats -------------------------------------------------------------

export interface OverviewStats {
    // Pool inventory — not range-dependent.
    total_accounts: number;
    active_accounts: number;
    usable_accounts: number;
    five_hour_average_pct: number;
    weekly_average_pct: number;
    five_hour_reset_at: string | null;
    weekly_reset_at: string | null;
    pool_used_pct: number;
    pool_remaining_pct: number;
    total_users: number;
    active_users: number;
    total_keys: number;
    // Usage over the selected range (defaults to month-to-date when no range is sent).
    tokens: number; // input + output only
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
    input_output_ratio: number | null;
    cache_hit_rate: number;
    input_rate_pct: number;
    output_rate_pct: number;
    cache_hit_rate_pct: number;
    // API-equivalent value for subscription usage plus locally calculated
    // billed spend for API fallback traffic.
    api_equivalent_cost_usd: number;
    requests: number;
    average_tps: number | null;
}

// Usage ----------------------------------------------------------------------

export interface UsageRecord {
    id: string;
    user_id: string;
    user_name: string | null;
    api_key_id: string | null;
    api_key_label: string | null;
    account_id: string | null;
    account_label: string | null;
    fallback_provider_id: string | null;
    fallback_provider_label: string | null;
    model: string;
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
    // Cache-creation split by TTL bucket.
    cache_creation_5m_input_tokens: number;
    cache_creation_1h_input_tokens: number;
    duration_ms?: number | null;
    tokens_per_second?: number | null;
    // Which cache TTL this request used, if any.
    cache_ttl: "1h" | "5m" | null;
    // Thinking effort requested for this individual message.
    reasoning_level: string | null;
    // API-equivalent cost of this single request.
    cost_usd: number;
    status_code: number | null;
    created_at: string;
}

export interface UsagePage {
    total: number;
    limit: number;
    offset: number;
    items: UsageRecord[];
}

export interface ProxyEvent {
    id: string;
    created_at: string;
    request_id: string;
    user_id: string | null;
    api_key_id: string | null;
    account_id: string | null;
    fallback_provider_id: string | null;
    event_type: string;
    status_code: number | null;
    message: string | null;
    metadata: Record<string, unknown>;
    archive_hot?: boolean;
}

export interface ProxyEventPage {
    total: number;
    limit: number;
    offset: number;
    events: ProxyEvent[];
}

export interface ArchiveRequestDetail {
    event_id: string;
    request_body_path: string;
    response_body_path: string;
    request_available: boolean;
    response_available: boolean;
}

// Analytics ------------------------------------------------------------------

export interface ActivityPoint {
    ts: string; // ISO 8601 UTC bucket start
    requests: number;
    tokens: number;
}

export interface ActivityResponse {
    granularity: "hour" | "day";
    points: ActivityPoint[];
}

// A selected dashboard time window. Bounds are ISO 8601 UTC strings; an absent
// bound lets the backend apply its per-endpoint default.
export interface TimeRange {
    start?: string;
    end?: string;
    // Human label for the current selection, e.g. "Last 30 days" or a custom span.
    label: string;
}

export interface HourlyUserSlice {
    user_id: string;
    user_name: string;
    requests: number;
}

export interface HourlyUser {
    user_id: string;
    user_name: string;
}

export interface HourlyPoint {
    hour: number;
    requests: number;
    tokens: number;
    by_user: HourlyUserSlice[];
}

export interface HourlyResponse {
    points: HourlyPoint[];
    users: HourlyUser[];
}

export interface UserUsage {
    user_id: string;
    user_name: string | null;
    requests: number;
    tokens: number;
    input_tokens: number;
    output_tokens: number;
    last_used_at: string | null;
}

export interface ByUserResponse {
    users: UserUsage[];
}

// Model mix ------------------------------------------------------------------

export interface ModelSlice {
    model: string;
    requests: number;
    input_tokens: number;
    output_tokens: number;
    average_tps: number | null;
}

export interface UserModelMix {
    user_id: string;
    user_name: string | null;
    total_requests: number;
    models: ModelSlice[];
}

export interface ModelMixResponse {
    users: UserModelMix[];
}

// Thinking-level mix --------------------------------------------------------

export interface ThinkingLevelSlice {
    level: string;
    requests: number;
    input_tokens: number;
    output_tokens: number;
}

export interface UserThinkingLevelMix {
    user_id: string;
    user_name: string | null;
    total_requests: number;
    levels: ThinkingLevelSlice[];
}

export interface ThinkingLevelMixResponse {
    users: UserThinkingLevelMix[];
}

// Distributions --------------------------------------------------------------

export interface PercentileBreakdown {
    min: number;
    p25: number;
    p50: number;
    p75: number;
    p90: number;
    p95: number;
    max: number;
}

export interface DistributionResponse {
    input_tokens: PercentileBreakdown;
    output_tokens: PercentileBreakdown;
    total_requests: number;
}

// Accounts -------------------------------------------------------------------

export type AccountStatus = "ACTIVE" | "DISABLED" | "COOLDOWN";
export type ProviderHealth = "UNKNOWN" | "HEALTHY" | "DEGRADED" | "REAUTH_REQUIRED";

export interface Account {
    id: string;
    label: string;
    account_email: string | null;
    egress_target_id: string | null;
    authenticated_override: boolean;
    warmup_enabled: boolean;
    warmup_next_at: string | null;
    warmup_last_at: string | null;
    warmup_last_status: string | null;
    warmup_last_error: string | null;
    tier: string | null;
    status: AccountStatus;
    provider_health: ProviderHealth;
    provider_health_code: string | null;
    provider_health_message: string | null;
    provider_health_checked_at: string | null;
    provider_health_last_success_at: string | null;
    provider_health_failure_count: number;
    session_used_pct: number | null;
    weekly_used_pct: number | null;
    session_reset_at: string | null;
    weekly_reset_at: string | null;
    monthly_used_pct: number | null;
    monthly_reset_at: string | null;
    cooldown_until: string | null;
    last_used_at: string | null;
    created_at: string;
    // Per-window rotation policies (always set; tuned in the dashboard).
    five_hour_rotation_threshold: number;
    weekly_rotation_threshold: number;
    // Deprecated single-threshold alias.
    rotation_threshold: number;
    cooldown_seconds: number;
    max_failover_attempts: number;
    priority: number;
    total_spend_usd: number;
    monthly_spend_usd: number;
}

export interface EgressTarget {
    id: string;
    label: string;
    kind: string;
    interface_name: string | null;
    private_ip: string | null;
    public_ip: string | null;
    max_concurrency: number;
    enabled: boolean;
}

export interface OAuthStartResponse {
    authorize_url: string;
    verifier: string;
}

// Anthropic-compatible pay-as-you-go fallbacks -----------------------------

export interface AnthropicFallback {
    id: string;
    label: string;
    base_url: string;
    key_hint: string;
    status: AccountStatus;
    provider_health: ProviderHealth;
    provider_health_message: string | null;
    provider_health_checked_at: string | null;
    cooldown_until: string | null;
    priority: number;
    egress_target_id: string | null;
    monthly_spend_limit_usd: number | null;
    monthly_spend_usd: number;
    monthly_spend_remaining_usd: number | null;
    monthly_spend_reset_at: string;
    model_count: number;
    model_catalog_refreshed_at: string | null;
    last_used_at: string | null;
    created_at: string;
}

// Users + API keys -----------------------------------------------------------

export const THINKING_LEVELS = ["low", "medium", "high", "max"] as const;
export type ThinkingLevel = (typeof THINKING_LEVELS)[number];
export type ThinkingMode = "disabled" | "enabled" | "adaptive";

export interface Preset {
    fallback_enabled: boolean;
    id: string;
    name: string;
    user_count: number;
    allowed_models: string[] | null;
    allowed_thinking_levels: ThinkingLevel[];
    allowed_thinking_modes: ThinkingMode[];
    model_overrides: Record<string, string>;
    model_thinking_levels: Record<string, ThinkingLevel[]>;
    model_thinking_modes: Record<string, ThinkingMode[]>;
    allow_extended_context: boolean;
}

export interface User {
    id: string;
    name: string;
    active: boolean;
    priority: number;
    fallback_enabled: boolean;
    // Only uses quota that other users are not projected to need.
    spare_capacity_only: boolean;
    key_count: number;
    // requests/min across all the user's keys; null/0 = no user-level cap.
    rate_limit_per_minute: number | null;
    rate_limit_per_hour: number | null;
    rate_limit_per_day: number | null;
    // tokens/calendar month across all the user's keys; null/0 = unlimited.
    monthly_token_budget: number | null;
    lifetime_token_budget: number | null;
    monthly_spend_budget_usd: number | null;
    lifetime_spend_budget_usd: number | null;
    model_overrides: Record<string, string>;
    // Explicit Claude effort levels this user may request.
    allowed_thinking_levels: ThinkingLevel[];
    allowed_models: string[] | null;
    allowed_thinking_modes: ThinkingMode[];
    model_thinking_levels: Record<string, ThinkingLevel[]>;
    model_thinking_modes: Record<string, ThinkingMode[]>;
    allow_extended_context: boolean;
    preset_id: string | null;
    preset_overrides: string[];
    last_used_at: string | null;
    created_at: string;
    total_tokens: number;
    total_requests: number;
    monthly_tokens_used: number;
    monthly_reset_at: string;
    total_spend_usd: number;
    monthly_spend_usd: number;
    // Share of each provider quota window, summed across accounts (1 = one account's full window).
    // Null when no account currently has that window open.
    quota_usage: Record<QuotaWindowKey, QuotaShare | null>;
}

export type QuotaWindowKey = "five_hour" | "weekly" | "monthly";

export interface QuotaShare {
    used_pct: number;
    reset_at: string | null;
}

export interface ApiKey {
    id: string;
    user_id: string;
    label: string | null;
    key_prefix: string;
    active: boolean;
    // requests/min: null = use the server default; 0 = unlimited.
    rate_limit_per_minute: number | null;
    // tokens/calendar month: null/0 = unlimited.
    monthly_token_budget: number | null;
    last_used_at: string | null;
    created_at: string;
}

export interface ApiKeyCreated {
    api_key: ApiKey;
    // Plaintext secret — shown exactly once.
    secret: string;
}

// Notifications -------------------------------------------------------------

export interface TelegramSettings {
    enabled: boolean;
    configured: boolean;
    chat_id: string | null;
    message_thread_id: number | null;
    timezone: string;
    last_success_at: string | null;
    last_error_at: string | null;
    last_error: string | null;
}

export interface NotificationRule {
    event_type: string;
    title: string;
    description: string;
    enabled: boolean;
    template: string;
    default_template: string;
    cooldown_seconds: number;
    variables: string[];
}

export interface NotificationSettings {
    telegram: TelegramSettings;
    rules: NotificationRule[];
}
