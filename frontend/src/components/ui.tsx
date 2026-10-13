"use client";

// ---------------------------------------------------------------------------
// Small, dependency-free UI primitives shared across pages.
// ---------------------------------------------------------------------------

import {
    Check,
    ChevronDown,
    ChevronLeft,
    ChevronRight,
    Copy,
    ListChecks,
    TriangleAlert,
    X,
} from "lucide-react";
import { ReactNode, useEffect, useId, useRef, useState } from "react";

// --- Button ----------------------------------------------------------------

type ButtonVariant = "primary" | "ghost" | "danger" | "subtle" | "success";

const BUTTON_STYLES: Record<ButtonVariant, string> = {
    // Near-black label on coral — high-contrast (AA) and reads as the primary CTA.
    primary: "bg-brand-500 text-ink-950 hover:bg-brand-400 border border-brand-400/40 shadow-sm",
    ghost: "bg-transparent text-fog-200 hover:bg-ink-800 border border-ink-600",
    subtle: "bg-ink-800 text-fog-100 hover:bg-ink-700 border border-ink-600",
    danger: "bg-bad-500/10 text-bad-500 hover:bg-bad-500/15 border border-bad-500/30",
    success: "bg-good-500/15 text-good-500 hover:bg-good-500/20 border border-good-500/35",
};

export function Button({
    children,
    variant = "subtle",
    className = "",
    ...props
}: {
    children: ReactNode;
    variant?: ButtonVariant;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
    return (
        <button
            className={`inline-flex min-h-9 items-center justify-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-medium whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${BUTTON_STYLES[variant]} ${className}`}
            {...props}
        >
            {children}
        </button>
    );
}

// --- Select menu -----------------------------------------------------------

export function SelectMenu({
    value,
    options,
    onChange,
    ariaLabel,
    className = "",
}: {
    value: string;
    options: { value: string; label: string }[];
    onChange: (value: string) => void;
    ariaLabel: string;
    className?: string;
}) {
    const [open, setOpen] = useState(false);
    const [openUp, setOpenUp] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const selected = options.find((option) => option.value === value);

    useEffect(() => {
        if (!open) return;
        const onPointerDown = (event: MouseEvent) => {
            if (!ref.current?.contains(event.target as Node)) setOpen(false);
        };
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") setOpen(false);
        };
        document.addEventListener("mousedown", onPointerDown);
        document.addEventListener("keydown", onKeyDown);
        return () => {
            document.removeEventListener("mousedown", onPointerDown);
            document.removeEventListener("keydown", onKeyDown);
        };
    }, [open]);

    return (
        <div ref={ref} className={`relative ${className}`}>
            <button
                type="button"
                aria-label={ariaLabel}
                aria-haspopup="listbox"
                aria-expanded={open}
                onClick={() => {
                    setOpenUp(
                        (ref.current?.getBoundingClientRect().bottom ?? 0) + 290 >
                            window.innerHeight &&
                            (ref.current?.getBoundingClientRect().top ?? 0) > 290,
                    );
                    setOpen((current) => !current);
                }}
                className="border-ink-600 bg-ink-900 text-fog-100 hover:bg-ink-800 focus:border-brand-400 focus:ring-brand-500/25 flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-sm transition-colors outline-none focus:ring-2"
            >
                <span className="truncate">{selected?.label ?? "Select"}</span>
                <ChevronDown
                    size={15}
                    className={`text-fog-400 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
                />
            </button>
            {open ? (
                <div
                    role="listbox"
                    className={`border-ink-600 bg-ink-850 absolute right-0 left-0 z-50 max-h-72 overflow-y-auto rounded-lg border py-1 shadow-[var(--shadow-pop)] ${openUp ? "bottom-full mb-1" : "top-full mt-1"}`}
                >
                    {options.map((option) => {
                        const active = option.value === value;
                        return (
                            <button
                                key={option.value}
                                type="button"
                                role="option"
                                aria-selected={active}
                                onClick={() => {
                                    onChange(option.value);
                                    setOpen(false);
                                }}
                                className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm ${active ? "bg-brand-500/15 text-brand-300" : "text-fog-200 hover:bg-ink-700"}`}
                            >
                                {active ? <Check size={14} /> : <span className="w-3.5" />}
                                <span>{option.label}</span>
                            </button>
                        );
                    })}
                </div>
            ) : null}
        </div>
    );
}

// --- Pagination ------------------------------------------------------------

