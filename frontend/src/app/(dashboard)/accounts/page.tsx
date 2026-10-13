"use client";

// ---------------------------------------------------------------------------
// Accounts page: pooled Claude subscription accounts with utilization, reset
// countdowns, per-account actions (edit / refresh / enable-disable / delete),
// and an OAuth "add account" flow.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, Check, ListChecks } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Account, EgressTarget, ProviderHealth } from "@/lib/types";
import { formatCountdown, formatDateTime, formatUsd, tierLabel } from "@/lib/format";
import {
    Badge,
    Button,
    BulkPriorityBar,
    Card,
    ConfirmDialog,
    EmptyState,
    ErrorState,
    Field,
    LoadingState,
    Modal,
    Segmented,
    SelectMenu,
    Spinner,
    TextInput,
    UsageBar,
} from "@/components/ui";

type AccountFilter = "all" | "authenticated" | "usable";
const ACCOUNT_FILTER_OPTIONS: { value: AccountFilter; label: string }[] = [
    { value: "all", label: "All accounts" },
    { value: "authenticated", label: "Authenticated accounts" },
    { value: "usable", label: "Usable accounts" },
];

// Parse an optional float field; blank -> null (reset to the global default).
function parseOptionalFloat(value: string): number | null {
    const t = value.trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
}

// Parse an optional non-negative integer field; blank -> null (reset to default).
function parseOptionalInt(value: string): number | null {
    const t = value.trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null;
}

// Map account status -> badge tone + label.
function cooldownIsActive(account: Account, now = Date.now()): boolean {
    return (
        account.status === "COOLDOWN" &&
        account.cooldown_until !== null &&
        new Date(account.cooldown_until).getTime() > now
    );
}

type ExhaustedQuotaWindow = {
    label: string;
    resetAt: string | null;
    fraction: number | null;
};

/**
 * COOLDOWN is also used internally to keep a quota-exhausted account out of
 * rotation until the provider reset. That state is a hard limit, not a
 * transient upstream cooldown, so it gets its own dashboard presentation.
 */
function exhaustedQuotaWindow(account: Account): ExhaustedQuotaWindow | null {
    const windows: ExhaustedQuotaWindow[] = [
        {
            label: "5-hour limit",
            resetAt: account.session_reset_at,
            fraction: account.session_used_pct,
        },
        {
            label: "Weekly limit",
            resetAt: account.weekly_reset_at,
            fraction: account.weekly_used_pct,
        },
        {
            label: "Monthly limit",
            resetAt: account.monthly_reset_at,
            fraction: account.monthly_used_pct,
        },
    ];
    const exhausted = windows.filter((window) => window.fraction !== null && window.fraction >= 1);
    if (exhausted.length === 0) return null;

    return exhausted.reduce((latest, window) => {
        if (!latest.resetAt) return window;
        if (!window.resetAt) return latest;
        return Date.parse(window.resetAt) > Date.parse(latest.resetAt) ? window : latest;
    });
}

function statusBadge(account: Account) {
    if (account.status === "DISABLED") return <Badge tone="bad">Disabled</Badge>;
    if (exhaustedQuotaWindow(account)) return <Badge tone="bad">Limit exhausted</Badge>;
    if (cooldownIsActive(account)) return <Badge tone="warn">Cooldown</Badge>;
    return <Badge tone="good">Active</Badge>;
}

function healthIsStale(account: Account) {
    if (!account.provider_health_checked_at) return true;
    return Date.now() - new Date(account.provider_health_checked_at).getTime() > 150_000;
}

function healthBadge(account: Account) {
    const health: ProviderHealth = account.provider_health;
    if (health === "REAUTH_REQUIRED") return <Badge tone="bad">Re-auth required</Badge>;
    return null;
}

function compareAccountsForDisplay(a: Account, b: Account): number {
    const aNeedsReauth = a.provider_health === "REAUTH_REQUIRED";
    const bNeedsReauth = b.provider_health === "REAUTH_REQUIRED";
    if (aNeedsReauth !== bNeedsReauth) return aNeedsReauth ? -1 : 1;
    return compareAccountsForRotation(a, b);
}

function accountIsUsable(account: Account, now = Date.now()): boolean {
    if (account.status === "DISABLED") return false;
    // The router still permits DEGRADED/UNKNOWN accounts when their normal
    // provider traffic can work; only re-authentication is a hard block.
    if (account.provider_health === "REAUTH_REQUIRED") return false;
    if (account.cooldown_until && new Date(account.cooldown_until).getTime() > now) return false;
    return (
        (account.session_used_pct ?? 0) < account.five_hour_rotation_threshold &&
        (account.weekly_used_pct ?? 0) < account.weekly_rotation_threshold &&
        (account.monthly_used_pct ?? 0) < 1
    );
}

function accountQuotaWindows(account: Account) {
    return [
        {
            key: "five_hour",
            label: "5-hour window",
            fraction: account.session_used_pct,
            resetAt: account.session_reset_at,
        },
        {
            key: "weekly",
            label: "Weekly window",
            fraction: account.weekly_used_pct,
            resetAt: account.weekly_reset_at,
        },
        {
            key: "monthly",
            label: "Monthly window",
            fraction: account.monthly_used_pct,
            resetAt: account.monthly_reset_at,
        },
    ].filter((window) => window.fraction !== null || window.resetAt !== null);
}

function appearsAuthenticated(account: Account): boolean {
    return account.authenticated_override || account.provider_health !== "REAUTH_REQUIRED";
}

/**
 * Mirror backend account_selection_key for display ordering. Priority remains
 * the primary order; accounts sharing a priority are then ordered by the
 * earliest known weekly reset, followed by the earliest 5-hour reset. Unknown
 * reset times sort last, with creation time and id providing stable ties.
 */
function accountSelectionSortKey(value: string | null): [number, number] {
    if (!value) return [1, Number.POSITIVE_INFINITY];
    const timestamp = Date.parse(value);
    return Number.isFinite(timestamp) ? [0, timestamp] : [1, Number.POSITIVE_INFINITY];
}

