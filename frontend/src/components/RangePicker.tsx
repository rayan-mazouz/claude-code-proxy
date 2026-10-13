"use client";

// ---------------------------------------------------------------------------
// Date-range picker: a two-month calendar with a quick-preset rail and From/To
// time selectors. Presets apply immediately; selecting two days (plus tweaking
// the times) commits a custom range. Bounds are emitted as ISO 8601 UTC.
// ---------------------------------------------------------------------------

import { useEffect, useMemo, useRef, useState } from "react";
import { TimeRange } from "@/lib/types";

type Preset = {
    label: string;
    minutes?: number;
    kind?: "month" | "all";
};

const PRESETS: Preset[] = [
    { label: "Last hour", minutes: 60 },
    { label: "Last 6 hours", minutes: 360 },
    { label: "Last 24 hours", minutes: 1440 },
    { label: "Last 7 days", minutes: 10080 },
    { label: "This month", kind: "month" },
    { label: "Last 30 days", minutes: 43200 },
    { label: "Last 90 days", minutes: 129600 },
    { label: "All time", kind: "all" },
];

function presetRange(preset: Preset, now: Date): TimeRange {
    if (preset.kind === "all") return { end: now.toISOString(), label: preset.label };
    const from =
        preset.kind === "month"
            ? new Date(now.getFullYear(), now.getMonth(), 1)
            : new Date(now.getTime() - (preset.minutes ?? 0) * 60000);
    return { start: from.toISOString(), end: now.toISOString(), label: preset.label };
}

// Preset ranges are relative ("Last hour", "This month"): re-anchor them to the
// current time whenever data is fetched, so refreshes include new activity.
// Custom ranges are absolute and returned unchanged.
export function resolveRange(range: TimeRange, now: Date = new Date()): TimeRange {
    const preset = PRESETS.find((candidate) => candidate.label === range.label);
    return preset ? presetRange(preset, now) : range;
}

const DOW = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
];

function startOfDay(d: Date): Date {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}
function addMonths(d: Date, n: number): Date {
    return new Date(d.getFullYear(), d.getMonth() + n, 1);
}
function sameDay(a: Date, b: Date): boolean {
    return startOfDay(a).getTime() === startOfDay(b).getTime();
}
function fmtStamp(d: Date): string {
    const h12 = d.getHours() % 12 || 12;
    const ap = d.getHours() < 12 ? "AM" : "PM";
    return `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${h12}:${String(d.getMinutes()).padStart(2, "0")} ${ap}`;
}

// Build the 7-column cell grid (including leading/trailing days) for one month.
function monthCells(year: number, month: number): Date[] {
    const first = new Date(year, month, 1);
    const lead = first.getDay();
    const dim = new Date(year, month + 1, 0).getDate();
    const count = Math.ceil((lead + dim) / 7) * 7;
    return Array.from({ length: count }, (_, i) => new Date(year, month, 1 - lead + i));
}