export function Pagination({
    total,
    page,
    pageSize,
    pageSizeOptions = [25, 50, 75, 100],
    onPageChange,
    onPageSizeChange,
    disabled = false,
}: {
    total: number;
    page: number;
    pageSize: number;
    pageSizeOptions?: number[];
    onPageChange: (page: number) => void;
    onPageSizeChange?: (pageSize: number) => void;
    disabled?: boolean;
}) {
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    // The typed page number is a draft tied to the page it was typed on; when the
    // page changes from outside (Previous/Next, filters) the input follows it.
    const [draft, setDraft] = useState({ page, text: String(page) });
    const draftPage = draft.page === page ? draft.text : String(page);
    const setDraftPage = (text: string) => setDraft({ page, text });

    const goToPage = () => {
        const parsed = Number(draftPage);
        const nextPage = Number.isFinite(parsed)
            ? Math.min(totalPages, Math.max(1, Math.trunc(parsed)))
            : page;
        setDraftPage(String(nextPage));
        if (nextPage !== page) onPageChange(nextPage);
    };

    return (
        <nav
            aria-label="Pagination"
            className="text-fog-300 flex flex-wrap items-center justify-end gap-2.5 text-xs"
        >
            {onPageSizeChange ? (
                <label className="text-fog-400 flex items-center gap-2">
                    <span>Rows</span>
                    <SelectMenu
                        value={String(pageSize)}
                        options={pageSizeOptions.map((option) => ({
                            value: String(option),
                            label: String(option),
                        }))}
                        onChange={(value) => onPageSizeChange(Number(value))}
                        ariaLabel="Rows per page"
                        className="w-[4.75rem]"
                    />
                </label>
            ) : null}
            <Button
                variant="ghost"
                disabled={disabled || page <= 1}
                onClick={() => onPageChange(page - 1)}
                className="h-9 gap-1.5 px-3"
            >
                <ChevronLeft size={15} aria-hidden="true" />
                Previous
            </Button>
            <label className="text-fog-400 inline-flex h-9 items-center gap-1.5 whitespace-nowrap">
                <span>Page</span>
                <input
                    type="number"
                    min={1}
                    max={totalPages}
                    inputMode="numeric"
                    value={draftPage}
                    onChange={(event) => setDraftPage(event.target.value)}
                    onKeyDown={(event) => {
                        if (event.key === "Enter") {
                            event.preventDefault();
                            goToPage();
                        }
                    }}
                    aria-label="Current page"
                    className="border-ink-600 bg-ink-900 text-fog-100 focus:border-brand-400 focus:ring-brand-500/25 h-9 w-14 rounded-md border px-2 text-center text-sm tabular-nums transition-colors outline-none focus:ring-2"
                />
                <span>of {totalPages}</span>
            </label>
            <Button
                variant="ghost"
                disabled={disabled || page >= totalPages}
                onClick={() => onPageChange(page + 1)}
                className="h-9 gap-1.5 px-3"
            >
                Next
                <ChevronRight size={15} aria-hidden="true" />
            </Button>
        </nav>
    );
}

// --- Card ------------------------------------------------------------------

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
    return (
        <div
            className={`border-ink-700 bg-ink-850 rounded-xl border shadow-[var(--shadow-card)] ${className}`}
        >
            {children}
        </div>
    );
}

// --- Stat card -------------------------------------------------------------

export function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
    return (
        <Card className="p-5">
            <div className="text-fog-400 text-xs font-medium tracking-wider uppercase">{label}</div>

            <div className="text-fog-100 mt-2.5 text-[1.7rem] leading-none font-semibold tabular-nums">
                {value}
            </div>

            {hint ? <div className="text-fog-400 mt-1.5 text-xs">{hint}</div> : null}
        </Card>
    );
}

// --- Badge -----------------------------------------------------------------

type BadgeTone = "good" | "warn" | "bad" | "neutral" | "brand";

