"use client";

// ---------------------------------------------------------------------------
// Overview / analytics. A date range drives the whole page:
//   - Hero band: API-equivalent value delivered + tokens/requests, with the
//     activity series (auto hourly/daily) pulled in alongside.
//   - Infra strip: pool load and account inventory (not range-dependent).
//   - Quota averages, user analytics, request analytics and peak requests.
//   - Two-up: requests by model + per-request token distribution.
// Auto-refresh and the range selection both re-fetch every panel.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Cpu } from "lucide-react";
import { api } from "@/lib/api";
import {
    DistributionResponse,
    HourlyResponse,
    ModelMixResponse,
    OverviewStats,
    PercentileBreakdown,
    TimeRange,
    ThinkingLevelMixResponse,
    UserLookup,
} from "@/lib/types";
import { formatCountdown, formatNumber, formatTokens, localTzAbbr } from "@/lib/format";
import { Button, Card, ErrorState, LoadingState, StatusToggle, TextInput } from "@/components/ui";
import {
    DualAreaChart,
    DualSeries,
    STACK_COLORS,
    StackedBarChart,
    StackSeries,
} from "@/components/Charts";
import { RequestAnalyticsCard } from "@/components/RequestAnalyticsCard";
import { RangePicker, resolveRange } from "@/components/RangePicker";
import { useResource } from "@/lib/useResource";
import { useStoredState } from "@/lib/useStoredState";
import { UserFilter } from "@/components/UserFilter";
import { EventTypeFilter } from "@/components/EventTypeFilter";

const AUTO_REFRESH_KEY = "dashboard_auto_refresh";
const REFRESH_SECS_KEY = "dashboard_refresh_secs";

// USD with no cents — the hero figure reads as a headline, not an invoice.
function formatUsd0(value: number): string {
    return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 0,
    }).format(value);
}

// Default window: the current local calendar month, applied explicitly so every panel shares it.
function thisMonth(): TimeRange {
    const end = new Date();
    const start = new Date(end.getFullYear(), end.getMonth(), 1);
    return { start: start.toISOString(), end: end.toISOString(), label: "This month" };
}

