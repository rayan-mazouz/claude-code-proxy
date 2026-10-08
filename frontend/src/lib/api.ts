// ---------------------------------------------------------------------------
// Typed fetch wrapper for the admin API.
//
//   - Prefixes every request with the configured base URL.
//   - Attaches the bearer token from localStorage when present.
//   - Sends / parses JSON and extracts the backend's `{detail}` error shape.
//   - On any 401, clears the stored token and redirects to /login.
// ---------------------------------------------------------------------------

import {
    Account,
    EgressTarget,
    ActivityResponse,
    ApiKey,
    ApiKeyCreated,
    AnthropicFallback,
    ByUserResponse,
    DistributionResponse,
    HourlyResponse,
    LoginResponse,
    ModelMixResponse,
    NotificationSettings,
    OAuthStartResponse,
    OverviewStats,
    TimeRange,
    ThinkingLevelMixResponse,
    ThinkingLevel,
    UsagePage,
    ProxyEventPage,
    ArchiveRequestDetail,
    AuthProfile,
    DashboardMember,
    UserLookup,
    User,
    Preset,
    ThinkingMode,
} from "./types";

// Serialise filter params (range bounds + optional user) to query params,
// omitting absent values. Returns a leading-"&" fragment for easy appending.
function filterParams(
    range?: Pick<TimeRange, "start" | "end">,
    userId?: string,
    model?: string,
): string {
    const p = new URLSearchParams();
    if (range?.start) p.set("start", range.start);
    if (range?.end) p.set("end", range.end);
    if (userId) p.set("user_id", userId);
    if (model) p.set("model", model);
    const s = p.toString();
    return s ? `&${s}` : "";
}

// The dashboard and backend share an origin: the dashboard is served at "/" and
// the backend at "/api". So in production we call the relative "/api" path and no
// URL needs baking in. NEXT_PUBLIC_API_BASE_URL only overrides this for local dev
// (e.g. pointing at a uvicorn instance on another port).
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "/api";

const TOKEN_KEY = "claude_proxy_admin_token";

// --- token storage ---------------------------------------------------------

export function getToken(): string | null {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
    if (typeof window === "undefined") return;
    window.localStorage.removeItem(TOKEN_KEY);
}

// --- error type ------------------------------------------------------------

export class ApiError extends Error {
    status: number;

    constructor(message: string, status: number) {
        super(message);
        this.name = "ApiError";
        this.status = status;
    }
}

// --- core request ----------------------------------------------------------

interface RequestOptions {
    method?: string;
    body?: unknown;
    // When true, a 401 will NOT trigger a redirect (used by the login call).
    skipAuthRedirect?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { method = "GET", body, skipAuthRedirect = false } = options;

    const headers: Record<string, string> = {
        "Content-Type": "application/json",
    };

    const token = getToken();
    if (token) {
        headers["Authorization"] = `Bearer ${token}`;
    }