const BADGE_STYLES: Record<BadgeTone, string> = {
    good: "bg-good-500/15 text-good-500 border-good-500/30",
    warn: "bg-warn-500/15 text-warn-500 border-warn-500/30",
    bad: "bg-bad-500/15 text-bad-500 border-bad-500/30",
    neutral: "bg-ink-700 text-fog-300 border-ink-600",
    brand: "bg-brand-500/15 text-brand-300 border-brand-400/30",
};

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: BadgeTone }) {
    return (
        <span
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${BADGE_STYLES[tone]}`}
        >
            {children}
        </span>
    );
}

// --- Usage bar -------------------------------------------------------------

// Renders a labelled progress bar from a 0..1 fraction (null = unknown).
export function UsageBar({ label, fraction }: { label: string; fraction: number | null }) {
    const pct = fraction === null ? null : Math.min(Math.max(fraction, 0), 1);

    // Colour ramps from brand -> warn -> bad as utilization climbs.
    let barColor = "bg-brand-500";
    if (pct !== null && pct >= 0.9) barColor = "bg-bad-500";
    else if (pct !== null && pct >= 0.7) barColor = "bg-warn-500";

    return (
        <div className="w-full">
            <div className="mb-1 flex items-center justify-between text-xs">
                <span className="text-fog-400">{label}</span>
                <span className="text-fog-300 tabular-nums">
                    {pct === null ? "unknown" : `${Math.round(pct * 100)}%`}
                </span>
            </div>

            <div className="bg-ink-700 h-1.5 w-full overflow-hidden rounded-full">
                {pct === null ? (
                    <div className="bg-ink-600/40 h-full w-full" />
                ) : (
                    <div
                        className={`h-full rounded-full ${barColor} transition-all`}
                        style={{ width: `${pct * 100}%` }}
                    />
                )}
            </div>
        </div>
    );
}

// --- Status toggle ---------------------------------------------------------

// A clearly-labelled on/off switch (the knob slides and a text label states the
// current state) used for enabling/disabling users and keys.
export function StatusToggle({
    on,
    busy = false,
    onClick,
    onLabel = "Active",
    offLabel = "Disabled",
    srLabel,
}: {
    on: boolean;
    busy?: boolean;
    onClick: () => void;
    onLabel?: string;
    offLabel?: string;
    srLabel: string;
}) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label={srLabel}
            disabled={busy}
            onClick={onClick}
            className="group inline-flex items-center gap-2 disabled:cursor-not-allowed disabled:opacity-60"
        >
            <span
                className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors ${
                    on
                        ? "bg-good-500/90 border-good-500/40"
                        : "border-ink-600 bg-ink-700 group-hover:bg-ink-600"
                }`}
            >
                <span
                    className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform ${
                        on ? "translate-x-[1.4rem]" : "translate-x-0.5"
                    }`}
                />
            </span>
            <span className={`text-xs font-medium ${on ? "text-good-500" : "text-fog-400"}`}>
                {on ? onLabel : offLabel}
            </span>
        </button>
    );
}

// --- Segmented control -----------------------------------------------------

// A compact pill group for switching between a few options (e.g. metric or
// chart type). Values are plain strings.
export function Segmented<T extends string>({
    options,
    value,
    onChange,
    size = "sm",
    label,
}: {
    options: { value: T; label: ReactNode }[];
    value: T;
    onChange: (value: T) => void;
    size?: "sm" | "xs";
    label?: string;
}) {
    const pad = size === "xs" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs";
    return (
        <div
            role="group"
            aria-label={label}
            className="border-ink-700 bg-ink-900 inline-flex rounded-lg border p-0.5"
        >
            {options.map((opt) => {
                const active = opt.value === value;
                return (
                    <button
                        key={opt.value}
                        type="button"
                        onClick={() => onChange(opt.value)}
                        aria-pressed={active}
                        className={`rounded-md font-medium transition-colors ${pad} ${
                            active ? "bg-ink-700 text-fog-100" : "text-fog-400 hover:text-fog-200"
                        }`}
                    >
                        {opt.label}
                    </button>
                );
            })}
        </div>
    );
}

// --- Bulk priority command bar -------------------------------------------

/** Keep bulk actions reachable while selecting cards in long priority lanes. */
export function BulkPriorityBar({
    entityLabel,
    selectedCount,
    visibleCount,
    priority,
    onPriorityChange,
    onSelectVisible,
    onApply,
    onCancel,
    onClose,
    applying = false,
}: {
    entityLabel: string;
    selectedCount: number;
    visibleCount: number;
    priority: string;
    onPriorityChange: (value: string) => void;
    onSelectVisible: () => void;
    onApply: () => void;
    onCancel: () => void;
    onClose: () => void;
    applying?: boolean;
}) {
    const allVisibleSelected = selectedCount > 0 && selectedCount === visibleCount;
    const parsedPriority = Number(priority);
    const canApply = selectedCount > 0 && Number.isInteger(parsedPriority) && parsedPriority >= 1;

    return (
        <div className="border-brand-500/35 bg-ink-900/95 sticky top-3 z-20 overflow-hidden rounded-xl border shadow-[var(--shadow-pop)] backdrop-blur">
            <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                <div className="flex min-w-0 items-center gap-3">
                    <span className="bg-brand-500/15 text-brand-300 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg">
                        <ListChecks size={18} aria-hidden="true" />
                    </span>
                    <div className="min-w-0">
                        <div className="text-fog-100 text-sm font-semibold">Bulk priority</div>
                        <div className="text-fog-400 truncate text-xs">
                            {selectedCount > 0
                                ? `${selectedCount} ${entityLabel}${selectedCount === 1 ? "" : "s"} selected`
                                : `Select ${entityLabel}s to assign them together`}
                        </div>
                    </div>
                </div>
                <div className="flex items-center gap-2">
                    <Button variant="ghost" onClick={onSelectVisible} disabled={visibleCount === 0}>
                        {allVisibleSelected ? "Clear visible" : "Select visible"}
                    </Button>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-fog-400 hover:bg-ink-800 hover:text-fog-100 inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors"
                        aria-label="Exit bulk priority mode"
                    >
                        <X size={16} aria-hidden="true" />
                    </button>
                </div>
            </div>
            <div className="border-ink-700 bg-ink-950/45 flex flex-wrap items-center gap-3 border-t px-4 py-3">
                <span className="text-fog-400 text-xs font-medium tracking-wide uppercase">
                    Assign priority
                </span>
                <div
                    className="border-ink-700 bg-ink-900 inline-flex rounded-lg border p-0.5"
                    role="group"
                    aria-label="Quick priority values"
                >
                    {[1, 2, 3, 4].map((value) => {
                        const active = parsedPriority === value;
                        return (
                            <button
                                key={value}
                                type="button"
                                onClick={() => onPriorityChange(String(value))}
                                aria-pressed={active}
                                className={`rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${active ? "bg-brand-500 text-ink-950" : "text-fog-300 hover:bg-ink-800 hover:text-fog-100"}`}
                            >
                                {value}
                            </button>
                        );
                    })}
                </div>
                <label className="text-fog-400 flex items-center gap-2 text-xs">
                    <span>Custom</span>
                    <input
                        type="number"
                        min={1}
                        step={1}
                        value={priority}
                        onChange={(event) => onPriorityChange(event.target.value)}
                        aria-label="Custom priority"
                        className="border-ink-600 bg-ink-900 text-fog-100 focus:border-brand-400 w-20 rounded-md border px-2.5 py-1.5 text-center font-mono text-xs outline-none"
                    />
                </label>
                <div className="ml-auto flex items-center gap-2">
                    <Button variant="ghost" onClick={onCancel}>
                        Cancel
                    </Button>
                    <Button variant="primary" disabled={!canApply || applying} onClick={onApply}>
                        {applying ? <Spinner /> : null} Apply to {selectedCount || "selected"}
                    </Button>
                </div>
            </div>
        </div>
    );
}

// --- Spinner / states ------------------------------------------------------

export function Spinner({ className = "" }: { className?: string }) {
    return (
        <span
            className={`border-fog-400 inline-block h-4 w-4 animate-spin rounded-full border-2 border-t-transparent ${className}`}
            aria-hidden
        />
    );
}

export function LoadingState({ label = "Loading…" }: { label?: string }) {
    return (
        <div className="text-fog-400 flex items-center justify-center gap-3 py-16 text-sm">
            <Spinner />
            {label}
        </div>
    );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
    return (
        <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
            <div className="text-bad-500 text-sm">{message}</div>
            {onRetry ? (
                <Button variant="subtle" onClick={onRetry}>
                    Retry
                </Button>
            ) : null}
        </div>
    );
}

export function EmptyState({ message }: { message: string }) {
    return (
        <div className="text-fog-400 flex items-center justify-center py-16 text-sm">{message}</div>
    );
}

// --- Modal -----------------------------------------------------------------

export function Modal({
    title,
    children,
    onClose,
    widthClass = "max-w-lg",
}: {
    title: string;
    children: ReactNode;
    onClose: () => void;
    widthClass?: string;
}) {
    const panelRef = useRef<HTMLDivElement>(null);
    const titleId = useId();

    // Move focus into the dialog on open and restore it to the trigger on close.
    useEffect(() => {
        const previouslyFocused = document.activeElement as HTMLElement | null;
        const panel = panelRef.current;
        const first = panel?.querySelector<HTMLElement>(
            'a[href], button:not([disabled]), textarea, input:not([disabled]), select, [tabindex]:not([tabindex="-1"])',
        );
        (first ?? panel)?.focus();
        return () => previouslyFocused?.focus?.();
    }, []);

    // Close on Escape; trap Tab within the dialog.
    useEffect(() => {
        const handler = (e: KeyboardEvent) => {
            if (e.key === "Escape") {
                onClose();
                return;
            }
            if (e.key === "Tab" && panelRef.current) {
                const f = Array.from(
                    panelRef.current.querySelectorAll<HTMLElement>(
                        'a[href], button:not([disabled]), textarea, input:not([disabled]), select, [tabindex]:not([tabindex="-1"])',
                    ),
                ).filter((el) => el.offsetParent !== null);
                if (f.length === 0) return;
                const first = f[0];
                const last = f[f.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };
        window.addEventListener("keydown", handler);
        return () => window.removeEventListener("keydown", handler);
    }, [onClose]);

    return (
        <div
            className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-20 backdrop-blur-sm"
            onClick={onClose}
        >
            <div
                ref={panelRef}
                role="dialog"
                aria-modal="true"
                aria-labelledby={titleId}
                tabIndex={-1}
                className={`w-full ${widthClass} border-ink-700 bg-ink-850 rounded-xl border shadow-[var(--shadow-pop)] outline-none`}
                onClick={(e) => e.stopPropagation()}
            >
                <div className="border-ink-700 flex items-center justify-between border-b px-5 py-3.5">
                    <h2 id={titleId} className="text-fog-100 text-sm font-semibold">
                        {title}
                    </h2>
                    <button
                        onClick={onClose}
                        className="text-fog-400 hover:bg-ink-700 hover:text-fog-100 rounded p-1 transition-colors"
                        aria-label="Close"
                    >
                        <svg
                            width="16"
                            height="16"
                            viewBox="0 0 16 16"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.5"
                        >
                            <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
                        </svg>
                    </button>
                </div>

                <div className="p-5">{children}</div>
            </div>
        </div>
    );
}

// --- Confirm dialog --------------------------------------------------------

export function ConfirmDialog({
    title,
    message,
    confirmLabel = "Confirm",
    busy = false,
    onConfirm,
    onCancel,
}: {
    title: string;
    message: string;
    confirmLabel?: string;
    busy?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}) {
    return (
        <Modal title={title} onClose={onCancel} widthClass="max-w-md">
            <p className="text-fog-300 text-sm leading-relaxed">{message}</p>

            <div className="mt-6 flex justify-end gap-2">
                <Button variant="ghost" onClick={onCancel} disabled={busy}>
                    Cancel
                </Button>
                <Button variant="danger" onClick={onConfirm} disabled={busy}>
                    {busy ? <Spinner /> : null}
                    {confirmLabel}
                </Button>
            </div>
        </Modal>
    );
}

// --- Text input / label ----------------------------------------------------

export function Field({
    label,
    children,
    hint,
}: {
    label: string;
    children: ReactNode;
    hint?: string;
}) {
    return (
        <label className="block">
            <span className="text-fog-300 mb-1.5 block text-xs font-medium">{label}</span>
            {children}
            {hint ? <span className="text-fog-400 mt-1 block text-xs">{hint}</span> : null}
        </label>
    );
}

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
    return (
        <input
            {...props}
            className={`border-ink-600 bg-ink-900 text-fog-100 placeholder:text-fog-400 focus:border-brand-400 focus:ring-brand-500/25 w-full rounded-md border px-3 py-2 text-sm transition-colors outline-none focus:ring-2 ${props.className ?? ""}`}
        />
    );
}

// --- Copy button -----------------------------------------------------------

export function CopyButton({
    text,
    label = "Copy",
    variant = "subtle",
}: {
    text: string;
    label?: string;
    variant?: ButtonVariant;
}) {
    const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
    const resetTimer = useRef<number | null>(null);

    useEffect(
        () => () => {
            if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
        },
        [],
    );

    const showCopyState = (state: "copied" | "failed") => {
        setCopyState(state);
        if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
        resetTimer.current = window.setTimeout(() => setCopyState("idle"), 2000);
    };

    const onCopy = async () => {
        try {
            await navigator.clipboard.writeText(text);
            showCopyState("copied");
        } catch {
            showCopyState("failed");
        }
    };

    return (
        <Button
            variant={
                copyState === "copied" ? "success" : copyState === "failed" ? "danger" : variant
            }
            onClick={onCopy}
            type="button"
            className="min-w-[5.25rem]"
        >
            <span aria-live="polite" className="inline-flex items-center gap-1.5">
                {copyState === "copied" ? (
                    <>
                        <Check size={14} aria-hidden="true" /> Copied
                    </>
                ) : copyState === "failed" ? (
                    <>
                        <TriangleAlert size={14} aria-hidden="true" /> Failed
                    </>
                ) : (
                    <>
                        <Copy size={14} aria-hidden="true" /> {label}
                    </>
                )}
            </span>
        </Button>
    );
}