export function RangePicker({
    value,
    onChange,
}: {
    value: TimeRange;
    onChange: (range: TimeRange) => void;
}) {
    const [open, setOpen] = useState(false);
    const wrapRef = useRef<HTMLDivElement>(null);

    // Draft selection while the popover is open.
    const [view, setView] = useState<Date>(() => new Date());
    const [start, setStart] = useState<Date>(() => new Date());
    const [end, setEnd] = useState<Date | null>(null);
    const [stage, setStage] = useState<0 | 1>(0); // 0: next click sets start

    // Seed the draft from the committed value each time the popover opens.
    function toggleOpen() {
        if (open) {
            setOpen(false);
            return;
        }
        const s = value.start ? new Date(value.start) : new Date(Date.now() - 43200 * 60000);
        const e = value.end ? new Date(value.end) : new Date();
        setStart(s);
        setEnd(e);
        setStage(0);
        setView(new Date(s.getFullYear(), s.getMonth(), 1));
        setOpen(true);
    }

    // Close on outside click / Escape.
    useEffect(() => {
        if (!open) return;
        const onDoc = (e: MouseEvent) => {
            if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
        };
        const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
        document.addEventListener("mousedown", onDoc);
        document.addEventListener("keydown", onKey);
        return () => {
            document.removeEventListener("mousedown", onDoc);
            document.removeEventListener("keydown", onKey);
        };
    }, [open]);

    function applyPreset(p: Preset) {
        onChange(presetRange(p, new Date()));
        setOpen(false);
    }

    function commit(s: Date, e: Date) {
        const [lo, hi] = s <= e ? [s, e] : [e, s];
        onChange({
            start: lo.toISOString(),
            end: hi.toISOString(),
            label: `${fmtStamp(lo)} → ${fmtStamp(hi)}`,
        });
    }

    function onDayClick(day: Date) {
        if (stage === 0) {
            const s = new Date(day);
            s.setHours(start.getHours(), start.getMinutes());
            setStart(s);
            setEnd(null);
            setStage(1);
        } else {
            const e = new Date(day);
            e.setHours((end ?? new Date()).getHours(), (end ?? new Date()).getMinutes());
            setEnd(e);
            setStage(0);
            commit(start, e);
        }
    }

    function onTimeChange(which: "start" | "end", h: number, m: number) {
        if (which === "start") {
            const s = new Date(start);
            s.setHours(h, m);
            setStart(s);
            if (end) commit(s, end);
        } else {
            const base = end ?? new Date();
            const e = new Date(base);
            e.setHours(h, m);
            setEnd(e);
            commit(start, e);
        }
    }

    const months = useMemo(() => [view, addMonths(view, 1)], [view]);
    const today = new Date();

    return (
        <div className="relative" ref={wrapRef}>
            <button
                type="button"
                aria-haspopup="dialog"
                aria-expanded={open}
                onClick={toggleOpen}
                className="border-ink-700 bg-ink-900 text-fog-100 hover:bg-ink-850 inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-sm font-medium"
            >
                <svg
                    width="14"
                    height="14"
                    viewBox="0 0 16 16"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.4"
                    className="text-fog-400"
                >
                    <rect x="2.5" y="3.5" width="11" height="10" rx="1.5" />
                    <path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" strokeLinecap="round" />
                </svg>
                <span>{value.label}</span>
            </button>

            {open ? (
                <div
                    role="dialog"
                    aria-label="Select date range"
                    className="border-ink-700 bg-ink-850 fixed inset-x-3 top-3 z-50 flex max-h-[calc(100dvh-1.5rem)] flex-col overflow-y-auto rounded-xl border shadow-[var(--shadow-pop)] md:absolute md:inset-x-auto md:top-full md:right-0 md:mt-2 md:max-h-none md:flex-row md:overflow-visible"
                >
                    <div className="p-3 md:p-4">
                        <div className="mb-2.5 flex items-center justify-between">
                            <CalNav
                                label="Previous month"
                                onClick={() => setView(addMonths(view, -1))}
                            >
                                ‹
                            </CalNav>
                            <div className="flex min-w-0 flex-1 justify-around text-sm font-semibold">
                                <span>
                                    {MONTHS[months[0].getMonth()]} {months[0].getFullYear()}
                                </span>
                                <span className="hidden md:inline">
                                    {MONTHS[months[1].getMonth()]} {months[1].getFullYear()}
                                </span>
                            </div>
                            <CalNav label="Next month" onClick={() => setView(addMonths(view, 1))}>
                                ›
                            </CalNav>
                        </div>

                        <div className="flex justify-center gap-7">
                            {months.map((m, index) => (
                                <div
                                    key={`${m.getFullYear()}-${m.getMonth()}`}
                                    className={index > 0 ? "hidden md:block" : "w-full md:w-auto"}
                                >
                                    <Month
                                        monthDate={m}
                                        start={start}
                                        end={end}
                                        today={today}
                                        onDayClick={onDayClick}
                                    />
                                </div>
                            ))}
                        </div>

                        <div className="border-ink-700 mt-3.5 grid gap-3 border-t pt-3.5 min-[520px]:grid-cols-2 md:flex md:gap-8">
                            <TimeField
                                label="From Time"
                                date={start}
                                onChange={(h, m) => onTimeChange("start", h, m)}
                            />
                            <TimeField
                                label="To Time"
                                date={end ?? start}
                                onChange={(h, m) => onTimeChange("end", h, m)}
                            />
                        </div>
                    </div>

                    <div className="border-ink-700 grid grid-cols-2 gap-0.5 border-t p-3 md:flex md:min-w-[150px] md:flex-col md:border-t-0 md:border-l">
                        {PRESETS.map((p) => {
                            const active = value.label === p.label;
                            return (
                                <button
                                    key={p.label}
                                    type="button"
                                    onClick={() => applyPreset(p)}
                                    aria-pressed={active}
                                    className={`rounded-lg px-3 py-2 text-left text-sm whitespace-nowrap ${
                                        active
                                            ? "bg-ink-700 text-fog-100 font-medium"
                                            : "text-fog-200 hover:bg-ink-800"
                                    }`}
                                >
                                    {p.label}
                                </button>
                            );
                        })}
                    </div>
                </div>
            ) : null}
        </div>
    );
}

