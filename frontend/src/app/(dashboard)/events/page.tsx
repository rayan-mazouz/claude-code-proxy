"use client";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { ProxyEvent, TimeRange, UserLookup } from "@/lib/types";
import { RangePicker } from "@/components/RangePicker";
import { UserFilter } from "@/components/UserFilter";
import { EventTypeFilter } from "@/components/EventTypeFilter";
import { RequestCaptureOverlay } from "@/components/RequestCaptureOverlay";
import { Button, Pagination, SelectMenu, StatusToggle, TextInput } from "@/components/ui";
import { formatCompactNumber, formatDateTime } from "@/lib/format";

const EVENT_TYPES = [
    "request.received",
    "request.exhausted",
    "request.spare_capacity_throttled",
    "account.attempt",
    "account.busy",
    "account.capacity",
    "account.cooldown",
    "account.error",
    "account.rate_limited",
    "account.response_received",
    "account.transient_error",
    "fallback.attempt",
    "fallback.busy",
    "fallback.capacity",
    "fallback.error",
    "fallback.response_received",
    "fallback.transient_error",
    "response.returned",
    "response.completed",
    "response.failed",
    "response.interrupted",
];
const GROUP_BY_OPTIONS = ["events"] as const;
type GroupBy = "request" | (typeof GROUP_BY_OPTIONS)[number];
const initialRange = (): TimeRange => {
    const now = new Date();
    return {
        start: new Date(now.getFullYear(), now.getMonth(), 1).toISOString(),
        end: now.toISOString(),
        label: "This month",
    };
};
const PAGE_SIZE_OPTIONS = [25, 50, 75, 100] as const;
const DEFAULT_PAGE_SIZE = 25;
const AUTO_REFRESH_KEY = "dashboard_auto_refresh";
const REFRESH_SECS_KEY = "dashboard_refresh_secs";
const rowTone = (type: string) =>
    type.includes("exhausted") ||
    type.includes("error") ||
    type.includes("capacity") ||
    type.includes("failed") ||
    type.includes("interrupted")
        ? "bg-bad-500/10 hover:bg-bad-500/15"
        : type.includes("cooldown") || type.includes("rate_limited") || type.includes("throttled")
          ? "bg-warn-500/10 hover:bg-warn-500/15"
          : type.includes("response_received") ||
              type.includes("returned") ||
              type === "response.completed"
            ? "bg-good-500/10 hover:bg-good-500/15"
            : "hover:bg-brand-500/5";

function displayUser(event: ProxyEvent, users: UserLookup[]): string {
    const name = event.metadata.user_name;
    if (typeof name === "string" && name) {
        return event.metadata.user_deleted && name !== "Deleted user" ? `${name} (deleted)` : name;
    }
    return (
        users.find((user) => user.id === event.user_id)?.name ??
        (event.user_id ? "User unavailable" : "Unattributed")
    );
}

function requestTone(events: ProxyEvent[]): string {
    const outcome = events.find((event) => typeof event.metadata.request_outcome === "string")
        ?.metadata.request_outcome;
    if (outcome === "failed") return "bg-bad-500/10 hover:bg-bad-500/15";
    if (outcome === "succeeded") return "bg-good-500/10 hover:bg-good-500/15";
    const terminal = [...events]
        .reverse()
        .find((event) =>
            ["response.returned", "request.exhausted", "request.rejected"].includes(
                event.event_type,
            ),
        );
    if (outcome !== "pending" && terminal?.status_code != null) {
        return terminal.status_code >= 400
            ? "bg-bad-500/10 hover:bg-bad-500/15"
            : "bg-good-500/10 hover:bg-good-500/15";
    }
    return "hover:bg-brand-500/5";
}