    const res = await fetch(`${API_BASE_URL}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
    });

    // Auth failure -> clear and bounce to login.
    if (res.status === 401 && !skipAuthRedirect) {
        clearToken();
        if (typeof window !== "undefined") {
            window.location.href = "/login";
        }
        throw new ApiError("Session expired. Please sign in again.", 401);
    }

    // Try to read a JSON body (may be empty for 204 responses).
    let data: unknown = null;
    const text = await res.text();
    if (text) {
        try {
            data = JSON.parse(text);
        } catch {
            data = text;
        }
    }

    if (!res.ok) {
        const detail =
            data && typeof data === "object" && "detail" in data
                ? String((data as { detail: unknown }).detail)
                : `Request failed (${res.status})`;
        throw new ApiError(detail, res.status);
    }

    return data as T;
}

// --- typed endpoints -------------------------------------------------------

export const api = {
    // Auth
    login(username: string, password: string) {
        return request<LoginResponse>("/v1/auth/login", {
            method: "POST",
            body: { username, password },
            skipAuthRedirect: true,
        });
    },
    authProfile() {
        return request<AuthProfile>("/v1/auth/me");
    },
    async analyticsUsers(): Promise<UserLookup[]> {
        const result = await request<{ users: UserLookup[] }>("/v1/lookups/users");
        return result.users;
    },
    async analyticsModels(): Promise<string[]> {
        const result = await request<{ models: string[] }>("/v1/lookups/models");
        return result.models;
    },
    teamMembers() {
        return request<{ members: DashboardMember[] }>("/v1/team/members");
    },
    teamPermissions() {
        return request<{ permissions: string[] }>("/v1/team/permissions");
    },
    createTeamMember(input: { username: string; password: string; permissions: string[] }) {
        return request<{ member: DashboardMember }>("/v1/team/members", {
            method: "POST",
            body: input,
        });
    },
    updateTeamMember(
        id: string,
        input: { password?: string; permissions?: string[]; active?: boolean },
    ) {
        return request<{ member: DashboardMember }>(`/v1/team/members/${id}`, {
            method: "PUT",
            body: input,
        });
    },
    deleteTeamMember(id: string) {
        return request<unknown>(`/v1/team/members/${id}`, { method: "DELETE" });
    },

    // Overview + analytics. Each accepts an optional range and user filter;
    // omitting the range lets the backend apply its per-endpoint default.
    overview(range?: TimeRange, userId?: string, model?: string) {
        return request<OverviewStats>(
            `/v1/stats/overview?${filterParams(range, userId, model).slice(1)}`,
        );
    },
    usage(limit: number, offset: number, range?: TimeRange, userId?: string, model?: string) {
        return request<UsagePage>(
            `/v1/stats/usage?limit=${limit}&offset=${offset}${filterParams(range, userId, model)}`,
        );
    },
    events(
        limit = 100,
        offset = 0,
        eventType?: string,
        userId?: string,
        start?: string,
        end?: string,
        requestId?: string,
        model?: string,
    ) {
        const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
        if (eventType) params.set("event_type", eventType);
        if (userId) params.set("user_id", userId);
        if (start) params.set("start", start);
        if (end) params.set("end", end);
        if (requestId) params.set("request_id", requestId);
        if (model) params.set("model", model);
        return request<ProxyEventPage>(`/v1/events?${params.toString()}`);
    },

    archiveRequest(eventId: string) {
        return request<ArchiveRequestDetail>(`/v1/requests/${eventId}`);
    },

    async archiveBody(eventId: string, side: "request" | "response") {
        const res = await fetch(`${API_BASE_URL}/v1/requests/${eventId}/body?side=${side}`, {
            headers: { Authorization: `Bearer ${getToken()}` },
            cache: "no-store",
        });
        const text = await res.text();
        if (!res.ok) {
            let detail = `Unable to load archived ${side} body.`;
            try {
                const data = JSON.parse(text) as { detail?: unknown };
                if (data.detail) detail = String(data.detail);
            } catch {}
            throw new ApiError(detail, res.status);
        }
        return text;
    },
    activity(range?: TimeRange, userId?: string, model?: string) {
        return request<ActivityResponse>(
            `/v1/stats/activity?${filterParams(range, userId, model).slice(1)}`,
        );
    },
    hourly(range?: TimeRange, userId?: string, model?: string) {
        return request<HourlyResponse>(
            `/v1/stats/hourly?${filterParams(range, userId, model).slice(1)}`,
        );
    },
    byUser(range?: TimeRange, userId?: string, model?: string) {
        return request<ByUserResponse>(
            `/v1/stats/by-user?${filterParams(range, userId, model).slice(1)}`,
        );
    },
    distributions(range?: TimeRange, userId?: string, model?: string) {
        return request<DistributionResponse>(
            `/v1/stats/distributions?${filterParams(range, userId, model).slice(1)}`,
        );
    },
    modelMix(range?: TimeRange, userId?: string, model?: string) {
        return request<ModelMixResponse>(
            `/v1/stats/model-mix?${filterParams(range, userId, model).slice(1)}`,
        );
    },
    thinkingLevelMix(range?: TimeRange, userId?: string, model?: string) {
        return request<ThinkingLevelMixResponse>(
            `/v1/stats/thinking-level-mix?${filterParams(range, userId, model).slice(1)}`,
        );
    },

    // Accounts
    async accounts(): Promise<Account[]> {
        const res = await request<{ accounts: Account[] }>("/v1/accounts");
        return res.accounts;
    },
    async egressTargets(): Promise<EgressTarget[]> {
        const res = await request<{ targets: EgressTarget[] }>("/v1/accounts/egress-targets");
        return res.targets;
    },
    async bulkSetAccountPriority(accountIds: string[], priority: number): Promise<Account[]> {
        const res = await request<{ accounts: Account[] }>("/v1/accounts/priorities/bulk", {
            method: "PUT",
            body: { account_ids: accountIds, priority },
        });
        return res.accounts;
    },
    async reorderAccounts(accountIds: string[]): Promise<Account[]> {
        const res = await request<{ accounts: Account[] }>("/v1/accounts/priorities", {
            method: "PUT",
            body: { account_ids: accountIds },
        });
        return res.accounts;
    },
    async updateAccount(
        id: string,
        patch: {
            label?: string;
            egress_target_id?: string | null;
            authenticated_override?: boolean | null;
            warmup_enabled?: boolean | null;
            // A number sets each rotation field; null or an omitted key leaves it unchanged.
            five_hour_rotation_threshold?: number | null;
            weekly_rotation_threshold?: number | null;
            // Deprecated: sets both provider-window thresholds.
            rotation_threshold?: number | null;
            cooldown_seconds?: number | null;
            max_failover_attempts?: number | null;
            priority?: number | null;
        },
    ): Promise<Account> {
        const res = await request<{ account: Account }>(`/v1/accounts/${id}`, {
            method: "PUT",
            body: patch,
        });
        return res.account;
    },
    async refreshQuota(id: string): Promise<Account> {
        const res = await request<{ account: Account }>(`/v1/accounts/${id}/refresh-quota`, {
            method: "POST",
        });
        return res.account;
    },
    async warmupAccount(id: string): Promise<Account> {
        const res = await request<{ account: Account }>(`/v1/accounts/${id}/warmup`, {
            method: "POST",
        });
        return res.account;
    },
    async disableAccount(id: string): Promise<Account> {
        const res = await request<{ account: Account }>(`/v1/accounts/${id}/disable`, {
            method: "POST",
        });
        return res.account;
    },
    async enableAccount(id: string): Promise<Account> {
        const res = await request<{ account: Account }>(`/v1/accounts/${id}/enable`, {
            method: "POST",
        });
        return res.account;
    },
    deleteAccount(id: string) {
        return request<unknown>(`/v1/accounts/${id}`, { method: "DELETE" });
    },

    // Anthropic-compatible pay-as-you-go fallbacks. Secrets are write-only
    // and are never included in any response payload.
    async fallbacks(): Promise<AnthropicFallback[]> {
        const res = await request<{ fallbacks: AnthropicFallback[] }>("/v1/fallbacks");
        return res.fallbacks;
    },
    async createFallback(input: {
        label: string;
        base_url: string;
        api_key: string;
        monthly_spend_limit_usd: number | null;
        priority: number;
        egress_target_id?: string | null;
    }): Promise<AnthropicFallback> {
        const res = await request<{ fallback: AnthropicFallback }>("/v1/fallbacks", {
            method: "POST",
            body: input,
        });
        return res.fallback;
    },
    async updateFallback(
        id: string,
        patch: {
            label?: string;
            base_url?: string;
            api_key?: string;
            monthly_spend_limit_usd?: number;
            clear_monthly_spend_limit?: boolean;
            priority?: number;
            egress_target_id?: string | null;
        },
    ): Promise<AnthropicFallback> {
        const res = await request<{ fallback: AnthropicFallback }>(`/v1/fallbacks/${id}`, {
            method: "PUT",
            body: patch,
        });
        return res.fallback;
    },
    async testFallback(id: string): Promise<AnthropicFallback> {
        const res = await request<{ fallback: AnthropicFallback }>(`/v1/fallbacks/${id}/test`, {
            method: "POST",
        });
        return res.fallback;
    },
    async enableFallback(id: string): Promise<AnthropicFallback> {
        const res = await request<{ fallback: AnthropicFallback }>(`/v1/fallbacks/${id}/enable`, {
            method: "POST",
        });
        return res.fallback;
    },
    async disableFallback(id: string): Promise<AnthropicFallback> {
        const res = await request<{ fallback: AnthropicFallback }>(`/v1/fallbacks/${id}/disable`, {
            method: "POST",
        });
        return res.fallback;
    },
    deleteFallback(id: string) {
        return request<unknown>(`/v1/fallbacks/${id}`, { method: "DELETE" });
    },
    oauthStart() {
        return request<OAuthStartResponse>("/v1/accounts/oauth/start", { method: "POST" });
    },
    async oauthComplete(label: string, code: string, verifier: string): Promise<Account> {
        const res = await request<{ account: Account }>("/v1/accounts/oauth/complete", {
            method: "POST",
            body: { label, code, verifier },
        });
        return res.account;
    },
    // Re-authenticate an existing account in place. Pairs with oauthStart, the
    // same start call the add-account flow uses.
    async reauthAccount(id: string, body: { code: string; verifier: string }): Promise<Account> {
        const res = await request<{ account: Account }>(`/v1/accounts/${id}/oauth/complete`, {
            method: "POST",
            body,
        });
        return res.account;
    },

    // Notifications
    notificationSettings() {
        return request<NotificationSettings>("/v1/notifications");
    },
    updateTelegram(settings: {
        enabled: boolean;
        bot_token?: string;
        chat_id?: string;
        message_thread_id?: number | null;
        timezone?: string;
    }) {
        return request<NotificationSettings>("/v1/notifications/telegram", {
            method: "PUT",
            body: settings,
        });
    },
    updateNotificationRule(
        eventType: string,
        rule: { enabled: boolean; template: string; cooldown_seconds: number },
    ) {
        return request<NotificationSettings>(`/v1/notifications/rules/${eventType}`, {
            method: "PUT",
            body: rule,
        });
    },
    testTelegram() {
        return request<{ delivered: boolean; detail: string }>("/v1/notifications/telegram/test", {
            method: "POST",
        });
    },

    // Users
    async users(): Promise<User[]> {
        const res = await request<{ users: User[]; total: number }>("/v1/users?limit=100");
        return res.users;
    },
    async bulkSetUserPriority(userIds: string[], priority: number): Promise<User[]> {
        const res = await request<{ users: User[]; total: number }>("/v1/users/priorities/bulk", {
            method: "PUT",
            body: { user_ids: userIds, priority },
        });
        return res.users;
    },
    async userModelOptions(): Promise<string[]> {
        const res = await request<{ models: string[] }>("/v1/users/model-options");
        return res.models;
    },
    async presets(): Promise<Preset[]> {
        const res = await request<{ presets: Preset[] }>("/v1/presets");
        return res.presets;
    },
    createPreset(payload: Omit<Preset, "id" | "user_count">): Promise<Preset> {
        return request<Preset>("/v1/presets", { method: "POST", body: payload });
    },
    updatePreset(id: string, payload: Omit<Preset, "id" | "user_count">): Promise<Preset> {
        return request<Preset>(`/v1/presets/${id}`, { method: "PUT", body: payload });
    },
    deletePreset(id: string) {
        return request<unknown>(`/v1/presets/${id}`, { method: "DELETE" });
    },
    async assignUserPreset(userId: string, presetId: string): Promise<User> {
        const res = await request<{ user: User }>(`/v1/users/${userId}/preset`, {
            method: "PUT",
            body: { preset_id: presetId },
        });
        return res.user;
    },
    async clearUserPresetOverride(userId: string, field: string): Promise<User> {
        const res = await request<{ user: User }>(`/v1/users/${userId}/preset-overrides/${field}`, {
            method: "DELETE",
        });
        return res.user;
    },
    async createUser(
        name: string,
        opts: {
            priority?: number;
            fallback_enabled?: boolean;
            spare_capacity_only?: boolean;
            rate_limit_per_minute?: number | null;
            rate_limit_per_hour?: number | null;
            rate_limit_per_day?: number | null;
            monthly_token_budget?: number | null;
            lifetime_token_budget?: number | null;
            monthly_spend_budget_usd?: number | null;
            lifetime_spend_budget_usd?: number | null;
            model_overrides?: Record<string, string>;
            allowed_thinking_levels?: ThinkingLevel[];
            preset_id?: string | null;
            allowed_models?: string[] | null;
            allowed_thinking_modes?: ThinkingMode[];
            model_thinking_levels?: Record<string, ThinkingLevel[]>;
            model_thinking_modes?: Record<string, ThinkingMode[]>;
            allow_extended_context?: boolean;
        } = {},
    ): Promise<User> {
        const res = await request<{ user: User }>("/v1/users", {
            method: "POST",
            body: {
                name,
                priority: opts.priority ?? 1,
                fallback_enabled: opts.fallback_enabled ?? false,
                spare_capacity_only: opts.spare_capacity_only ?? false,
                rate_limit_per_minute: opts.rate_limit_per_minute ?? null,
                rate_limit_per_hour: opts.rate_limit_per_hour ?? null,
                rate_limit_per_day: opts.rate_limit_per_day ?? null,
                monthly_token_budget: opts.monthly_token_budget ?? null,
                lifetime_token_budget: opts.lifetime_token_budget ?? null,
                monthly_spend_budget_usd: opts.monthly_spend_budget_usd ?? null,
                lifetime_spend_budget_usd: opts.lifetime_spend_budget_usd ?? null,
                model_overrides: opts.model_overrides ?? {},
                allowed_thinking_levels: opts.allowed_thinking_levels,
                preset_id: opts.preset_id,
                allowed_models: opts.allowed_models,
                allowed_thinking_modes: opts.allowed_thinking_modes,
                model_thinking_levels: opts.model_thinking_levels,
                model_thinking_modes: opts.model_thinking_modes,
                allow_extended_context: opts.allow_extended_context,
            },
        });
        return res.user;
    },
    async updateUser(
        id: string,
        patch: {
            name?: string;
            preset_id?: string | null;
            clear_preset_overrides?: string[];
            active?: boolean;
            priority?: number;
            fallback_enabled?: boolean;
            spare_capacity_only?: boolean;
            rate_limit_per_minute?: number;
            rate_limit_per_hour?: number;
            rate_limit_per_day?: number;
            monthly_token_budget?: number;
            lifetime_token_budget?: number;
            monthly_spend_budget_usd?: number;
            lifetime_spend_budget_usd?: number;
            model_overrides?: Record<string, string>;
            allowed_thinking_levels?: ThinkingLevel[];
            allowed_models?: string[] | null;
            allowed_thinking_modes?: ThinkingMode[];
            model_thinking_levels?: Record<string, ThinkingLevel[]>;
            model_thinking_modes?: Record<string, ThinkingMode[]>;
            allow_extended_context?: boolean;
        },
    ): Promise<User> {
        const res = await request<{ user: User }>(`/v1/users/${id}`, {
            method: "PUT",
            body: patch,
        });
        return res.user;
    },
    deleteUser(id: string) {
        return request<unknown>(`/v1/users/${id}`, { method: "DELETE" });
    },

    // API keys (a user can hold several)
    async keys(userId: string): Promise<ApiKey[]> {
        const res = await request<{ keys: ApiKey[] }>(`/v1/users/${userId}/keys`);
        return res.keys;
    },
    createKey(
        userId: string,
        opts: {
            label?: string;
            rate_limit_per_minute?: number | null;
            monthly_token_budget?: number | null;
        } = {},
    ) {
        return request<ApiKeyCreated>(`/v1/users/${userId}/keys`, {
            method: "POST",
            body: {
                label: opts.label && opts.label.trim() !== "" ? opts.label.trim() : null,
                rate_limit_per_minute: opts.rate_limit_per_minute ?? null,
                monthly_token_budget: opts.monthly_token_budget ?? null,
            },
        });
    },
    async updateKey(
        userId: string,
        keyId: string,
        patch: {
            label?: string;
            active?: boolean;
            rate_limit_per_minute?: number;
            monthly_token_budget?: number;
        },
    ): Promise<ApiKey> {
        const res = await request<{ api_key: ApiKey }>(`/v1/users/${userId}/keys/${keyId}`, {
            method: "PUT",
            body: patch,
        });
        return res.api_key;
    },
    deleteKey(userId: string, keyId: string) {
        return request<unknown>(`/v1/users/${userId}/keys/${keyId}`, { method: "DELETE" });
    },
};
