"use client";

import { useId, useMemo, useState } from "react";
import {
    CartesianGrid,
    Line,
    LineChart,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from "recharts";
import { ActivityResponse } from "@/lib/types";
import { formatCompactNumber } from "@/lib/format";
import { groupRequestUsers, OTHERS_ID } from "@/lib/requestAnalytics";
import { STACK_COLORS } from "@/components/Charts";

type View = "tps" | "models" | "users";
const VIEWS: { value: View; label: string }[] = [
    { value: "tps", label: "TPS" },
    { value: "models", label: "Requests by model" },
    { value: "users", label: "Requests by user" },
];
const AXIS = "var(--color-chart-axis)";
const GRID = "var(--color-chart-grid)";
function colorFor(index: number) {
    return index < STACK_COLORS.length
        ? STACK_COLORS[index]
        : `hsl(${(index * 137.5) % 360} 55% 60%)`;
}

function dates(ts: string, hourly: boolean) {
    const date = new Date(ts);
    return {
        axis: hourly
            ? date.toLocaleTimeString(undefined, {
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
              })
            : date.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
        label: date.toLocaleString(undefined, {
            month: "short",
            day: "numeric",
            year: "numeric",
            ...(hourly ? { hour: "2-digit", minute: "2-digit" } : {}),
        }),
    };
}

export function RequestAnalyticsCard({ activity }: { activity: ActivityResponse }) {
    const [view, setView] = useState<View>("tps");
    const [hidden, setHidden] = useState<Set<string>>(new Set());
    const id = useId();
    const hourly = activity.granularity === "hour";
    const userBuckets = useMemo(
        () => groupRequestUsers(activity.points.map((point) => point.requests_by_user ?? {})),
        [activity],
    );
    const series = useMemo(() => {
        const totals = new Map<string, number>();
        for (const [index, point] of activity.points.entries()) {
            const values =
                view === "tps"
                    ? point.model_tps
                    : view === "users"
                      ? userBuckets[index]
                      : point.requests_by_model;
            for (const [key, value] of Object.entries(values ?? {}))
                totals.set(key, (totals.get(key) ?? 0) + value);
        }
        const colorKeys =
            view === "users"
                ? [...totals.keys()].sort()
                : Array.from(
                      new Set(
                          activity.points.flatMap((point) => [
                              ...Object.keys(point.requests_by_model ?? {}),
                              ...Object.keys(point.model_tps ?? {}),
                          ]),
                      ),
                  ).sort();
        return [...totals.keys()]
            .sort((a, b) =>
                view === "users" ? (totals.get(b) ?? 0) - (totals.get(a) ?? 0) : a.localeCompare(b),
            )
            .map((key, i) => ({
                id: key,
                key: `series${i}`,
                label:
                    view === "users"
                        ? key === OTHERS_ID
                            ? "Others"
                            : (activity.user_names?.[key] ?? "Deleted user")
                        : key,
                color:
                    view === "users" && key === OTHERS_ID
                        ? "var(--color-fog-400)"
                        : colorFor(colorKeys.indexOf(key)),
            }));
    }, [activity, view, userBuckets]);
    const visible = series.filter((s) => !hidden.has(`${view}:${s.id}`));
    const effectiveHidden = visible.length ? hidden : new Set<string>();
    const data = activity.points.map((point, index) => {
        const values =
            view === "tps"
                ? point.model_tps
                : view === "users"
                  ? userBuckets[index]
                  : point.requests_by_model;
        return {
            ...dates(point.ts, hourly),
            ...Object.fromEntries(
                series.map((s) => [s.key, values?.[s.id] ?? (view === "tps" ? null : 0)]),
            ),
        };
    });
    return (
        <section
            aria-label="Request analytics"
            className="border-ink-700 bg-ink-850 min-w-0 overflow-hidden rounded-xl border shadow-[var(--shadow-card)]"
        >
            <div className="flex flex-col gap-4 p-4 sm:p-5 xl:flex-row xl:items-center xl:justify-between">
                <div className="min-w-0">
                    <h3 className="text-fog-100 text-sm font-semibold">Request analytics</h3>
                </div>
                <div
                    role="tablist"
                    aria-label="Request analytics view"
                    className="border-ink-700 bg-ink-900 grid grid-cols-3 gap-1 rounded-lg border p-1 xl:grid-cols-3"
                >
                    {VIEWS.map((tab) => (
                        <button
                            type="button"
                            role="tab"
                            key={tab.value}
                            id={`${id}-${tab.value}`}
                            aria-controls={`${id}-panel`}
                            aria-selected={view === tab.value}
                            tabIndex={view === tab.value ? 0 : -1}
                            onKeyDown={(event) => {
                                const index = VIEWS.findIndex((item) => item.value === tab.value);
                                const next =
                                    event.key === "ArrowRight"
                                        ? (index + 1) % VIEWS.length
                                        : event.key === "ArrowLeft"
                                          ? (index + VIEWS.length - 1) % VIEWS.length
                                          : event.key === "Home"
                                            ? 0
                                            : event.key === "End"
                                              ? VIEWS.length - 1
                                              : -1;
                                if (next < 0) return;
                                event.preventDefault();
                                setView(VIEWS[next].value);
                                document.getElementById(`${id}-${VIEWS[next].value}`)?.focus();
                            }}
                            onClick={() => setView(tab.value)}
                            className={`focus-visible:outline-brand-400 min-w-0 rounded-md px-3 py-2 text-xs font-medium whitespace-normal transition-colors focus-visible:outline-2 xl:whitespace-nowrap ${view === tab.value ? "bg-ink-700 text-fog-100 shadow-sm" : "text-fog-400 hover:text-fog-200"}`}
                        >
                            {tab.label}
                        </button>
                    ))}
                </div>
            </div>
            <div
                role="tabpanel"
                id={`${id}-panel`}
                aria-labelledby={`${id}-${view}`}
                className="border-ink-700 min-w-0 border-t px-3 py-5 sm:px-5"
            >
                {series.length ? (
                    <>
                        <div
                            className="mb-5 flex max-h-28 flex-wrap gap-x-4 gap-y-1 overflow-y-auto"
                            aria-label={view === "users" ? "User series" : "Model series"}
                        >
                            {series.map((s) => {
                                const isHidden = effectiveHidden.has(`${view}:${s.id}`);
                                return (
                                    <button
                                        type="button"
                                        key={s.id}
                                        aria-pressed={!isHidden}
                                        disabled={!isHidden && visible.length === 1}
                                        onClick={() =>
                                            setHidden((current) => {
                                                const next = new Set(current);
                                                const key = `${view}:${s.id}`;
                                                if (next.has(key)) next.delete(key);
                                                else next.add(key);
                                                return next;
                                            })
                                        }
                                        className={`text-fog-300 focus-visible:outline-brand-400 inline-flex max-w-full items-center gap-2 rounded py-1 text-xs focus-visible:outline-2 ${isHidden ? "opacity-35" : ""}`}
                                    >
                                        <span
                                            className="h-2 w-2 shrink-0 rounded-full"
                                            style={{ backgroundColor: s.color }}
                                        />
                                        <span className="truncate font-mono" title={s.label}>
                                            {s.label.replace(/^(gpt-|claude-)/, "")}
                                        </span>
                                    </button>
                                );
                            })}
                        </div>
                        <div className="h-[280px] w-full sm:h-[320px]">
                            <ResponsiveContainer width="100%" height="100%">
                                <LineChart
                                    data={data}
                                    margin={{ top: 8, right: 12, left: 0, bottom: 8 }}
                                    accessibilityLayer
                                >
                                    <CartesianGrid
                                        stroke={GRID}
                                        strokeDasharray="3 5"
                                        vertical={false}
                                    />
                                    <XAxis
                                        dataKey="axis"
                                        stroke={AXIS}
                                        tickLine={false}
                                        axisLine={false}
                                        minTickGap={32}
                                        tick={{ fontSize: 11 }}
                                        dy={8}
                                    />
                                    <YAxis
                                        stroke={AXIS}
                                        tickLine={false}
                                        axisLine={false}
                                        width={44}
                                        tick={{ fontSize: 11 }}
                                        domain={[0, "auto"]}
                                        allowDecimals={view === "tps"}
                                        tickFormatter={(value: number) =>
                                            String(formatCompactNumber(value))
                                        }
                                    />
                                    <Tooltip
                                        content={({ active, payload }) => {
                                            if (!active || !payload?.length) return null;
                                            const entries = payload.filter((p) => p.value != null);
                                            if (!entries.length) return null;
                                            return (
                                                <div className="border-ink-600 bg-ink-950 text-fog-200 max-w-[calc(100vw-5rem)] rounded-xl border p-3 text-xs shadow-xl sm:max-w-[270px]">
                                                    <p className="text-fog-400 mb-2">
                                                        {String(payload[0]?.payload.label ?? "")}
                                                    </p>
                                                    {entries.map((p) => (
                                                        <div
                                                            key={String(p.dataKey)}
                                                            className="flex justify-between gap-4 py-1"
                                                        >
                                                            <span
                                                                className="truncate"
                                                                style={{ color: p.color }}
                                                            >
                                                                {p.name}
                                                            </span>
                                                            <span className="shrink-0 font-mono tabular-nums">
                                                                {view === "tps"
                                                                    ? `${Number(p.value).toFixed(2)} t/s`
                                                                    : `${Number(p.value).toLocaleString()} req`}
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            );
                                        }}
                                    />
                                    {series.map((s) => (
                                        <Line
                                            key={s.id}
                                            dataKey={s.key}
                                            name={s.label}
                                            stroke={s.color}
                                            strokeWidth={2.4}
                                            type="monotone"
                                            dot={{ r: 2, strokeWidth: 0, fill: s.color }}
                                            activeDot={{ r: 5, fill: s.color }}
                                            hide={effectiveHidden.has(`${view}:${s.id}`)}
                                            connectNulls={false}
                                            isAnimationActive={false}
                                        />
                                    ))}
                                </LineChart>
                            </ResponsiveContainer>
                        </div>
                    </>
                ) : (
                    <div className="text-fog-400 flex h-[280px] items-center justify-center text-sm">
                        {view === "tps"
                            ? "No TPS measurements in this range."
                            : "No requests in this range."}
                    </div>
                )}
            </div>
        </section>
    );
}