function compareAccountsForRotation(a: Account, b: Account): number {
    const priority = a.priority - b.priority;
    if (priority !== 0) return priority;

    for (const [aReset, bReset] of [
        [a.weekly_reset_at, b.weekly_reset_at],
        [a.session_reset_at, b.session_reset_at],
        [a.monthly_reset_at, b.monthly_reset_at],
    ] as const) {
        const [aKnown, aTimestamp] = accountSelectionSortKey(aReset);
        const [bKnown, bTimestamp] = accountSelectionSortKey(bReset);
        if (aKnown !== bKnown) return aKnown - bKnown;
        if (aTimestamp !== bTimestamp) return aTimestamp - bTimestamp;
    }

    const aCreated = Date.parse(a.created_at);
    const bCreated = Date.parse(b.created_at);
    if (Number.isFinite(aCreated) && Number.isFinite(bCreated) && aCreated !== bCreated) {
        return aCreated - bCreated;
    }
    return a.id.localeCompare(b.id);
}

function egressTargetLabel(account: Account, targets: EgressTarget[]): string {
    const target = targets.find((candidate) => candidate.id === account.egress_target_id);
    if (!target) return "First configured path";
    if (target.interface_name && target.private_ip) {
        return `${target.interface_name} · ${target.private_ip}${target.public_ip ? ` → ${target.public_ip}` : ""}`;
    }
    return target.label;
}