function CalNav({
    children,
    label,
    onClick,
}: {
    children: React.ReactNode;
    label: string;
    onClick: () => void;
}) {
    return (
        <button
            type="button"
            aria-label={label}
            onClick={onClick}
            className="border-ink-700 bg-ink-900 text-fog-300 hover:bg-ink-800 inline-flex h-7 w-7 items-center justify-center rounded-md border"
        >
            {children}
        </button>
    );
}

function Month({
    monthDate,
    start,
    end,
    today,
    onDayClick,
}: {
    monthDate: Date;
    start: Date;
    end: Date | null;
    today: Date;
    onDayClick: (d: Date) => void;
}) {
    const y = monthDate.getFullYear();
    const m = monthDate.getMonth();
    const cells = monthCells(y, m);
    const lo = start;
    const hi = end;

    return (
        <div className="mx-auto w-full max-w-[280px] md:w-[238px]">
            <div className="mb-1 grid grid-cols-7">
                {DOW.map((d) => (
                    <span key={d} className="text-fog-400 py-1 text-center text-[11px]">
                        {d}
                    </span>
                ))}
            </div>
            <div className="grid grid-cols-7">
                {cells.map((date, i) => {
                    const inMonth = date.getMonth() === m;
                    const isStart = sameDay(date, lo);
                    const isEnd = hi != null && sameDay(date, hi);
                    const inRange =
                        hi != null &&
                        startOfDay(date) > startOfDay(lo) &&
                        startOfDay(date) < startOfDay(hi);
                    const isToday = sameDay(date, today);

                    let cls =
                        "relative flex h-8 items-center justify-center text-[12.5px] tabular-nums ";
                    if (!inMonth) {
                        cls += "text-ink-600 ";
                    } else if (isStart || isEnd) {
                        cls += "bg-brand-500 text-ink-950 font-semibold rounded-md ";
                    } else if (inRange) {
                        cls += "bg-brand-500/15 text-fog-200 ";
                    } else {
                        cls += "text-fog-200 hover:bg-ink-800 hover:rounded-md cursor-pointer ";
                    }

                    return (
                        <button
                            key={i}
                            type="button"
                            disabled={!inMonth}
                            onClick={() => inMonth && onDayClick(date)}
                            className={cls}
                        >
                            {date.getDate()}
                            {isToday && !isStart && !isEnd ? (
                                <span className="bg-brand-400 absolute bottom-1 h-1 w-1 rounded-full" />
                            ) : null}
                        </button>
                    );
                })}
            </div>
        </div>
    );
}

function TimeField({
    label,
    date,
    onChange,
}: {
    label: string;
    date: Date;
    onChange: (hours24: number, minutes: number) => void;
}) {
    const h12 = date.getHours() % 12 || 12;
    const pm = date.getHours() >= 12;
    const minutes = date.getMinutes();

    const to24 = (h: number, isPm: boolean) => (h % 12) + (isPm ? 12 : 0);
    const selectCls =
        "border-ink-600 bg-ink-900 text-fog-100 rounded-md border px-1.5 py-1.5 text-[12.5px]";

    return (
        <div>
            <div className="text-fog-400 mb-1.5 text-[11px]">{label}</div>
            <div className="flex items-center gap-1">
                <select
                    aria-label={`${label} hour`}
                    value={h12}
                    onChange={(e) => onChange(to24(Number(e.target.value), pm), minutes)}
                    className={selectCls}
                >
                    {Array.from({ length: 12 }, (_, i) => i + 1).map((h) => (
                        <option key={h} value={h}>
                            {String(h).padStart(2, "0")}
                        </option>
                    ))}
                </select>
                <span className="text-fog-400">:</span>
                <select
                    aria-label={`${label} minute`}
                    value={minutes}
                    onChange={(e) => onChange(date.getHours(), Number(e.target.value))}
                    className={selectCls}
                >
                    {Array.from({ length: 60 }, (_, i) => i).map((mm) => (
                        <option key={mm} value={mm}>
                            {String(mm).padStart(2, "0")}
                        </option>
                    ))}
                </select>
                <select
                    aria-label={`${label} meridiem`}
                    value={pm ? "pm" : "am"}
                    onChange={(e) => onChange(to24(h12, e.target.value === "pm"), minutes)}
                    className={selectCls}
                >
                    <option value="am">AM</option>
                    <option value="pm">PM</option>
                </select>
            </div>
        </div>
    );
}