function displayModel(metadata: Record<string, unknown>): string {
    const effective = String(metadata.model ?? "—");
    const requested = typeof metadata.requested_model === "string" ? metadata.requested_model : "";
    const reported =
        typeof metadata.upstream_response_model === "string"
            ? metadata.upstream_response_model
            : "";
    const compact = (model: string) => model.replace(/^claude-/, "");
    const details: string[] = [];
    if (requested && requested !== effective) details.push(`requested ${compact(requested)}`);
    if (reported && reported !== effective) details.push(`reported ${compact(reported)}`);
    return details.length > 0
        ? `${compact(effective)} (${details.join(", ")})`
        : compact(effective);
}

function groupValue(event: ProxyEvent, groupBy: GroupBy): string {
    return groupBy === "request" ? event.request_id : event.id;
}

function CacheReadMetric({ cached, input }: { cached: unknown; input: unknown }) {
    if (cached == null || input == null) return <>—</>;
    const cachedTokens = Number(cached);
    const inputTokens = Number(input);
    if (!Number.isFinite(cachedTokens) || !Number.isFinite(inputTokens)) return <>—</>;
    if (inputTokens <= 0) return <>{formatCompactNumber(cachedTokens)}</>;

    const percentage = (cachedTokens / inputTokens) * 100;
    const tone =
        percentage > 95
            ? "bg-good-500/15 text-good-300 ring-good-500/25"
            : "bg-bad-500/15 text-bad-300 ring-bad-500/25";
    return (
        <span
            className={`inline-flex min-w-0 items-center gap-1 rounded-md px-2 py-1 text-xs leading-tight whitespace-nowrap ring-1 ring-inset ${tone}`}
        >
            <span>{formatCompactNumber(cachedTokens)}</span>
            <span className="text-[11px] font-medium">({percentage.toFixed(1)}%)</span>
        </span>
    );
}