export default function OverviewPage() {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const queryString = searchParams.toString();
    const query = useMemo(() => {
        const params = new URLSearchParams(queryString);
        const fallback = thisMonth();
        const rangeLabel = params.get("range");
        return {
            range: {
                start:
                    params.get("start") || (rangeLabel === "All time" ? undefined : fallback.start),
                end: params.get("end") || fallback.end,
                label: rangeLabel || fallback.label,
            } satisfies TimeRange,
            userId: params.get("user") || null,
            model: params.get("model") || "",
        };
    }, [queryString]);
    // The URL is the single source of truth for the filters, so links and the
    // back button restore exactly what was on screen.
    const range = query.range;
    const filterUserId = query.userId;
    const filterModel = query.model;

    const updateUrl = useCallback(
        (updates: Record<string, string | null | undefined>) => {
            const params = new URLSearchParams(queryString);
            Object.entries(updates).forEach(([key, value]) => {
                if (value === null || value === undefined || value === "") params.delete(key);
                else params.set(key, value);
            });
            const nextQueryString = params.toString();
            router.replace(nextQueryString ? `${pathname}?${nextQueryString}` : pathname, {
                scroll: false,
            });
        },
        [pathname, queryString, router],
    );

    // --- analytics ----------------------------------------------------------------
    const filterKey = JSON.stringify([
        range.start ?? null,
        range.end ?? null,
        range.label,
        filterUserId,
        filterModel,
    ]);
    // One round trip returns every panel plus the filter options for this scope.
    const dashboard = useResource(
        () =>
            api.dashboard(resolveRange(range), filterUserId ?? undefined, filterModel || undefined),
        filterKey,
    );
    const stats = dashboard.data?.overview ?? null;
    const activity = dashboard.data?.activity ?? null;
    const hourly = dashboard.data?.hourly ?? null;
    const distro = dashboard.data?.distributions ?? null;
    const modelMix = dashboard.data?.model_mix ?? null;
    const thinkingLevelMix = dashboard.data?.thinking_level_mix ?? null;
    const allUsers: UserLookup[] = dashboard.data?.lookups.users ?? [];
    const modelOptions: string[] = dashboard.data?.lookups.models ?? [];
    const analyticsError = dashboard.error;
    const analyticsLoading = dashboard.loading;
    const refreshing = dashboard.loading;
    const refreshAll = dashboard.reload;

    // --- auto-refresh --------------------------------------------------------
    const [autoRefresh, setAutoRefresh] = useStoredState(
        AUTO_REFRESH_KEY,
        false,
        (raw) => raw === "1",
        (on) => (on ? "1" : "0"),
    );
    const [refreshSecs, setRefreshSecs] = useStoredState(REFRESH_SECS_KEY, 10, (raw) => {
        const secs = Number(raw);
        return Number.isFinite(secs) && secs > 0 ? Math.floor(secs) : undefined;
    });

    useEffect(() => {
        if (!autoRefresh) return;
        const id = setInterval(refreshAll, Math.max(1, refreshSecs) * 1000);
        return () => clearInterval(id);
    }, [autoRefresh, refreshSecs, refreshAll]);

    // Activity series, labelled by the granularity the backend chose.
    const granularity = activity?.granularity ?? "day";
    const activityData = useMemo(
        () =>
            (activity?.points ?? []).map((p) => {
                const d = new Date(p.ts);
                const axis =
                    granularity === "hour"
                        ? `${String(d.getHours()).padStart(2, "0")}:00`
                        : `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
                const label =
                    granularity === "hour"
                        ? new Intl.DateTimeFormat("en-US", {
                              month: "short",
                              day: "numeric",
                              hour: "2-digit",
                              minute: "2-digit",
                              hour12: true,
                          }).format(d)
                        : new Intl.DateTimeFormat("en-US", {
                              month: "short",
                              day: "numeric",
                          }).format(d);
                return { label, axis, requests: p.requests, tokens: p.tokens };
            }),
        [activity, granularity],
    );
    const activityDualSeries: [DualSeries, DualSeries] = [
        {
            key: "requests",
            label: "Requests",
            tone: "coral",
            format: (v) => `${formatNumber(v)} req`,
        },
        { key: "tokens", label: "Tokens", tone: "amber", format: (v) => `${formatTokens(v)}` },
    ];
    const hourlyUsers = hourly?.users ?? [];
    const hourlyUserTotals = new Map<string, number>();
    for (const point of hourly?.points ?? []) {
        for (const slice of point.by_user) {
            hourlyUserTotals.set(
                slice.user_id,
                (hourlyUserTotals.get(slice.user_id) ?? 0) + slice.requests,
            );
        }
    }
    const topHourlyUsers = [...hourlyUsers]
        .sort(
            (a, b) =>
                (hourlyUserTotals.get(b.user_id) ?? 0) - (hourlyUserTotals.get(a.user_id) ?? 0),
        )
        .slice(0, 5);
    const topHourlyUserIds = new Set(topHourlyUsers.map((user) => user.user_id));
    const hasOtherHourlyUsers = hourlyUsers.length > topHourlyUsers.length;
    const hourlyStackSeries: StackSeries[] = topHourlyUsers.map((u, i) => ({
        key: u.user_id,
        label: u.user_name,
        color: STACK_COLORS[i % STACK_COLORS.length],
    }));
    if (hasOtherHourlyUsers) {
        hourlyStackSeries.push({
            key: "other-users",
            label: "Other users",
            color: "var(--color-fog-400)",
        });
    }
    const tz = localTzAbbr();
    const hourlyStackData = (hourly?.points ?? [])
        .map((h) => {
            const ref = new Date();
            ref.setUTCHours(h.hour, 0, 0, 0);
            const lh = ref.getHours();
            const lm = ref.getMinutes();
            const timeStr = `${String(lh).padStart(2, "0")}:${String(lm).padStart(2, "0")}`;
            const row: Record<string, string | number> & { label: string; axis: string } = {
                label: `${timeStr} ${tz}`,
                axis: lm === 0 ? String(lh) : timeStr,
            };
            for (const slice of h.by_user) {
                if (topHourlyUserIds.has(slice.user_id)) {
                    row[slice.user_id] = slice.requests;
                } else if (hasOtherHourlyUsers) {
                    row["other-users"] = Number(row["other-users"] ?? 0) + slice.requests;
                }
            }
            return { row, sortKey: lh * 60 + lm };
        })
        .sort((a, b) => a.sortKey - b.sortKey)
        .map(({ row }) => row);

    return (
        <div className="min-w-0 space-y-5">
            {/* Header */}
            <header className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-fog-100 text-2xl font-semibold tracking-tight">Overview</h1>
                    <p className="text-fog-400 mt-0.5 text-sm">
                        What your subscription pool delivered, and how it&apos;s being used.
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-3">
                    {autoRefresh ? (
                        <span className="text-good-500 inline-flex items-center gap-1.5 text-xs font-medium tracking-wider uppercase">
                            <span className="bg-good-500 h-1.5 w-1.5 animate-pulse rounded-full" />
                            Live
                        </span>
                    ) : null}
                    <div className="border-ink-700 bg-ink-900 flex h-9 items-center gap-2 rounded-lg border px-3">
                        <StatusToggle
                            on={autoRefresh}
                            onClick={() => setAutoRefresh(!autoRefresh)}
                            onLabel="Auto"
                            offLabel="Auto"
                            srLabel={autoRefresh ? "Disable auto-refresh" : "Enable auto-refresh"}
                        />
                        <div className="w-14">
                            <TextInput
                                type="number"
                                min={1}
                                value={String(refreshSecs)}
                                disabled={!autoRefresh}
                                aria-label="Auto-refresh interval in seconds"
                                onChange={(e) => {
                                    const n = Number(e.target.value);
                                    if (Number.isFinite(n) && n > 0) setRefreshSecs(Math.floor(n));
                                }}
                                className="px-2 py-0.5 text-center disabled:opacity-50"
                            />
                        </div>
                        <span className="text-fog-400 text-xs">sec</span>
                    </div>
                    <Button
                        variant="ghost"
                        className="h-9"
                        onClick={refreshAll}
                        disabled={refreshing}
                    >
                        {refreshing ? "Refreshing…" : "Refresh"}
                    </Button>
                    <RangePicker
                        value={range}
                        onChange={(next) => {
                            updateUrl({
                                start: next.start,
                                end: next.end,
                                range: next.label,
                            });
                        }}
                    />
                    <UserFilter
                        users={allUsers}
                        value={filterUserId}
                        onChange={(next) => {
                            updateUrl({ user: next });
                        }}
                    />
                    <EventTypeFilter
                        value={filterModel}
                        options={modelOptions}
                        onChange={(next) => {
                            updateUrl({ model: next });
                        }}
                        label="Model"
                        allLabel="All models"
                        idPrefix="overview-model-options"
                        showArrow={false}
                        compact
                        icon={Cpu}
                    />
                </div>
            </header>

            {analyticsLoading && !stats ? (
                <Card>
                    <LoadingState />
                </Card>
            ) : analyticsError && !stats ? (
                <Card>
                    <ErrorState message={analyticsError} onRetry={refreshAll} />
                </Card>
            ) : stats ? (
                <>
                    {/* Hero: value + activity */}
                    <Card className="grid grid-cols-1 overflow-hidden lg:grid-cols-[minmax(260px,0.82fr)_1.18fr]">
                        <div className="flex flex-col p-6 sm:p-7">
                            <div className="text-fog-400 text-xs font-medium tracking-wider uppercase">
                                {range.label}
                            </div>
                            <div
                                className="text-fog-100 mt-3 text-5xl leading-none font-semibold tracking-tight tabular-nums"
                                style={{ fontVariantNumeric: "oldstyle-nums tabular-nums" }}
                            >
                                {formatUsd0(stats.api_equivalent_cost_usd)}
                            </div>
                            <div className="text-fog-300 mt-3.5 text-sm font-medium">
                                API-equivalent value delivered
                            </div>
                            <div className="mt-4 flex gap-6">
                                <div>
                                    <div className="text-fog-100 text-lg tabular-nums">
                                        {formatTokens(stats.tokens)}
                                    </div>
                                    <div className="text-fog-400 mt-1 text-[10px] tracking-wider uppercase">
                                        Tokens
                                    </div>
                                </div>
                                <div>
                                    <div className="text-fog-100 text-lg tabular-nums">
                                        {formatNumber(stats.requests)}
                                    </div>
                                    <div className="text-fog-400 mt-1 text-[10px] tracking-wider uppercase">
                                        Requests
                                    </div>
                                </div>
                            </div>
                            <p className="border-brand-500/55 text-fog-400 mt-auto border-l-2 pt-4 pl-3 text-xs">
                                Flat-rate subscriptions, measured at list API prices — value
                                delivered, not money owed.
                            </p>
                        </div>
                        <div className="border-ink-700 border-t p-5 lg:border-t-0 lg:border-l">
                            <div className="mb-2 flex items-center justify-between gap-2">
                                <span className="text-fog-400 text-xs">
                                    Activity per {granularity}
                                </span>
                                <div className="flex gap-3">
                                    {activityDualSeries.map((s) => (
                                        <span
                                            key={s.key}
                                            className="text-fog-300 flex items-center gap-1.5 text-xs"
                                        >
                                            <span
                                                className="h-2 w-2 shrink-0 rounded-full"
                                                style={{
                                                    backgroundColor: `var(--color-chart-${s.tone === "coral" ? 1 : 2})`,
                                                }}
                                            />
                                            {s.label}
                                        </span>
                                    ))}
                                </div>
                            </div>
                            {activity ? (
                                <DualAreaChart
                                    data={activityData}
                                    series={activityDualSeries}
                                    height={210}
                                />
                            ) : (
                                <LoadingState />
                            )}
                        </div>
                    </Card>

                    <SystemTokenFlow stats={stats} />

                    <QuotaAverages stats={stats} />

                    {modelMix && modelMix.users.some((user) => user.total_requests > 0) ? (
                        <UserInsightsCard modelData={modelMix} thinkingData={thinkingLevelMix} />
                    ) : null}

                    {activity ? <RequestAnalyticsCard activity={activity} /> : null}

                    {stats.requests > 0 ? (
                        <PeakHoursCard
                            hourly={hourly}
                            data={hourlyStackData}
                            series={hourlyStackSeries}
                            timezone={tz}
                        />
                    ) : null}

                    {/* Independent columns avoid stretching short cards to match data-heavy neighbours. */}
                    {stats.requests > 0 ? (
                        <div className="grid min-w-0 grid-cols-1 items-start gap-4 lg:grid-cols-2">
                            <div className="min-w-0 space-y-4">
                                {modelMix &&
                                modelMix.users.some((user) => user.total_requests > 0) ? (
                                    <RequestsByModelCard data={modelMix} />
                                ) : null}
                            </div>
                            <div className="min-w-0 space-y-4">
                                {distro && distro.total_requests > 0 ? (
                                    <DistributionCard distro={distro} />
                                ) : null}
                            </div>
                        </div>
                    ) : null}
                </>
            ) : null}
        </div>
    );
}

function SystemTokenFlow({ stats }: { stats: OverviewStats }) {
    const inputRate = Math.max(0, Math.min(100, stats.input_rate_pct));
    const outputRate = Math.max(0, Math.min(100, stats.output_rate_pct));
    const cacheHitRate = Math.max(0, Math.min(100, stats.cache_hit_rate_pct));

    return (
        <Card className="grid grid-cols-1 overflow-hidden md:grid-cols-2">
            <div className="p-5 sm:p-6">
                <div className="flex items-center justify-between gap-3">
                    <h3 className="text-fog-100 text-sm font-semibold">Input vs output rate</h3>
                </div>
                <div className="mt-4 grid grid-cols-2 gap-4">
                    <div>
                        <div className="text-fog-100 text-4xl font-semibold tabular-nums">
                            {inputRate.toFixed(2)}%
                        </div>
                        <div className="text-fog-400 mt-1 text-xs">of traffic is input</div>
                    </div>
                    <div>
                        <div className="text-fog-100 text-4xl font-semibold tabular-nums">
                            {outputRate.toFixed(2)}%
                        </div>
                        <div className="text-fog-400 mt-1 text-xs">of traffic is output</div>
                    </div>
                </div>
                <div
                    className="bg-ink-700 mt-5 flex h-2 overflow-hidden rounded-full"
                    role="img"
                    aria-label={`${formatTokens(stats.input_tokens)} input tokens and ${formatTokens(stats.output_tokens)} output tokens`}
                >
                    <span
                        className="h-full bg-[var(--color-chart-1)] transition-[width] duration-500"
                        style={{ width: `${inputRate}%` }}
                    />
                    <span
                        className="h-full bg-[var(--color-chart-2)] transition-[width] duration-500"
                        style={{ width: `${outputRate}%` }}
                    />
                </div>
                <div className="mt-3 grid grid-cols-2 gap-3 text-xs tabular-nums">
                    <span className="text-fog-300">
                        <i className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-[var(--color-chart-1)]" />
                        {formatTokens(stats.input_tokens)} input
                    </span>
                    <span className="text-fog-300 text-right">
                        <i className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-[var(--color-chart-2)]" />
                        {formatTokens(stats.output_tokens)} output
                    </span>
                </div>
            </div>

            <div className="border-ink-700 border-t p-5 sm:p-6 md:border-t-0 md:border-l">
                <h3 className="text-fog-100 text-sm font-semibold">Input cache hit rate</h3>
                <div className="mt-4 flex items-baseline justify-between gap-4">
                    <div className="text-fog-100 text-4xl font-semibold tabular-nums">
                        {cacheHitRate.toFixed(1)}%
                    </div>
                    <div className="text-fog-400 text-right text-xs">
                        {formatTokens(stats.cached_input_tokens)} cached of{" "}
                        {formatTokens(stats.input_tokens)} input tokens
                    </div>
                </div>
                <div
                    className="bg-ink-700 mt-5 h-2 overflow-hidden rounded-full"
                    role="progressbar"
                    aria-label="Input cache hit rate"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(cacheHitRate)}
                >
                    <div
                        className="bg-brand-500 h-full rounded-full transition-[width] duration-500"
                        style={{ width: `${cacheHitRate}%` }}
                    />
                </div>
            </div>
        </Card>
    );
}

function QuotaAverages({ stats }: { stats: OverviewStats }) {
    const accountLabel = `${formatNumber(stats.active_accounts)} / ${formatNumber(stats.total_accounts)} accounts`;
    return (
        <Card className="grid grid-cols-1 overflow-hidden md:grid-cols-2">
            <QuotaAverageCell
                label="Average 5-hour limit"
                usedPct={stats.five_hour_average_pct}
                resetAt={stats.five_hour_reset_at}
                accountLabel={accountLabel}
            />
            <QuotaAverageCell
                label="Average weekly limit"
                usedPct={stats.weekly_average_pct}
                resetAt={stats.weekly_reset_at}
                accountLabel={accountLabel}
                divided
            />
        </Card>
    );
}

function QuotaAverageCell({
    label,
    usedPct,
    resetAt,
    accountLabel,
    divided = false,
}: {
    label: string;
    usedPct: number;
    resetAt: string | null;
    accountLabel: string;
    divided?: boolean;
}) {
    const percentage = Math.max(0, Math.min(100, usedPct));
    return (
        <div
            className={`p-5 sm:p-6 ${divided ? "border-ink-700 border-t md:border-t-0 md:border-l" : ""}`}
        >
            <h3 className="text-fog-100 text-sm font-semibold">{label}</h3>
            <div className="text-fog-100 mt-4 text-4xl font-semibold tabular-nums">
                {percentage.toFixed(1)}%
            </div>
            <div className="text-fog-400 mt-1 text-xs">average used</div>
            <div className="text-fog-400 mt-2 text-xs">
                {resetAt ? formatCountdown(resetAt) : "reset time unavailable"}
            </div>
            <div
                className="bg-ink-700 mt-5 h-2 overflow-hidden rounded-full"
                role="progressbar"
                aria-label={`${label} average used`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={percentage}
            >
                <div
                    className="bg-brand-500 h-full rounded-full transition-[width] duration-500"
                    style={{ width: `${percentage}%` }}
                />
            </div>
            <div className="text-fog-400 mt-2 text-xs">
                {accountLabel} authenticated and enabled
            </div>
        </div>
    );
}

function PeakHoursCard({
    hourly,
    data,
    series,
    timezone,
}: {
    hourly: HourlyResponse | null;
    data: Array<Record<string, string | number> & { label: string; axis: string }>;
    series: StackSeries[];
    timezone: string;
}) {
    return (
        <Card className="p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h3 className="text-fog-100 text-sm font-semibold">Peak requests</h3>
                    <p className="text-fog-400 mt-0.5 text-xs">Requests by hour · {timezone}</p>
                </div>
            </div>
            <div className="mt-3">
                {hourly ? (
                    series.length > 0 ? (
                        <StackedBarChart
                            data={data}
                            series={series}
                            axisInterval={2}
                            format={(value) => `${formatNumber(value)} req`}
                            height={240}
                        />
                    ) : (
                        <div className="text-fog-400 flex h-44 items-center justify-center text-sm">
                            No usage in this range.
                        </div>
                    )
                ) : (
                    <LoadingState />
                )}
            </div>
            {series.length > 0 ? (
                <div className="border-ink-700 mt-3 flex max-h-16 flex-wrap gap-x-3 gap-y-1.5 overflow-y-auto border-t pt-3">
                    {series.map((item) => (
                        <span
                            key={item.key}
                            className="text-fog-300 flex items-center gap-1.5 text-xs"
                        >
                            <span
                                className="h-2.5 w-2.5 shrink-0 rounded-sm"
                                style={{ backgroundColor: item.color }}
                            />
                            {item.label}
                        </span>
                    ))}
                </div>
            ) : null}
        </Card>
    );
}

const MODEL_COLORS = [
    "var(--color-chart-1)",
    "var(--color-chart-2)",
    "var(--color-chart-3)",
    "var(--color-chart-4)",
    "#7aabcf",
    "#c97b8b",
    "#8bc9a3",
    "#d4a76a",
];

function modelColor(index: number): string {
    return MODEL_COLORS[index % MODEL_COLORS.length];
}

function shortModel(model: string): string {
    return model.replace(/^claude-/, "").replace(/-20\d{6}$/, "");
}

type UserInsightView = "tokens" | "models" | "thinking";

function UserInsightsCard({
    modelData,
    thinkingData,
}: {
    modelData: ModelMixResponse;
    thinkingData: ThinkingLevelMixResponse | null;
}) {
    const [view, setView] = useState<UserInsightView>("models");
    const users = modelData.users.filter((user) => user.total_requests > 0);
    const allModels = Array.from(
        new Set(users.flatMap((user) => user.models.map((model) => model.model))),
    );
    const allLevels = thinkingData ? orderedThinkingLevels(thinkingData) : [];
    const thinkingByUser = new Map((thinkingData?.users ?? []).map((user) => [user.user_id, user]));
    const thinkingTotals = new Map<string, number>();
    for (const user of thinkingData?.users ?? []) {
        for (const level of user.levels) {
            thinkingTotals.set(
                level.level,
                (thinkingTotals.get(level.level) ?? 0) + level.requests,
            );
        }
    }

    return (
        <Card className="overflow-hidden">
            <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-end sm:justify-between">
                <div>
                    <h3 className="text-fog-100 text-sm font-semibold">User analytics</h3>
                    <div className="text-fog-500 mt-1 font-mono text-[11px]">
                        {formatNumber(users.length)} active users
                    </div>
                </div>
                <div
                    className="border-ink-700 bg-ink-900 grid grid-cols-3 rounded-lg border p-1"
                    role="tablist"
                    aria-label="User analytics view"
                >
                    {(["models", "tokens", "thinking"] as const).map((tab) => (
                        <button
                            key={tab}
                            type="button"
                            role="tab"
                            aria-selected={view === tab}
                            onClick={() => setView(tab)}
                            className={`rounded-md px-3 py-1.5 text-xs font-medium capitalize transition-colors ${view === tab ? "bg-ink-700 text-fog-100" : "text-fog-400 hover:text-fog-200"}`}
                        >
                            {tab}
                        </button>
                    ))}
                </div>
            </div>

            <UserInsightLegend
                view={view}
                models={allModels}
                levels={allLevels}
                thinkingTotals={thinkingTotals}
            />

            <div className="border-ink-700 max-h-[390px] overflow-y-auto overscroll-contain border-t">
                {users.length > 0 ? (
                    users.map((user) => {
                        const input = user.models.reduce(
                            (sum, model) => sum + model.input_tokens,
                            0,
                        );
                        const output = user.models.reduce(
                            (sum, model) => sum + model.output_tokens,
                            0,
                        );
                        const totalTokens = input + output;
                        const thinking = thinkingByUser.get(user.user_id);
                        return (
                            <div
                                key={user.user_id}
                                className="border-ink-800 grid gap-3 border-b px-5 py-3.5 last:border-0 sm:grid-cols-[minmax(130px,0.32fr)_1fr_110px] sm:items-center"
                            >
                                <div className="min-w-0">
                                    <div className="text-fog-100 truncate text-sm font-semibold">
                                        {user.user_name ?? "—"}
                                    </div>
                                    <div className="text-fog-500 mt-0.5 text-[10px] tabular-nums">
                                        {formatNumber(user.total_requests)} requests
                                    </div>
                                </div>

                                {view === "tokens" ? (
                                    <SplitTokenBar input={input} output={output} />
                                ) : view === "models" ? (
                                    <SegmentBar
                                        items={user.models.map((model) => ({
                                            key: model.model,
                                            label: shortModel(model.model),
                                            value: model.requests,
                                            color: modelColor(allModels.indexOf(model.model)),
                                        }))}
                                        total={user.total_requests}
                                    />
                                ) : thinking && thinking.total_requests > 0 ? (
                                    <SegmentBar
                                        items={thinking.levels.map((level) => ({
                                            key: level.level,
                                            label: thinkingLevelLabel(level.level),
                                            value: level.requests,
                                            color: modelColor(allLevels.indexOf(level.level)),
                                        }))}
                                        total={thinking.total_requests}
                                    />
                                ) : (
                                    <div className="text-fog-500 text-xs">No thinking data</div>
                                )}

                                <div className="text-left sm:text-right">
                                    <div className="text-fog-100 text-sm tabular-nums">
                                        {view === "tokens"
                                            ? formatTokens(totalTokens)
                                            : formatNumber(user.total_requests)}
                                    </div>
                                    <div className="text-fog-500 mt-0.5 text-[10px] tracking-wide uppercase">
                                        {view === "tokens" ? "total tokens" : "requests"}
                                    </div>
                                </div>
                            </div>
                        );
                    })
                ) : (
                    <div className="text-fog-400 flex h-44 items-center justify-center text-sm">
                        No user activity in this range.
                    </div>
                )}
            </div>
        </Card>
    );
}

function UserInsightLegend({
    view,
    models,
    levels,
    thinkingTotals,
}: {
    view: UserInsightView;
    models: string[];
    levels: string[];
    thinkingTotals: Map<string, number>;
}) {
    const totalThinkingRequests = Array.from(thinkingTotals.values()).reduce(
        (sum, value) => sum + value,
        0,
    );
    const items: Array<{ key: string; label: string; color: string; meta?: string }> =
        view === "tokens"
            ? [
                  { key: "input", label: "Input tokens", color: "var(--color-chart-1)" },
                  { key: "output", label: "Output tokens", color: "var(--color-chart-2)" },
              ]
            : view === "models"
              ? models.map((model, index) => ({
                    key: model,
                    label: shortModel(model),
                    color: modelColor(index),
                }))
              : levels.map((level, index) => ({
                    key: level,
                    label: thinkingLevelLabel(level),
                    color: modelColor(index),
                    meta: `${formatNumber(thinkingTotals.get(level) ?? 0)} · ${(totalThinkingRequests >
                    0
                        ? ((thinkingTotals.get(level) ?? 0) / totalThinkingRequests) * 100
                        : 0
                    ).toFixed(1)}%`,
                }));

    return (
        <div className="border-ink-700 bg-ink-900/45 flex max-h-20 flex-wrap gap-x-4 gap-y-1.5 overflow-y-auto border-t px-5 py-2.5">
            {items.map((item) => (
                <span key={item.key} className="text-fog-400 flex items-center gap-1.5 text-[11px]">
                    <span
                        className="h-2 w-2 shrink-0 rounded-sm"
                        style={{ backgroundColor: item.color }}
                    />
                    {item.label}
                    {item.meta ? (
                        <span className="text-fog-500 tabular-nums">{item.meta}</span>
                    ) : null}
                </span>
            ))}
        </div>
    );
}

function SplitTokenBar({ input, output }: { input: number; output: number }) {
    const total = input + output;
    const inputPct = total > 0 ? (input / total) * 100 : 0;
    const outputPct = total > 0 ? (output / total) * 100 : 0;
    return (
        <div>
            <div
                className="bg-ink-800 flex h-6 overflow-hidden rounded-md"
                title={`${formatTokens(input)} input · ${formatTokens(output)} output`}
            >
                <span
                    className="h-full bg-[var(--color-chart-1)]"
                    style={{ width: `${inputPct}%` }}
                />
                <span
                    className="h-full bg-[var(--color-chart-2)]"
                    style={{ width: `${outputPct}%` }}
                />
            </div>
            <div className="text-fog-500 mt-1 flex justify-between font-mono text-[10px]">
                <span>{formatTokens(input)} in</span>
                <span>{formatTokens(output)} out</span>
            </div>
        </div>
    );
}

function SegmentBar({
    items,
    total,
}: {
    items: Array<{ key: string; label: string; value: number; color: string }>;
    total: number;
}) {
    return (
        <div className="bg-ink-800 flex h-6 gap-px overflow-hidden rounded-md">
            {items.map((item) => {
                const pct = total > 0 ? Math.round((item.value / total) * 100) : 0;
                return (
                    <span
                        key={item.key}
                        className="flex h-full items-center justify-center text-[9px] font-semibold text-black/60"
                        style={{ flex: item.value, backgroundColor: item.color }}
                        title={`${item.label}: ${formatNumber(item.value)} requests (${pct}%)`}
                    >
                        {pct >= 12 ? `${pct}%` : ""}
                    </span>
                );
            })}
        </div>
    );
}

function RequestsByModelCard({ data }: { data: ModelMixResponse }) {
    const byModel: Record<string, { requests: number }> = {};
    for (const u of data.users) {
        for (const m of u.models) {
            byModel[m.model] = byModel[m.model] || { requests: 0 };
            byModel[m.model].requests += m.requests;
        }
    }
    const sorted = Object.entries(byModel).sort((a, b) => b[1].requests - a[1].requests);
    const maxReq = sorted[0]?.[1].requests ?? 1;
    const totalReq = sorted.reduce((s, [, v]) => s + v.requests, 0);
    const allModels = Array.from(new Set(data.users.flatMap((u) => u.models.map((m) => m.model))));

    return (
        <Card className="p-5">
            <h3 className="text-fog-100 text-sm font-semibold">Requests by model</h3>
            <p className="text-fog-400 mt-0.5 mb-4 text-xs">Request volume by model</p>
            <div className="space-y-2.5">
                {sorted.map(([model, v]) => {
                    const pct = Math.round((v.requests / totalReq) * 100);
                    const ci = allModels.indexOf(model);
                    return (
                        <div key={model} className="flex items-center gap-2.5">
                            <span className="text-fog-200 w-28 shrink-0 truncate font-mono text-xs">
                                {shortModel(model)}
                            </span>
                            <div className="bg-ink-800 h-5 flex-1 overflow-hidden rounded">
                                <div
                                    className="h-full rounded"
                                    style={{
                                        width: `${Math.max((v.requests / maxReq) * 100, 2)}%`,
                                        backgroundColor: modelColor(ci),
                                        opacity: 0.85,
                                    }}
                                />
                            </div>
                            <span className="text-fog-200 w-16 text-right text-xs tabular-nums">
                                {formatNumber(v.requests)}
                            </span>
                            <span className="text-fog-400 w-8 text-right text-[11px] tabular-nums">
                                {pct}%
                            </span>
                        </div>
                    );
                })}
            </div>
        </Card>
    );
}

const THINKING_LEVEL_ORDER = [
    "max",
    "high",
    "medium",
    "low",
    "minimal",
    "adaptive",
    "enabled",
    "disabled",
    "not_recorded",
];

function thinkingLevelLabel(level: string): string {
    if (level === "not_recorded") return "Not recorded";
    return level.replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function orderedThinkingLevels(data: ThinkingLevelMixResponse): string[] {
    return Array.from(
        new Set(data.users.flatMap((u) => u.levels.map((level) => level.level))),
    ).sort((a, b) => {
        const ai = THINKING_LEVEL_ORDER.indexOf(a);
        const bi = THINKING_LEVEL_ORDER.indexOf(b);
        if (ai === -1 && bi === -1) return a.localeCompare(b);
        if (ai === -1) return THINKING_LEVEL_ORDER.length - 1;
        if (bi === -1) return 1 - THINKING_LEVEL_ORDER.length;
        return ai - bi;
    });
}

const PERCENTILE_LABELS = ["Min", "P25", "P50", "P75", "P90", "P95", "Max"] as const;
const PERCENTILE_KEYS: (keyof PercentileBreakdown)[] = [
    "min",
    "p25",
    "p50",
    "p75",
    "p90",
    "p95",
    "max",
];

function DistributionCard({ distro }: { distro: DistributionResponse }) {
    function bars(breakdown: PercentileBreakdown, tone: "coral" | "amber") {
        const color = tone === "coral" ? "var(--color-chart-1)" : "var(--color-chart-2)";
        const localMax = Math.max(breakdown.max, 1);
        return (
            <div className="space-y-2">
                {PERCENTILE_KEYS.map((key, i) => {
                    const val = breakdown[key];
                    const pct = (val / localMax) * 100;
                    return (
                        <div key={key} className="flex items-center gap-3">
                            <span className="text-fog-400 w-8 text-right font-mono text-[11px]">
                                {PERCENTILE_LABELS[i]}
                            </span>
                            <div className="bg-ink-900 relative h-5 flex-1 overflow-hidden rounded">
                                <div
                                    className="h-full rounded transition-all duration-300"
                                    style={{
                                        width: `${Math.max(pct, 0.5)}%`,
                                        backgroundColor: color,
                                    }}
                                />
                            </div>
                            <span className="text-fog-200 w-20 text-right text-xs tabular-nums">
                                {formatTokens(val)}
                            </span>
                        </div>
                    );
                })}
            </div>
        );
    }

    return (
        <Card className="p-5">
            <div className="flex items-baseline justify-between">
                <h3 className="text-fog-100 text-sm font-semibold">
                    Per-request token distribution
                </h3>
            </div>
            <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-2">
                <div>
                    <h4 className="text-fog-200 mb-3 text-xs font-semibold">Input tokens</h4>
                    {bars(distro.input_tokens, "coral")}
                </div>
                <div>
                    <h4 className="text-fog-200 mb-3 text-xs font-semibold">Output tokens</h4>
                    {bars(distro.output_tokens, "amber")}
                </div>
            </div>
        </Card>
    );
}