export default function AccountsPage() {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const queryString = searchParams.toString();
    const [accounts, setAccounts] = useState<Account[] | null>(null);
    const [egressTargets, setEgressTargets] = useState<EgressTarget[]>([]);
    const [savedPriorities, setSavedPriorities] = useState<Record<string, number>>({});
    const [draggedId, setDraggedId] = useState<string | null>(null);
    const [savingPriority, setSavingPriority] = useState(false);
    const [selectedAccountIds, setSelectedAccountIds] = useState<string[]>([]);
    const [selectionMode, setSelectionMode] = useState(false);
    const [healthDetailsId, setHealthDetailsId] = useState<string | null>(null);
    const [bulkPriority, setBulkPriority] = useState("1");
    const [applyingBulkPriority, setApplyingBulkPriority] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);

    // Per-account in-flight action id (so spinners only show on the right row).
    const [busyId, setBusyId] = useState<string | null>(null);

    // Modal targets.
    const [editTarget, setEditTarget] = useState<Account | null>(null);
    const [reauthTarget, setReauthTarget] = useState<Account | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<Account | null>(null);
    const [deleting, setDeleting] = useState(false);
    const [showAdd, setShowAdd] = useState(false);
    // Filters start from the URL (so links restore them) and are written back on change.
    const [accountFilter, setAccountFilter] = useState<AccountFilter>(() => {
        const candidate = new URLSearchParams(queryString).get("section");
        return candidate === "all" || candidate === "authenticated" || candidate === "usable"
            ? candidate
            : "usable";
    });
    const [accountSearch, setAccountSearch] = useState(
        () => new URLSearchParams(queryString).get("search") ?? "",
    );
    const loadRequestId = useRef(0);

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

    // A "tick" used to re-render countdowns every second.
    const [, setTick] = useState(0);
    useEffect(() => {
        const id = setInterval(() => setTick((t) => t + 1), 1000);
        return () => clearInterval(id);
    }, []);

    // Loading and error flags only change after the request settles; the initial
    // state already shows the spinner, and the retry button resets it explicitly.
    const load = useCallback(() => {
        const requestId = ++loadRequestId.current;
        const isCurrent = () => requestId === loadRequestId.current;
        return Promise.all([api.accounts(), api.egressTargets()]).then(
            ([loaded, targets]) => {
                if (!isCurrent()) return;
                setError(null);
                setAccounts(loaded);
                setEgressTargets(targets);
                setSavedPriorities(
                    Object.fromEntries(loaded.map((account) => [account.id, account.priority])),
                );
                setLoading(false);
            },
            (err: unknown) => {
                if (!isCurrent()) return;
                setError(err instanceof Error ? err.message : "Failed to load accounts.");
                setLoading(false);
            },
        );
    }, []);

    const applyAccount = useCallback((updated: Account) => {
        setAccounts((current) => {
            if (!current) return [updated];
            return current.some((account) => account.id === updated.id)
                ? current.map((account) => (account.id === updated.id ? updated : account))
                : [...current, updated];
        });
        setSavedPriorities((current) => ({ ...current, [updated.id]: updated.priority }));
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    useEffect(() => {
        const refresh = () => {
            if (document.visibilityState === "visible") void load();
        };
        const timer = window.setInterval(refresh, 15_000);
        document.addEventListener("visibilitychange", refresh);
        return () => {
            window.clearInterval(timer);
            document.removeEventListener("visibilitychange", refresh);
        };
    }, [load]);

    const priorityDirty =
        accounts !== null &&
        accounts.some((account) => savedPriorities[account.id] !== account.priority);
    const toggleAccountSelection = (id: string) => {
        setSelectedAccountIds((current) =>
            current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
        );
    };
    const applyBulkPriority = async () => {
        const priority = Number(bulkPriority);
        if (
            !accounts ||
            selectedAccountIds.length === 0 ||
            !Number.isInteger(priority) ||
            priority < 1
        )
            return;
        setApplyingBulkPriority(true);
        setError(null);
        try {
            const updated = await api.bulkSetAccountPriority(selectedAccountIds, priority);
            setAccounts(updated);
            setSavedPriorities(
                Object.fromEntries(updated.map((account) => [account.id, account.priority])),
            );
            setSelectedAccountIds([]);
            setSelectionMode(false);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not set account priority.");
        } finally {
            setApplyingBulkPriority(false);
        }
    };
    const exitSelectionMode = () => {
        setSelectedAccountIds([]);
        setSelectionMode(false);
    };
    const toggleVisibleSelection = () => {
        if (selectedAccountIds.length === visibleAccounts.length) {
            setSelectedAccountIds([]);
            return;
        }
        setSelectedAccountIds(visibleAccounts.map((account) => account.id));
    };
    const normalizedSearch = accountSearch.trim().toLocaleLowerCase();
    const visibleAccounts =
        accounts
            ?.filter((account) => {
                if (accountFilter === "authenticated" && !appearsAuthenticated(account))
                    return false;
                if (accountFilter === "usable" && !accountIsUsable(account)) return false;
                if (!normalizedSearch) return true;
                return `${account.label}\n${account.account_email ?? ""}`
                    .toLocaleLowerCase()
                    .includes(normalizedSearch);
            })
            .sort(compareAccountsForDisplay) ?? [];
    const priorityLanes = Array.from(
        new Set(visibleAccounts.map((account) => account.priority)),
    ).sort((a, b) => a - b);

    const dropAccount = (priority: number) => {
        if (!accounts || !draggedId) {
            setDraggedId(null);
            return;
        }
        setAccounts(
            accounts.map((account) =>
                account.id === draggedId ? { ...account, priority } : account,
            ),
        );
        setDraggedId(null);
    };

    const savePriority = async () => {
        if (!accounts || !priorityDirty) return;
        setSavingPriority(true);
        setError(null);
        try {
            const changed = accounts.filter(
                (account) => savedPriorities[account.id] !== account.priority,
            );
            await Promise.all(
                changed.map((account) =>
                    api.updateAccount(account.id, { priority: account.priority }),
                ),
            );
            setSavedPriorities(
                Object.fromEntries(accounts.map((account) => [account.id, account.priority])),
            );
            void load();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not save priority order.");
        } finally {
            setSavingPriority(false);
        }
    };

    // Run a per-account action then refresh the list.
    const runAction = async (id: string, fn: () => Promise<Account>) => {
        setBusyId(id);
        try {
            applyAccount(await fn());
            void load();
        } catch (err) {
            setError(err instanceof ApiError ? err.message : "Action failed. Please retry.");
        } finally {
            setBusyId(null);
        }
    };

    const confirmDelete = async () => {
        if (!deleteTarget) return;
        setDeleting(true);
        try {
            await api.deleteAccount(deleteTarget.id);
            setAccounts(
                (current) =>
                    current?.filter((account) => account.id !== deleteTarget.id) ?? current,
            );
            setDeleteTarget(null);
            void load();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Delete failed.");
        } finally {
            setDeleting(false);
        }
    };

    return (
        <div className="space-y-6">
            <header>
                <div>
                    <h1 className="text-fog-100 text-2xl font-semibold tracking-tight">Accounts</h1>
                    <p className="text-fog-400 mt-0.5 max-w-2xl text-sm">
                        Pooled Claude subscriptions. Drag an account between priority lanes to
                        change its priority; accounts within a lane stay in deterministic rotation
                        order.
                    </p>
                </div>
            </header>

            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div className="w-full md:w-72 md:shrink-0">
                    <TextInput
                        type="search"
                        value={accountSearch}
                        onChange={(event) => {
                            const next = event.target.value;
                            setAccountSearch(next);
                            updateUrl({ search: next });
                        }}
                        placeholder="Search name or email…"
                        aria-label="Search accounts by name or email"
                    />
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2 md:ml-auto">
                    <Segmented
                        options={ACCOUNT_FILTER_OPTIONS}
                        value={accountFilter}
                        onChange={(next) => {
                            setAccountFilter(next);
                            updateUrl({ section: next });
                        }}
                        label="Filter accounts"
                    />
                    <Button variant="ghost" onClick={() => void load()}>
                        Refresh
                    </Button>
                    <Button
                        variant={selectionMode ? "primary" : "ghost"}
                        onClick={() =>
                            selectionMode ? exitSelectionMode() : setSelectionMode(true)
                        }
                    >
                        <ListChecks size={15} aria-hidden="true" />
                        {selectionMode ? "Selecting" : "Bulk priority"}
                    </Button>
                    <Button
                        variant="primary"
                        disabled={!priorityDirty || savingPriority}
                        onClick={() => void savePriority()}
                    >
                        {savingPriority ? <Spinner /> : null} Save priority changes
                    </Button>
                    <Button variant="primary" onClick={() => setShowAdd(true)}>
                        Add account
                    </Button>
                </div>
            </div>

            {selectionMode ? (
                <BulkPriorityBar
                    entityLabel="account"
                    selectedCount={selectedAccountIds.length}
                    visibleCount={visibleAccounts.length}
                    priority={bulkPriority}
                    onPriorityChange={setBulkPriority}
                    onSelectVisible={toggleVisibleSelection}
                    onApply={() => void applyBulkPriority()}
                    onCancel={() => setSelectedAccountIds([])}
                    onClose={exitSelectionMode}
                    applying={applyingBulkPriority}
                />
            ) : null}

            {error ? (
                <div
                    role="alert"
                    className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-sm"
                >
                    {error}
                </div>
            ) : null}

            {loading ? (
                <Card>
                    <LoadingState />
                </Card>
            ) : error && !accounts ? (
                <Card>
                    <ErrorState
                        message={error}
                        onRetry={() => {
                            setLoading(true);
                            setError(null);
                            void load();
                        }}
                    />
                </Card>
            ) : accounts && accounts.length > 0 && visibleAccounts.length === 0 ? (
                <Card>
                    <EmptyState
                        message={
                            normalizedSearch
                                ? "No accounts match this search and filter."
                                : accountFilter === "authenticated"
                                  ? "No authenticated accounts found. Choose All to inspect accounts that need re-authentication."
                                  : "No accounts are currently usable. Choose All to inspect every linked account."
                        }
                    />
                </Card>
            ) : accounts && accounts.length > 0 ? (
                <div className="space-y-8">
                    {priorityLanes.map((priority) => {
                        const laneAccounts = visibleAccounts.filter(
                            (account) => account.priority === priority,
                        );
                        return (
                            <section
                                key={priority}
                                aria-labelledby={`priority-${priority}`}
                                onDragOver={(event) => event.preventDefault()}
                                onDrop={() => dropAccount(priority)}
                                className={`rounded-xl border p-4 transition-colors ${
                                    draggedId
                                        ? "border-brand-500/50 bg-brand-500/5"
                                        : "border-ink-700 bg-ink-950/20"
                                }`}
                            >
                                <div className="mb-4 flex items-center justify-between gap-3">
                                    <div>
                                        <h2
                                            id={`priority-${priority}`}
                                            className="text-fog-100 text-sm font-semibold"
                                        >
                                            Priority {priority}
                                        </h2>
                                        <p className="text-fog-500 mt-1 text-xs">
                                            {laneAccounts.length} account
                                            {laneAccounts.length === 1 ? "" : "s"} · drop here to
                                            assign
                                        </p>
                                    </div>
                                    {draggedId ? (
                                        <span className="text-brand-300 text-xs">
                                            Release to move
                                        </span>
                                    ) : null}
                                </div>
                                {laneAccounts.length === 0 ? (
                                    <div className="border-ink-700 text-fog-500 rounded-lg border border-dashed px-4 py-6 text-center text-xs">
                                        Empty priority lane · drop an account here
                                    </div>
                                ) : (
                                    <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
                                        {laneAccounts.map((acc) => {
                                            const busy = busyId === acc.id;
                                            const disabled = acc.status === "DISABLED";
                                            const reauthRequired =
                                                acc.provider_health === "REAUTH_REQUIRED";
                                            const usable = accountIsUsable(acc);
                                            const probeWarning =
                                                acc.provider_health === "DEGRADED" ||
                                                acc.provider_health === "UNKNOWN" ||
                                                healthIsStale(acc);
                                            const assignedEgress = egressTargetLabel(
                                                acc,
                                                egressTargets,
                                            );
                                            return (
                                                <div
                                                    key={acc.id}
                                                    draggable
                                                    aria-label={`Drag ${acc.label} to another priority lane`}
                                                    aria-grabbed={draggedId === acc.id}
                                                    onDragStart={() => setDraggedId(acc.id)}
                                                    onDragEnd={() => setDraggedId(null)}
                                                    onDragOver={(event) => event.preventDefault()}
                                                    onDrop={(event) => {
                                                        event.stopPropagation();
                                                        dropAccount(priority);
                                                    }}
                                                    className={
                                                        draggedId === acc.id ? "opacity-60" : ""
                                                    }
                                                >
                                                    <Card
                                                        className={`cursor-grab overflow-hidden transition-shadow active:cursor-grabbing ${selectedAccountIds.includes(acc.id) ? "border-brand-400/80 shadow-[0_0_0_2px_color-mix(in_srgb,var(--color-brand-500)_35%,transparent)]" : ""}`}
                                                    >
                                                        {/* Status accent rail + body */}
                                                        <div className="flex">
                                                            <div
                                                                aria-hidden="true"
                                                                className={`w-1 shrink-0 transition-colors ${!usable ? "bg-bad-500" : probeWarning ? "bg-warn-500" : "bg-good-500"}`}
                                                            />
                                                            <div className="flex-1 p-5">
                                                                {/* Header row */}
                                                                <div className="flex items-start justify-between gap-3">
                                                                    <div className="min-w-0">
                                                                        <div className="flex flex-wrap items-center gap-2">
                                                                            {selectionMode ? (
                                                                                <button
                                                                                    type="button"
                                                                                    onClick={(
                                                                                        event,
                                                                                    ) => {
                                                                                        event.stopPropagation();
                                                                                        toggleAccountSelection(
                                                                                            acc.id,
                                                                                        );
                                                                                    }}
                                                                                    aria-pressed={selectedAccountIds.includes(
                                                                                        acc.id,
                                                                                    )}
                                                                                    aria-label={`Select ${acc.label}`}
                                                                                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border transition-colors ${selectedAccountIds.includes(acc.id) ? "border-brand-400 bg-brand-500 text-ink-950" : "border-ink-600 bg-ink-900 hover:border-brand-400 text-transparent"}`}
                                                                                >
                                                                                    <Check
                                                                                        size={13}
                                                                                        strokeWidth={
                                                                                            3
                                                                                        }
                                                                                        aria-hidden="true"
                                                                                    />
                                                                                </button>
                                                                            ) : null}
                                                                            <span className="text-fog-100 truncate text-sm font-semibold">
                                                                                {acc.label}
                                                                            </span>
                                                                            {statusBadge(acc)}
                                                                            {healthBadge(acc)}
                                                                            {acc.authenticated_override ? (
                                                                                <Badge tone="warn">
                                                                                    Always
                                                                                    authenticated
                                                                                </Badge>
                                                                            ) : null}
                                                                            <Badge tone="neutral">
                                                                                Priority{" "}
                                                                                {acc.priority}
                                                                            </Badge>
                                                                            {acc.tier ? (
                                                                                <Badge tone="brand">
                                                                                    <span
                                                                                        className="max-w-48 truncate"
                                                                                        title={tierLabel(
                                                                                            acc.tier,
                                                                                        )}
                                                                                    >
                                                                                        {tierLabel(
                                                                                            acc.tier,
                                                                                        )}
                                                                                    </span>
                                                                                </Badge>
                                                                            ) : null}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 truncate text-xs">
                                                                            {acc.account_email ??
                                                                                "no email on file"}
                                                                        </div>
                                                                        <div className="text-fog-500 mt-1 truncate text-[11px]">
                                                                            Egress ·{" "}
                                                                            {assignedEgress}
                                                                        </div>
                                                                        {false &&
                                                                        acc.provider_health ===
                                                                            "REAUTH_REQUIRED" ? (
                                                                            <div
                                                                                className={`mt-3 rounded-lg border px-3 py-2.5 ${acc.provider_health === "REAUTH_REQUIRED" ? "border-bad-500/30 bg-bad-500/10" : "border-warn-500/30 bg-warn-500/10"}`}
                                                                            >
                                                                                <div className="flex items-start gap-2">
                                                                                    <AlertTriangle
                                                                                        size={15}
                                                                                        className={
                                                                                            acc.provider_health ===
                                                                                            "REAUTH_REQUIRED"
                                                                                                ? "text-bad-500 mt-0.5 shrink-0"
                                                                                                : "text-warn-500 mt-0.5 shrink-0"
                                                                                        }
                                                                                        aria-hidden="true"
                                                                                    />
                                                                                    <div className="min-w-0 flex-1">
                                                                                        <div
                                                                                            className={`text-xs font-semibold ${acc.provider_health === "REAUTH_REQUIRED" ? "text-bad-500" : "text-warn-500"}`}
                                                                                        >
                                                                                            {acc.provider_health ===
                                                                                            "REAUTH_REQUIRED"
                                                                                                ? "Re-authentication required"
                                                                                                : "Provider health probe warning"}
                                                                                        </div>
                                                                                        <p className="text-fog-300 mt-0.5 text-[11px] leading-relaxed">
                                                                                            {acc.provider_health_message ??
                                                                                                "The latest provider health check did not complete successfully."}
                                                                                        </p>
                                                                                    </div>
                                                                                    <button
                                                                                        type="button"
                                                                                        onClick={() =>
                                                                                            setHealthDetailsId(
                                                                                                healthDetailsId ===
                                                                                                    acc.id
                                                                                                    ? null
                                                                                                    : acc.id,
                                                                                            )
                                                                                        }
                                                                                        aria-expanded={
                                                                                            healthDetailsId ===
                                                                                            acc.id
                                                                                        }
                                                                                        className="text-fog-300 hover:text-fog-100 shrink-0 text-[11px] font-medium underline decoration-dotted underline-offset-2"
                                                                                    >
                                                                                        {healthDetailsId ===
                                                                                        acc.id
                                                                                            ? "Hide"
                                                                                            : "Details"}
                                                                                    </button>
                                                                                </div>
                                                                                {healthDetailsId ===
                                                                                acc.id ? (
                                                                                    <dl className="border-ink-700/70 mt-2 grid grid-cols-2 gap-x-3 gap-y-2 border-t pt-2 text-[11px]">
                                                                                        <div>
                                                                                            <dt className="text-fog-500">
                                                                                                Reason
                                                                                            </dt>
                                                                                            <dd className="text-fog-200 mt-0.5 font-mono">
                                                                                                {acc.provider_health_code ??
                                                                                                    "unknown"}
                                                                                            </dd>
                                                                                        </div>
                                                                                        <div>
                                                                                            <dt className="text-fog-500">
                                                                                                Failures
                                                                                            </dt>
                                                                                            <dd className="text-fog-200 mt-0.5 tabular-nums">
                                                                                                {
                                                                                                    acc.provider_health_failure_count
                                                                                                }
                                                                                            </dd>
                                                                                        </div>
                                                                                        <div>
                                                                                            <dt className="text-fog-500">
                                                                                                Last
                                                                                                success
                                                                                            </dt>
                                                                                            <dd className="text-fog-200 mt-0.5">
                                                                                                {formatDateTime(
                                                                                                    acc.provider_health_last_success_at,
                                                                                                )}
                                                                                            </dd>
                                                                                        </div>
                                                                                        <div>
                                                                                            <dt className="text-fog-500">
                                                                                                Checked
                                                                                            </dt>
                                                                                            <dd className="text-fog-200 mt-0.5">
                                                                                                {formatDateTime(
                                                                                                    acc.provider_health_checked_at,
                                                                                                )}
                                                                                            </dd>
                                                                                        </div>
                                                                                    </dl>
                                                                                ) : null}
                                                                            </div>
                                                                        ) : null}
                                                                    </div>
                                                                    <div className="flex shrink-0 items-center gap-2">
                                                                        {busy ? <Spinner /> : null}
                                                                    </div>
                                                                </div>

                                                                {/* Utilization windows (dimmed while parked) */}
                                                                <div
                                                                    className={`mt-5 ${disabled ? "opacity-50" : ""}`}
                                                                >
                                                                    <div
                                                                        className={`border-ink-700 bg-ink-900/45 grid gap-3 rounded-lg border p-3 ${accountQuotaWindows(acc).length > 1 ? "grid-cols-[repeat(auto-fit,minmax(180px,1fr))]" : ""}`}
                                                                    >
                                                                        {accountQuotaWindows(acc)
                                                                            .length === 0 ? (
                                                                            <div className="text-fog-400 text-xs">
                                                                                No quota data
                                                                                returned by
                                                                                Anthropic.
                                                                            </div>
                                                                        ) : (
                                                                            accountQuotaWindows(
                                                                                acc,
                                                                            ).map((window) => (
                                                                                <div
                                                                                    key={window.key}
                                                                                    className="bg-ink-950/35 rounded-md p-2.5"
                                                                                >
                                                                                    <UsageBar
                                                                                        label={
                                                                                            window.label
                                                                                        }
                                                                                        fraction={
                                                                                            window.fraction
                                                                                        }
                                                                                    />
                                                                                    <div className="text-fog-400 mt-1.5 text-[11px]">
                                                                                        {formatCountdown(
                                                                                            window.resetAt,
                                                                                        )}
                                                                                    </div>
                                                                                </div>
                                                                            ))
                                                                        )}
                                                                    </div>
                                                                </div>

                                                                <div className="border-ink-700 bg-ink-900/45 mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-lg border">
                                                                    <div className="bg-ink-950/40 px-3 py-2.5">
                                                                        <div className="text-fog-100 text-sm font-semibold tabular-nums">
                                                                            {formatUsd(
                                                                                acc.total_spend_usd,
                                                                            )}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-[10px] tracking-wider uppercase">
                                                                            All-time spend
                                                                        </div>
                                                                    </div>
                                                                    <div className="bg-ink-950/40 px-3 py-2.5">
                                                                        <div className="text-fog-100 text-sm font-semibold tabular-nums">
                                                                            {formatUsd(
                                                                                acc.monthly_spend_usd,
                                                                            )}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-[10px] tracking-wider uppercase">
                                                                            This month
                                                                        </div>
                                                                    </div>
                                                                </div>

                                                                {/* State notices */}
                                                                {disabled ? (
                                                                    <div className="border-ink-600 bg-ink-900 text-fog-300 mt-3 rounded-md border px-2.5 py-1.5 text-[11px]">
                                                                        Parked — excluded from
                                                                        rotation. Enable to return
                                                                        it to the pool.
                                                                    </div>
                                                                ) : null}
                                                                {exhaustedQuotaWindow(acc) ? (
                                                                    <div className="border-bad-500/30 bg-bad-500/10 text-bad-500 mt-3 rounded-md border px-2.5 py-1.5 text-[11px]">
                                                                        {
                                                                            exhaustedQuotaWindow(
                                                                                acc,
                                                                            )!.label
                                                                        }{" "}
                                                                        exhausted ·{" "}
                                                                        {formatCountdown(
                                                                            exhaustedQuotaWindow(
                                                                                acc,
                                                                            )!.resetAt,
                                                                        )}
                                                                    </div>
                                                                ) : cooldownIsActive(acc) ? (
                                                                    <div className="border-warn-500/30 bg-warn-500/10 text-warn-500 mt-3 rounded-md border px-2.5 py-1.5 text-[11px]">
                                                                        In cooldown —{" "}
                                                                        {formatCountdown(
                                                                            acc.cooldown_until,
                                                                        )}
                                                                    </div>
                                                                ) : null}
                                                                {/* Meta */}
                                                                <div className="text-fog-400 mt-4 grid grid-cols-2 gap-2 text-[11px]">
                                                                    <div>
                                                                        Last used
                                                                        <div className="text-fog-300">
                                                                            {formatDateTime(
                                                                                acc.last_used_at,
                                                                            )}
                                                                        </div>
                                                                    </div>
                                                                    <div>
                                                                        Added
                                                                        <div className="text-fog-300">
                                                                            {formatDateTime(
                                                                                acc.created_at,
                                                                            )}
                                                                        </div>
                                                                    </div>
                                                                    <div>
                                                                        Provider checked
                                                                        <div className="text-fog-300">
                                                                            {formatDateTime(
                                                                                acc.provider_health_checked_at,
                                                                            )}
                                                                        </div>
                                                                    </div>
                                                                </div>

                                                                {/* Actions */}
                                                                <div className="border-ink-700 mt-4 flex flex-wrap gap-2 border-t pt-4">
                                                                    <Button
                                                                        variant="ghost"
                                                                        disabled={busy}
                                                                        onClick={() =>
                                                                            setEditTarget(acc)
                                                                        }
                                                                    >
                                                                        Edit
                                                                    </Button>
                                                                    <Button
                                                                        variant="ghost"
                                                                        disabled={
                                                                            busy || reauthRequired
                                                                        }
                                                                        onClick={() =>
                                                                            void runAction(
                                                                                acc.id,
                                                                                () =>
                                                                                    api.refreshQuota(
                                                                                        acc.id,
                                                                                    ),
                                                                            )
                                                                        }
                                                                    >
                                                                        Refresh quota
                                                                    </Button>
                                                                    <Button
                                                                        variant="ghost"
                                                                        disabled={
                                                                            busy ||
                                                                            disabled ||
                                                                            acc.provider_health !==
                                                                                "HEALTHY" ||
                                                                            acc.session_used_pct ===
                                                                                null ||
                                                                            (acc.session_reset_at !==
                                                                                null &&
                                                                                new Date(
                                                                                    acc.session_reset_at,
                                                                                ).getTime() >
                                                                                    Date.now())
                                                                        }
                                                                        onClick={() =>
                                                                            void runAction(
                                                                                acc.id,
                                                                                () =>
                                                                                    api.warmupAccount(
                                                                                        acc.id,
                                                                                    ),
                                                                            )
                                                                        }
                                                                    >
                                                                        Warm Up
                                                                    </Button>
                                                                    <Button
                                                                        variant={
                                                                            reauthRequired
                                                                                ? "primary"
                                                                                : "ghost"
                                                                        }
                                                                        disabled={busy}
                                                                        onClick={() =>
                                                                            setReauthTarget(acc)
                                                                        }
                                                                    >
                                                                        Re-authenticate
                                                                    </Button>

                                                                    {disabled ? (
                                                                        <Button
                                                                            variant="primary"
                                                                            disabled={busy}
                                                                            onClick={() =>
                                                                                void runAction(
                                                                                    acc.id,
                                                                                    () =>
                                                                                        api.enableAccount(
                                                                                            acc.id,
                                                                                        ),
                                                                                )
                                                                            }
                                                                        >
                                                                            Enable
                                                                        </Button>
                                                                    ) : (
                                                                        <Button
                                                                            variant="subtle"
                                                                            disabled={busy}
                                                                            onClick={() =>
                                                                                void runAction(
                                                                                    acc.id,
                                                                                    () =>
                                                                                        api.disableAccount(
                                                                                            acc.id,
                                                                                        ),
                                                                                )
                                                                            }
                                                                        >
                                                                            Disable
                                                                        </Button>
                                                                    )}

                                                                    <Button
                                                                        variant="danger"
                                                                        disabled={busy}
                                                                        onClick={() =>
                                                                            setDeleteTarget(acc)
                                                                        }
                                                                    >
                                                                        Delete
                                                                    </Button>
                                                                </div>
                                                            </div>
                                                        </div>
                                                    </Card>
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </section>
                        );
                    })}
                </div>
            ) : (
                <Card>
                    <EmptyState message="No accounts yet. Add one to get started." />
                </Card>
            )}

            {/* Edit */}
            {editTarget ? (
                <EditAccountModal
                    account={editTarget}
                    egressTargets={egressTargets}
                    onClose={() => setEditTarget(null)}
                    onSaved={(updated) => {
                        setEditTarget(null);
                        applyAccount(updated);
                        void load();
                    }}
                />
            ) : null}

            {/* Re-authenticate */}
            {reauthTarget ? (
                <ReauthAccountModal
                    account={reauthTarget}
                    onClose={() => setReauthTarget(null)}
                    onDone={(updated) => {
                        setReauthTarget(null);
                        applyAccount(updated);
                        void load();
                    }}
                />
            ) : null}

            {/* Delete confirm */}
            {deleteTarget ? (
                <ConfirmDialog
                    title="Delete account"
                    message={`Delete "${deleteTarget.label}"? It will be removed from the pool and detached from usage history. This cannot be undone.`}
                    confirmLabel="Delete"
                    busy={deleting}
                    onConfirm={() => void confirmDelete()}
                    onCancel={() => setDeleteTarget(null)}
                />
            ) : null}

            {/* Add account flow */}
            {showAdd ? (
                <AddAccountModal
                    onClose={() => setShowAdd(false)}
                    onDone={(updated) => {
                        setShowAdd(false);
                        applyAccount(updated);
                        void load();
                    }}
                />
            ) : null}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Edit-account modal — rename the label and update rotation policy.
// ---------------------------------------------------------------------------

function EditAccountModal({
    account,
    egressTargets,
    onClose,
    onSaved,
}: {
    account: Account;
    egressTargets: EgressTarget[];
    onClose: () => void;
    onSaved: (account: Account) => void;
}) {
    const [label, setLabel] = useState(account.label);
    const [authenticatedOverride, setAuthenticatedOverride] = useState(
        account.authenticated_override,
    );
    const [fiveHourRotation, setFiveHourRotation] = useState(
        String(account.five_hour_rotation_threshold),
    );
    const [weeklyRotation, setWeeklyRotation] = useState(String(account.weekly_rotation_threshold));
    const [cooldown, setCooldown] = useState(String(account.cooldown_seconds));
    const [priority, setPriority] = useState(String(account.priority));
    const defaultEgressTargetId = egressTargets.find((target) => target.enabled)?.id ?? "";
    const [egressTargetId, setEgressTargetId] = useState(
        account.egress_target_id ?? defaultEgressTargetId,
    );
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setSubmitting(true);
        try {
            const updated = await api.updateAccount(account.id, {
                label: label.trim(),
                authenticated_override: authenticatedOverride,
                five_hour_rotation_threshold: parseOptionalFloat(fiveHourRotation),
                weekly_rotation_threshold: parseOptionalFloat(weeklyRotation),
                cooldown_seconds: parseOptionalInt(cooldown),
                priority: parseOptionalInt(priority),
                egress_target_id: egressTargetId || defaultEgressTargetId || null,
            });
            onSaved(updated);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Save failed.");
            setSubmitting(false);
        }
    };

    return (
        <Modal title={`Edit ${account.label}`} onClose={onClose}>
            <form onSubmit={submit} className="space-y-4">
                <Field label="Label" hint="A name to identify this account in the pool.">
                    <TextInput
                        value={label}
                        onChange={(e) => setLabel(e.target.value)}
                        autoFocus
                        required
                    />
                </Field>
                <Field label="Email" hint="Managed by OAuth and updated on authentication.">
                    <div className="text-fog-200 text-sm">
                        {account.account_email ?? "Email unavailable"}
                    </div>
                </Field>
                <Field
                    label="Egress network path"
                    hint="Accounts use the first configured regional path by default. Select another path to pin this account to it."
                >
                    <SelectMenu
                        value={egressTargetId}
                        onChange={setEgressTargetId}
                        ariaLabel="Select egress network path"
                        options={[
                            ...egressTargets
                                .filter((target) => target.enabled)
                                .map((target) => ({
                                    value: target.id,
                                    label:
                                        target.interface_name && target.private_ip
                                            ? `${target.interface_name} · ${target.private_ip}${target.public_ip ? ` → ${target.public_ip}` : ""}`
                                            : target.label,
                                })),
                        ]}
                    />
                </Field>
                <Field
                    label="Show in Authenticated accounts"
                    hint="Keeps this account in the Authenticated accounts view even when re-authentication is required. This does not authenticate the account or make it usable."
                >
                    <label className="border-fog-700 bg-ink-900 flex cursor-pointer items-center justify-between rounded-md border px-3 py-2">
                        <span className="text-fog-200 text-sm">
                            Include this account in the authenticated view
                        </span>
                        <input
                            type="checkbox"
                            checked={authenticatedOverride}
                            onChange={(event) => setAuthenticatedOverride(event.target.checked)}
                            className="accent-brand-500 h-4 w-4"
                        />
                    </label>
                </Field>

                <div className="border-ink-700 space-y-4 border-t pt-4">
                    <p className="text-fog-400 text-xs">Rotation policy for this account.</p>
                    <Field
                        label="5-hour rotation threshold"
                        hint="5-hour utilization (0–1) at which this account rotates out."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            max={1}
                            step={0.05}
                            value={fiveHourRotation}
                            onChange={(e) => setFiveHourRotation(e.target.value)}
                            required
                        />
                    </Field>
                    <Field
                        label="Weekly rotation threshold"
                        hint="Weekly utilization (0–1) at which this account rotates out."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            max={1}
                            step={0.05}
                            value={weeklyRotation}
                            onChange={(e) => setWeeklyRotation(e.target.value)}
                            required
                        />
                    </Field>
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                        <Field
                            label="Cooldown seconds"
                            hint="Rest period after a 429 with no retry-after."
                        >
                            <TextInput
                                type="number"
                                min={0}
                                value={cooldown}
                                onChange={(e) => setCooldown(e.target.value)}
                                required
                            />
                        </Field>
                        <Field
                            label="Priority"
                            hint="1 is tried first; moving it shifts the other accounts."
                        >
                            <TextInput
                                type="number"
                                min={1}
                                value={priority}
                                onChange={(e) => setPriority(e.target.value)}
                                required
                            />
                        </Field>
                    </div>
                </div>

                {error ? (
                    <div
                        role="alert"
                        className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-sm"
                    >
                        {error}
                    </div>
                ) : null}

                <div className="flex justify-end gap-2 pt-1">
                    <Button type="button" variant="ghost" onClick={onClose} disabled={submitting}>
                        Cancel
                    </Button>
                    <Button type="submit" variant="primary" disabled={submitting || !label.trim()}>
                        {submitting ? <Spinner /> : null}
                        Save
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Add-account OAuth flow.
//   1. POST oauth/start -> { authorize_url, verifier }
//   2. User opens the link, authorizes, pastes the returned code + a label.
//   3. POST oauth/complete { label, code, verifier }
// ---------------------------------------------------------------------------

function AddAccountModal({
    onClose,
    onDone,
}: {
    onClose: () => void;
    onDone: (account: Account) => void;
}) {
    const [starting, setStarting] = useState(true);
    const [startError, setStartError] = useState<string | null>(null);

    const [authorizeUrl, setAuthorizeUrl] = useState("");
    const [verifier, setVerifier] = useState("");

    const [label, setLabel] = useState("");
    const [code, setCode] = useState("");

    const [completing, setCompleting] = useState(false);
    const [completeError, setCompleteError] = useState<string | null>(null);

    const start = useCallback(
        () =>
            api.oauthStart().then(
                (res) => {
                    setAuthorizeUrl(res.authorize_url);
                    setVerifier(res.verifier);
                    setStarting(false);
                },
                (err: unknown) => {
                    setStartError(err instanceof Error ? err.message : "Failed to start OAuth.");
                    setStarting(false);
                },
            ),
        [],
    );

    useEffect(() => {
        void start();
    }, [start]);

    const complete = async (e: React.FormEvent) => {
        e.preventDefault();
        setCompleteError(null);
        setCompleting(true);
        try {
            const account = await api.oauthComplete(label.trim(), code.trim(), verifier);
            onDone(account);
        } catch (err) {
            setCompleteError(err instanceof Error ? err.message : "Failed to complete OAuth.");
            setCompleting(false);
        }
    };

    return (
        <Modal title="Add account" onClose={onClose} widthClass="max-w-xl">
            {starting ? (
                <LoadingState label="Starting authorization…" />
            ) : startError ? (
                <ErrorState
                    message={startError}
                    onRetry={() => {
                        setStarting(true);
                        setStartError(null);
                        void start();
                    }}
                />
            ) : (
                <form onSubmit={complete} className="space-y-5">
                    {/* Step 1 */}
                    <div>
                        <div className="text-fog-400 mb-2 text-xs font-semibold tracking-wider uppercase">
                            Step 1 — Authorize
                        </div>
                        <p className="text-fog-300 text-sm">
                            Open the authorization link below, sign in to the Claude account,
                            approve access, then copy the code Claude gives you.
                        </p>
                        <a
                            href={authorizeUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="border-brand-400/40 bg-brand-500/10 text-brand-300 hover:bg-brand-500/20 mt-3 inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm font-medium break-all"
                        >
                            Open authorization page ↗
                        </a>
                    </div>

                    {/* Step 2 */}
                    <div className="border-ink-700 space-y-3 border-t pt-5">
                        <div className="text-fog-400 text-xs font-semibold tracking-wider uppercase">
                            Step 2 — Complete
                        </div>

                        <Field label="Label" hint="A name to identify this account.">
                            <TextInput
                                value={label}
                                onChange={(e) => setLabel(e.target.value)}
                                placeholder="e.g. team-pool-1"
                                required
                            />
                        </Field>

                        <Field label="Authorization code">
                            <TextInput
                                value={code}
                                onChange={(e) => setCode(e.target.value)}
                                placeholder="Paste the code from Claude"
                                required
                            />
                        </Field>

                        {completeError ? (
                            <div
                                role="alert"
                                className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-sm"
                            >
                                {completeError}
                            </div>
                        ) : null}

                        <div className="flex justify-end gap-2 pt-1">
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={onClose}
                                disabled={completing}
                            >
                                Cancel
                            </Button>
                            <Button
                                type="submit"
                                variant="primary"
                                disabled={completing || !label.trim() || !code.trim()}
                            >
                                {completing ? <Spinner /> : null}
                                Add account
                            </Button>
                        </div>
                    </div>
                </form>
            )}
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Re-authenticate an existing account in place. Mirrors the add-account flow:
//   1. POST oauth/start -> { authorize_url, verifier }
//   2. Admin opens the link, authorizes, pastes the returned code.
//   3. POST accounts/{id}/oauth/complete { code, verifier }
// ---------------------------------------------------------------------------

function ReauthAccountModal({
    account,
    onClose,
    onDone,
}: {
    account: Account;
    onClose: () => void;
    onDone: (account: Account) => void;
}) {
    const [starting, setStarting] = useState(true);
    const [startError, setStartError] = useState<string | null>(null);

    const [authorizeUrl, setAuthorizeUrl] = useState("");
    const [verifier, setVerifier] = useState("");

    const [code, setCode] = useState("");

    const [completing, setCompleting] = useState(false);
    const [completeError, setCompleteError] = useState<string | null>(null);

    const start = useCallback(
        () =>
            api.oauthStart().then(
                (res) => {
                    setAuthorizeUrl(res.authorize_url);
                    setVerifier(res.verifier);
                    setStarting(false);
                },
                (err: unknown) => {
                    setStartError(err instanceof Error ? err.message : "Failed to start OAuth.");
                    setStarting(false);
                },
            ),
        [],
    );

    useEffect(() => {
        void start();
    }, [start]);

    const complete = async (e: React.FormEvent) => {
        e.preventDefault();
        setCompleteError(null);
        setCompleting(true);
        try {
            const updated = await api.reauthAccount(account.id, {
                code: code.trim(),
                verifier,
            });
            onDone(updated);
        } catch (err) {
            setCompleteError(err instanceof Error ? err.message : "Failed to complete OAuth.");
            setCompleting(false);
        }
    };

    return (
        <Modal title={`Re-authenticate ${account.label}`} onClose={onClose} widthClass="max-w-xl">
            {starting ? (
                <LoadingState label="Starting authorization…" />
            ) : startError ? (
                <ErrorState
                    message={startError}
                    onRetry={() => {
                        setStarting(true);
                        setStartError(null);
                        void start();
                    }}
                />
            ) : (
                <form onSubmit={complete} className="space-y-5">
                    {/* Step 1 */}
                    <div>
                        <div className="text-fog-400 mb-2 text-xs font-semibold tracking-wider uppercase">
                            Step 1 — Authorize
                        </div>
                        <p className="text-fog-300 text-sm">
                            Open the authorization link below, sign in to{" "}
                            <span className="text-fog-100 font-medium">{account.label}</span>,
                            approve access, then copy the code Claude gives you.
                        </p>
                        <a
                            href={authorizeUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="border-brand-400/40 bg-brand-500/10 text-brand-300 hover:bg-brand-500/20 mt-3 inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm font-medium break-all"
                        >
                            Open authorization page ↗
                        </a>
                    </div>

                    {/* Step 2 */}
                    <div className="border-ink-700 space-y-3 border-t pt-5">
                        <div className="text-fog-400 text-xs font-semibold tracking-wider uppercase">
                            Step 2 — Complete
                        </div>

                        <Field label="Authorization code">
                            <TextInput
                                value={code}
                                onChange={(e) => setCode(e.target.value)}
                                placeholder="Paste the code from Claude"
                                required
                            />
                        </Field>

                        {completeError ? (
                            <div
                                role="alert"
                                className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-sm"
                            >
                                {completeError}
                            </div>
                        ) : null}

                        <div className="flex justify-end gap-2 pt-1">
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={onClose}
                                disabled={completing}
                            >
                                Cancel
                            </Button>
                            <Button
                                type="submit"
                                variant="primary"
                                disabled={completing || !code.trim()}
                            >
                                {completing ? <Spinner /> : null}
                                Re-authenticate
                            </Button>
                        </div>
                    </div>
                </form>
            )}
        </Modal>
    );
}