export default function EventsPage() {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const queryString = searchParams.toString();
    const query = useMemo(() => {
        const params = new URLSearchParams(queryString);
        const defaultRange = initialRange();
        const pageSizeValue = Number(params.get("pageSize"));
        const pageValue = Number(params.get("page"));
        const pageSize = PAGE_SIZE_OPTIONS.includes(
            pageSizeValue as (typeof PAGE_SIZE_OPTIONS)[number],
        )
            ? pageSizeValue
            : DEFAULT_PAGE_SIZE;
        const page = Number.isInteger(pageValue) && pageValue > 0 ? pageValue : 1;
        const rawGroupBy = params.get("groupBy");
        const groupBy: GroupBy = GROUP_BY_OPTIONS.includes(
            rawGroupBy as (typeof GROUP_BY_OPTIONS)[number],
        )
            ? (rawGroupBy as GroupBy)
            : "request";
        const rangeLabel = params.get("range");
        return {
            pageSize,
            offset: (page - 1) * pageSize,
            groupBy,
            eventType: params.get("event") ?? "",
            model: params.get("model") ?? "",
            userId: params.get("user") || null,
            range: {
                start:
                    params.get("start") ||
                    (rangeLabel === "All time" ? undefined : defaultRange.start),
                end: params.get("end") || defaultRange.end,
                label: rangeLabel || defaultRange.label,
            } satisfies TimeRange,
        };
    }, [queryString]);
    const [range, setRange] = useState<TimeRange>(initialRange);
    const [users, setUsers] = useState<UserLookup[]>([]);
    const [models, setModels] = useState<string[]>([]);
    const [userId, setUserId] = useState<string | null>(null);
    const [eventType, setEventType] = useState("");
    const [model, setModel] = useState("");
    const [groupBy, setGroupBy] = useState<GroupBy>("request");
    const [events, setEvents] = useState<ProxyEvent[]>([]);
    const [expandedRequests, setExpandedRequests] = useState<Set<string>>(new Set());
    const [total, setTotal] = useState(0);
    const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
    const [offset, setOffset] = useState(0);
    const [queryHydrated, setQueryHydrated] = useState(false);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [autoRefresh, setAutoRefresh] = useState(false);
    const [refreshSecs, setRefreshSecs] = useState(10);
    const [error, setError] = useState<string | null>(null);
    const [captureEventId, setCaptureEventId] = useState<string | null>(null);
    const load = useCallback(
        (pageOffset = offset, selectedEventType = eventType, selectedModel = model) => {
            setLoading(true);
            setError(null);
            return api
                .events(
                    pageSize,
                    pageOffset,
                    selectedEventType || undefined,
                    userId || undefined,
                    range.start,
                    range.end,
                    undefined,
                    selectedModel || undefined,
                    groupBy === "request" ? "request" : undefined,
                )
                .then((page) => {
                    setEvents(page.events);
                    setTotal(page.total);
                })
                .catch((e) => setError(e instanceof Error ? e.message : "Unable to load events."))
                .finally(() => setLoading(false));
        },
        [eventType, groupBy, model, offset, pageSize, range.end, range.start, userId],
    );
    const toggleRequest = (requestId: string) => {
        setExpandedRequests((current) => {
            const next = new Set(current);
            if (next.has(requestId)) next.delete(requestId);
            else next.add(requestId);
            return next;
        });
    };
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
    const refreshEvents = useCallback(async () => {
        setRefreshing(true);
        try {
            await load(0);
            setOffset(0);
            updateUrl({ page: "1" });
        } finally {
            setRefreshing(false);
        }
    }, [load, updateUrl]);
    useEffect(() => {
        api.analyticsUsers()
            .then(setUsers)
            .catch(() => {});
        api.analyticsModels()
            .then(setModels)
            .catch(() => {});
    }, []);
    useEffect(() => {
        setRange(query.range);
        setUserId(query.userId);
        setEventType(query.eventType);
        setModel(query.model);
        setGroupBy(query.groupBy);
        setPageSize(query.pageSize);
        setOffset(query.offset);
        setQueryHydrated(true);
    }, [query]);
    useEffect(() => {
        if (!queryHydrated) return;
        load(offset);
    }, [load, offset, queryHydrated]);
    useEffect(() => {
        if (!queryHydrated || total <= 0 || offset < total) return;
        const lastPageOffset = Math.max(0, (Math.ceil(total / pageSize) - 1) * pageSize);
        setOffset(lastPageOffset);
        updateUrl({ page: String(Math.floor(lastPageOffset / pageSize) + 1) });
    }, [offset, pageSize, queryHydrated, total, updateUrl]);
    useEffect(() => {
        try {
            if (window.localStorage.getItem(AUTO_REFRESH_KEY) === "1") setAutoRefresh(true);
            const seconds = Number(window.localStorage.getItem(REFRESH_SECS_KEY));
            if (Number.isFinite(seconds) && seconds > 0) setRefreshSecs(Math.floor(seconds));
        } catch {}
    }, []);
    useEffect(() => {
        try {
            window.localStorage.setItem(AUTO_REFRESH_KEY, autoRefresh ? "1" : "0");
        } catch {}
    }, [autoRefresh]);
    useEffect(() => {
        try {
            window.localStorage.setItem(REFRESH_SECS_KEY, String(refreshSecs));
        } catch {}
    }, [refreshSecs]);
    useEffect(() => {
        if (!autoRefresh) return;
        const id = setInterval(() => void refreshEvents(), Math.max(1, refreshSecs) * 1000);
        return () => clearInterval(id);
    }, [autoRefresh, refreshEvents, refreshSecs]);
    return (
        <main className="space-y-5">
            <header className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-fog-100 font-serif text-2xl font-semibold tracking-tight">
                        Proxy events
                    </h1>
                    <p className="text-fog-400 mt-0.5 text-sm">
                        Trace request routing, retries, capacity failures, cooldowns, and completed
                        responses.
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
                            onClick={() => setAutoRefresh((current) => !current)}
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
                                onChange={(event) => {
                                    const seconds = Number(event.target.value);
                                    if (Number.isFinite(seconds) && seconds > 0)
                                        setRefreshSecs(Math.floor(seconds));
                                }}
                                className="px-2 py-0.5 text-center disabled:opacity-50"
                            />
                        </div>
                        <span className="text-fog-400 text-xs">sec</span>
                    </div>
                    <Button
                        variant="ghost"
                        className="h-9"
                        onClick={() => void refreshEvents()}
                        disabled={refreshing}
                    >
                        {refreshing ? "Refreshing…" : "Refresh"}
                    </Button>
                    <RangePicker
                        value={range}
                        onChange={(next) => {
                            setRange(next);
                            setOffset(0);
                            updateUrl({
                                start: next.start,
                                end: next.end,
                                range: next.label,
                                page: "1",
                            });
                        }}
                    />
                    <UserFilter
                        users={users}
                        value={userId}
                        onChange={(next) => {
                            setUserId(next);
                            setOffset(0);
                            updateUrl({ user: next, page: "1" });
                        }}
                    />
                </div>
            </header>
            <div className="border-ink-700 bg-ink-900 flex flex-wrap items-end gap-3 rounded-xl border p-3">
                <EventTypeFilter
                    value={eventType}
                    options={EVENT_TYPES}
                    onChange={(next) => {
                        setEventType(next);
                        setOffset(0);
                        updateUrl({ event: next, page: "1" });
                    }}
                />
                <EventTypeFilter
                    value={model}
                    options={models}
                    onChange={(next) => {
                        setModel(next);
                        setOffset(0);
                        updateUrl({ model: next, page: "1" });
                    }}
                    label="Model"
                    allLabel="All models"
                    idPrefix="event-model-options"
                />
                <div className="min-w-[220px]">
                    <span className="text-fog-400 block text-xs">Group by</span>
                    <SelectMenu
                        value={groupBy}
                        options={[
                            { value: "request", label: "Individual requests" },
                            { value: "events", label: "All events" },
                        ]}
                        onChange={(next) => {
                            const nextGroupBy = next as GroupBy;
                            setGroupBy(nextGroupBy);
                            setOffset(0);
                            updateUrl({
                                groupBy: nextGroupBy === "request" ? null : nextGroupBy,
                                page: "1",
                            });
                        }}
                        ariaLabel="Group events by"
                        className="mt-1"
                    />
                </div>
                <button
                    onClick={() => {
                        setOffset(0);
                        updateUrl({
                            event: eventType,
                            model,
                            user: userId,
                            start: range.start,
                            end: range.end,
                            range: range.label,
                            page: "1",
                            pageSize: String(pageSize),
                        });
                    }}
                    className="bg-brand-500 text-ink-950 rounded-lg px-3 py-2 text-sm font-semibold"
                >
                    Apply filters
                </button>
            </div>
            <section className="border-ink-700 bg-ink-900 overflow-hidden rounded-xl border">
                {loading ? (
                    <div className="text-fog-400 p-6 text-sm">Loading…</div>
                ) : error ? (
                    <div className="text-bad-400 p-6 text-sm">{error}</div>
                ) : events.length === 0 ? (
                    <div className="text-fog-400 p-6 text-sm">No events in this range.</div>
                ) : (
                    <div className="overflow-x-auto overscroll-x-contain">
                        <table className="w-full min-w-[1720px] text-left text-sm">
                            <thead className="bg-ink-850 text-fog-400 border-ink-700 border-b text-xs uppercase">
                                <tr>
                                    {[
                                        "Date / time",
                                        "User",
                                        "Model",
                                        "Thinking",
                                        "Account / provider",
                                        groupBy === "request" ? "Request / event" : "Event",
                                        "Input",
                                        "Output",
                                        "TPS",
                                        "Cache read",
                                        "Cache write",
                                        "Cost",
                                        "Status",
                                    ].map((x) => (
                                        <th
                                            key={x}
                                            className="px-3 py-3 font-medium whitespace-nowrap"
                                        >
                                            {x}
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {(() => {
                                    const groups = new Map<string, ProxyEvent[]>();
                                    events.forEach((event) => {
                                        const key = groupValue(event, groupBy);
                                        const existing = groups.get(key);
                                        if (existing) existing.push(event);
                                        else groups.set(key, [event]);
                                    });
                                    const rows: ReactNode[] = [];
                                    groups.forEach((groupEvents, key) => {
                                        const expanded = expandedRequests.has(key);
                                        const allEvents = [...groupEvents].sort(
                                            (a, b) =>
                                                a.created_at.localeCompare(b.created_at) ||
                                                a.id.localeCompare(b.id),
                                        );
                                        const summary =
                                            [...allEvents]
                                                .reverse()
                                                .find(
                                                    (event) =>
                                                        event.event_type === "response.returned",
                                                ) ?? allEvents[allEvents.length - 1];
                                        const visibleEvents =
                                            groupBy === "request"
                                                ? [
                                                      { event: summary, summary: true },
                                                      ...(expanded
                                                          ? allEvents.map((event) => ({
                                                                event,
                                                                summary: false,
                                                            }))
                                                          : []),
                                                  ]
                                                : groupEvents.map((event) => ({
                                                      event,
                                                      summary: false,
                                                  }));
                                        visibleEvents.forEach(
                                            ({ event: e, summary: isSummary }) => {
                                                const m = e.metadata as Record<string, unknown>;
                                                rows.push(
                                                    <tr
                                                        key={isSummary ? `request-${key}` : e.id}
                                                        className={`border-ink-800 border-b align-top ${isSummary ? requestTone(allEvents) : rowTone(e.event_type)}`}
                                                    >
                                                        <td
                                                            className={`text-fog-400 w-44 min-w-44 py-3 pr-3 font-mono text-xs whitespace-nowrap ${groupBy === "request" && !isSummary ? "pl-8" : "pl-3"}`}
                                                        >
                                                            <span className="block">
                                                                {formatDateTime(e.created_at)}
                                                            </span>
                                                            {isSummary ? (
                                                                <button
                                                                    type="button"
                                                                    aria-expanded={expanded}
                                                                    aria-label={`${expanded ? "Collapse" : "Expand"} request ${key}`}
                                                                    onClick={() =>
                                                                        toggleRequest(key)
                                                                    }
                                                                    className="border-brand-400/40 bg-brand-500/10 text-brand-200 hover:bg-brand-500/20 focus-visible:outline-brand-400 mt-2 inline-flex items-center gap-1.5 rounded-md border px-2 py-1.5 font-sans text-xs font-semibold focus-visible:outline-2"
                                                                >
                                                                    {expanded ? (
                                                                        <ChevronDown
                                                                            size={16}
                                                                            aria-hidden="true"
                                                                        />
                                                                    ) : (
                                                                        <ChevronRight
                                                                            size={16}
                                                                            aria-hidden="true"
                                                                        />
                                                                    )}
                                                                    {expanded
                                                                        ? "Hide events"
                                                                        : `Show events (${allEvents.length})`}
                                                                </button>
                                                            ) : null}
                                                        </td>
                                                        <td className="text-fog-300 w-32 min-w-32 px-3 py-3 text-xs whitespace-nowrap">
                                                            {displayUser(e, users)}
                                                        </td>
                                                        <td className="text-fog-300 w-56 min-w-56 px-3 py-3 text-xs">
                                                            <span
                                                                className="block max-w-56 truncate"
                                                                title={displayModel(m)}
                                                            >
                                                                {displayModel(m)}
                                                            </span>
                                                        </td>
                                                        <td className="text-fog-300 w-28 min-w-28 px-3 py-3 text-xs whitespace-nowrap">
                                                            {String(
                                                                m.thinking_level ??
                                                                    m.reasoning_level ??
                                                                    "—",
                                                            )}
                                                        </td>
                                                        <td className="text-fog-400 w-80 max-w-80 min-w-80 px-3 py-3 font-mono text-xs whitespace-normal">
                                                            <span className="block [overflow-wrap:anywhere] break-words">
                                                                {String(m.account_name ?? "—")}
                                                            </span>
                                                        </td>
                                                        <td className="text-brand-300 w-64 min-w-64 px-3 py-3 font-medium whitespace-normal">
                                                            <div className="flex flex-wrap items-center gap-2">
                                                                {isSummary ? (
                                                                    <span>
                                                                        <span
                                                                            className="block font-mono text-xs"
                                                                            title={key}
                                                                        >
                                                                            {key.length > 22
                                                                                ? `${key.slice(0, 12)}…${key.slice(-6)}`
                                                                                : key}
                                                                        </span>
                                                                    </span>
                                                                ) : (
                                                                    <span>{e.event_type}</span>
                                                                )}
                                                                {(e.event_type ===
                                                                    "request.received" ||
                                                                    e.event_type ===
                                                                        "response.returned") &&
                                                                e.archive_hot !== false &&
                                                                e.request_id?.startsWith("req_") ? (
                                                                    <button
                                                                        type="button"
                                                                        onClick={() =>
                                                                            setCaptureEventId(
                                                                                e.request_id.slice(
                                                                                    4,
                                                                                ),
                                                                            )
                                                                        }
                                                                        className="text-fog-300 hover:text-brand-200 rounded border border-current/30 px-1.5 py-0.5 text-[10px] font-medium tracking-wide uppercase"
                                                                    >
                                                                        View capture
                                                                    </button>
                                                                ) : null}
                                                            </div>
                                                        </td>
                                                        <td className="w-24 min-w-24 px-3 py-3 font-mono text-xs whitespace-nowrap">
                                                            <span
                                                                title={
                                                                    typeof m.usage_unavailable_reason ===
                                                                    "string"
                                                                        ? m.usage_unavailable_reason
                                                                        : undefined
                                                                }
                                                            >
                                                                {m.usage_available === false
                                                                    ? "Unavailable"
                                                                    : formatCompactNumber(
                                                                          m.input_tokens,
                                                                      )}
                                                            </span>
                                                        </td>
                                                        <td className="w-24 min-w-24 px-3 py-3 font-mono text-xs whitespace-nowrap">
                                                            {formatCompactNumber(m.output_tokens)}
                                                        </td>
                                                        <td className="text-fog-300 w-24 min-w-24 px-3 py-3 font-mono text-xs whitespace-nowrap">
                                                            {m.tokens_per_second == null
                                                                ? "—"
                                                                : `${Number(m.tokens_per_second).toFixed(1)} t/s`}
                                                        </td>
                                                        <td className="w-32 min-w-32 px-3 py-3 font-mono text-xs">
                                                            <CacheReadMetric
                                                                cached={m.cached_input_tokens}
                                                                input={m.input_tokens}
                                                            />
                                                        </td>
                                                        <td className="w-28 min-w-28 px-3 py-3 font-mono text-xs whitespace-nowrap">
                                                            {formatCompactNumber(
                                                                m.cache_write_tokens,
                                                            )}
                                                        </td>
                                                        <td className="w-28 min-w-28 px-3 py-3 font-mono text-xs whitespace-nowrap">
                                                            {m.cost_usd == null
                                                                ? "—"
                                                                : `$${Number(m.cost_usd).toFixed(4)}`}
                                                        </td>
                                                        <td className="text-fog-300 w-24 min-w-24 px-3 py-3 whitespace-nowrap">
                                                            {e.status_code ?? "—"}
                                                        </td>
                                                    </tr>,
                                                );
                                            },
                                        );
                                    });
                                    return rows;
                                })()}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>
            {total > 0 ? (
                <Pagination
                    total={total}
                    page={Math.floor(offset / pageSize) + 1}
                    pageSize={pageSize}
                    pageSizeOptions={[...PAGE_SIZE_OPTIONS]}
                    disabled={loading}
                    onPageSizeChange={(next) => {
                        setPageSize(next);
                        setOffset(0);
                        updateUrl({ pageSize: String(next), page: "1" });
                    }}
                    onPageChange={(nextPage) => {
                        setOffset((nextPage - 1) * pageSize);
                        updateUrl({ page: String(nextPage) });
                    }}
                />
            ) : null}
            {captureEventId ? (
                <RequestCaptureOverlay
                    eventId={captureEventId}
                    onClose={() => setCaptureEventId(null)}
                />
            ) : null}
        </main>
    );
}
