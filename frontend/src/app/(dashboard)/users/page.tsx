"use client";

// ---------------------------------------------------------------------------
// Users page: each user owns one or more API keys. Create/edit/delete users,
// set per-user rate limits and token budgets, manage their keys (one-time
// secret reveal with shell + settings.json setup), and see month-to-date usage.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useState } from "react";
import { Check, ChevronRight, KeyRound, ListChecks, Pencil, Search, Trash2 } from "lucide-react";
import { api, API_BASE_URL, ApiError } from "@/lib/api";
import { ApiKey, THINKING_LEVELS, ThinkingLevel, ThinkingMode, User, Preset } from "@/lib/types";
import {
    ModelAccessEditor,
    ModelRewritesEditor,
    ModelRulesEditor,
    PresetsPanel,
    ThinkingModesEditor,
} from "@/components/presets-panel";
import { formatDateTime, formatNumber, formatTokens, formatUsd } from "@/lib/format";
import {
    Badge,
    Button,
    BulkPriorityBar,
    Card,
    ConfirmDialog,
    CopyButton,
    EmptyState,
    ErrorState,
    Field,
    LoadingState,
    Modal,
    SelectMenu,
    Spinner,
    StatusToggle,
    TextInput,
} from "@/components/ui";

// Parse an optional non-negative integer limit field; blank -> null (no limit).
function parseLimitInput(value: string): number | null {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    const n = Number(trimmed);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null;
}

function parseMoneyInput(value: string): number | null {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    const n = Number(trimmed);
    return Number.isFinite(n) && n >= 0 ? n : null;
}

function UserRateLimitFields({
    minute,
    hour,
    day,
    onMinute,
    onHour,
    onDay,
}: {
    minute: string;
    hour: string;
    day: string;
    onMinute: (value: string) => void;
    onHour: (value: string) => void;
    onDay: (value: string) => void;
}) {
    return (
        <div>
            <div className="text-fog-300 mb-2 text-xs font-medium">Request rate limits</div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field label="Per minute">
                    <TextInput
                        type="number"
                        min={0}
                        value={minute}
                        onChange={(event) => onMinute(event.target.value)}
                        placeholder="Unlimited"
                    />
                </Field>
                <Field label="Per hour">
                    <TextInput
                        type="number"
                        min={0}
                        value={hour}
                        onChange={(event) => onHour(event.target.value)}
                        placeholder="Unlimited"
                    />
                </Field>
                <Field label="Per day">
                    <TextInput
                        type="number"
                        min={0}
                        value={day}
                        onChange={(event) => onDay(event.target.value)}
                        placeholder="Unlimited"
                    />
                </Field>
            </div>
            <p className="text-fog-400 mt-1.5 text-xs">
                Across all of this user&apos;s keys. Rolling windows; 0 or blank means unlimited.
            </p>
        </div>
    );
}

// Render the small "rate / budget" limit chips shared by users and keys.
function LimitBadges({ rate, budget }: { rate: number | null; budget: number | null }) {
    if (!rate && !budget) return null;
    return (
        <span className="inline-flex flex-wrap gap-1.5">
            {rate ? <Badge tone="neutral">{formatNumber(rate)}/min</Badge> : null}
            {budget ? <Badge tone="neutral">{formatTokens(budget)} tok/mo</Badge> : null}
        </span>
    );
}

function MonthlyUsage({ user }: { user: User }) {
    const hasBudget = (user.monthly_token_budget ?? 0) > 0;
    const used = Math.max(0, user.monthly_tokens_used);
    const budget = user.monthly_token_budget ?? 0;
    const percentage = hasBudget ? Math.min(used / budget, 1) : 0;
    const hasSpendBudget = (user.monthly_spend_budget_usd ?? 0) > 0;
    const spendPercentage = hasSpendBudget
        ? Math.min(user.monthly_spend_usd / (user.monthly_spend_budget_usd ?? 1), 1)
        : 0;
    return (
        <div className="border-ink-700 bg-ink-900/45 mt-4 rounded-lg border p-4">
            <div className="grid gap-4 sm:grid-cols-2">
                <div>
                    <div className="flex items-baseline justify-between gap-2">
                        <div className="text-fog-200 text-sm font-medium">Tokens this month</div>
                        <div className="text-fog-100 font-mono text-sm font-semibold tabular-nums">
                            {formatTokens(used)}
                            {hasBudget ? ` / ${formatTokens(budget)}` : ""}
                        </div>
                    </div>
                    <div className="text-fog-400 mt-0.5 text-xs">
                        {hasBudget
                            ? `${(percentage * 100).toFixed(1)}% of monthly limit used`
                            : "No monthly limit set"}
                    </div>
                    {hasBudget ? (
                        <div className="bg-ink-700 mt-3 h-2 overflow-hidden rounded-full">
                            <div
                                className={`h-full rounded-full transition-all ${percentage >= 0.9 ? "bg-bad-500" : percentage >= 0.7 ? "bg-warn-500" : "bg-brand-500"}`}
                                style={{ width: `${percentage * 100}%` }}
                            />
                        </div>
                    ) : null}
                </div>
                <div className="border-ink-700 sm:border-l sm:pl-4">
                    <div className="flex items-baseline justify-between gap-2">
                        <div className="text-fog-200 text-sm font-medium">Spend this month</div>
                        <div className="text-fog-100 font-mono text-sm font-semibold tabular-nums">
                            {formatUsd(user.monthly_spend_usd)}
                            {hasSpendBudget
                                ? ` / ${formatUsd(user.monthly_spend_budget_usd ?? 0)}`
                                : ""}
                        </div>
                    </div>
                    <div className="text-fog-400 mt-0.5 text-xs">
                        {hasSpendBudget
                            ? `${(spendPercentage * 100).toFixed(1)}% of monthly limit used`
                            : "No monthly spend limit set"}
                    </div>
                    {hasSpendBudget ? (
                        <div className="bg-ink-700 mt-3 h-2 overflow-hidden rounded-full">
                            <div
                                className={`h-full rounded-full transition-all ${spendPercentage >= 0.9 ? "bg-bad-500" : spendPercentage >= 0.7 ? "bg-warn-500" : "bg-brand-500"}`}
                                style={{ width: `${spendPercentage * 100}%` }}
                            />
                        </div>
                    ) : null}
                </div>
            </div>
            <div className="text-fog-400 mt-2 text-xs">
                Resets {formatDateTime(user.monthly_reset_at)}
            </div>
        </div>
    );
}

function ThinkingLevelSelector({
    value,
    onChange,
}: {
    value: ThinkingLevel[];
    onChange: (levels: ThinkingLevel[]) => void;
}) {
    const toggle = (level: ThinkingLevel) => {
        onChange(
            value.includes(level)
                ? value.filter((candidate) => candidate !== level)
                : THINKING_LEVELS.filter(
                      (candidate) => candidate === level || value.includes(candidate),
                  ),
        );
    };

    return (
        <Field
            label="Allowed thinking levels"
            hint="Requests with an explicit effort outside this set are blocked. Requests without one remain allowed."
        >
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {THINKING_LEVELS.map((level) => {
                    const checked = value.includes(level);
                    return (
                        <label
                            key={level}
                            className={`flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm capitalize transition-colors ${
                                checked
                                    ? "border-ink-700 bg-ink-900/60 text-fog-200"
                                    : "border-ink-700 bg-ink-900/50 text-fog-400"
                            }`}
                        >
                            <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggle(level)}
                                className="border-ink-600 bg-ink-900 accent-brand-500 h-4 w-4 rounded"
                            />
                            {level}
                        </label>
                    );
                })}
            </div>
        </Field>
    );
}

const PRESET_OVERRIDE_LABELS: Record<string, string> = {
    fallback_enabled: "API fallback overridden",
    allowed_models: "Models overridden",
    allowed_thinking_levels: "Thinking levels overridden",
    allowed_thinking_modes: "Thinking modes overridden",
    model_overrides: "Model rewrites overridden",
    model_thinking_levels: "Per-model thinking overridden",
    model_thinking_modes: "Per-model modes overridden",
    allow_extended_context: "Extended context overridden",
};

function PolicyBadges({ user, presetName }: { user: User; presetName?: string }) {
    const overrideLabels = user.preset_id
        ? user.preset_overrides.map(
              (field) => PRESET_OVERRIDE_LABELS[field] ?? `${field} overridden`,
          )
        : [];
    return (
        <span className="inline-flex flex-wrap gap-1.5">
            <Badge tone={user.preset_id ? "brand" : "neutral"}>
                {user.preset_id ? `Preset: ${presetName ?? "Applied"}` : "No preset"}
            </Badge>
            {overrideLabels.map((label) => (
                <Badge key={label} tone="neutral">
                    {label}
                </Badge>
            ))}
        </span>
    );
}

function UserPolicyFields({
    modelOptions,
    fallbackEnabled,
    onFallbackEnabled,
    visible,
    allowedModels,
    onAllowedModels,
    thinkingLevels,
    onThinkingLevels,
    thinkingModes,
    onThinkingModes,
    modelOverrides,
    onModelOverrides,
    modelLevels,
    onModelLevels,
    modelModes,
    onModelModes,
    allowExtendedContext,
    onAllowExtendedContext,
}: {
    modelOptions: string[];
    fallbackEnabled: boolean;
    onFallbackEnabled: (value: boolean) => void;
    visible?: string[];
    allowedModels: string[] | null;
    onAllowedModels: (value: string[] | null) => void;
    thinkingLevels: ThinkingLevel[];
    onThinkingLevels: (value: ThinkingLevel[]) => void;
    thinkingModes: ThinkingMode[];
    onThinkingModes: (value: ThinkingMode[]) => void;
    modelOverrides: Record<string, string>;
    onModelOverrides: (value: Record<string, string>) => void;
    modelLevels: Record<string, ThinkingLevel[]>;
    onModelLevels: (value: Record<string, ThinkingLevel[]>) => void;
    modelModes: Record<string, ThinkingMode[]>;
    onModelModes: (value: Record<string, ThinkingMode[]>) => void;
    allowExtendedContext: boolean;
    onAllowExtendedContext: (value: boolean) => void;
}) {
    const show = (field: string) => !visible || visible.includes(field);
    return (
        <div className="space-y-5">
            {show("fallback_enabled") && (
                <label className="border-ink-700 bg-ink-900/50 text-fog-200 flex items-center gap-2.5 rounded-md border px-3 py-2.5 text-sm">
                    <input
                        type="checkbox"
                        checked={fallbackEnabled}
                        onChange={(event) => onFallbackEnabled(event.target.checked)}
                        className="accent-brand-500 h-4 w-4 shrink-0"
                    />
                    Allow API fallback providers
                </label>
            )}
            {show("allow_extended_context") && (
                <label className="border-ink-700 bg-ink-900/50 text-fog-200 flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm">
                    <input
                        type="checkbox"
                        checked={allowExtendedContext}
                        onChange={(e) => onAllowExtendedContext(e.target.checked)}
                        className="accent-brand-500 mt-0.5 h-4 w-4 shrink-0"
                    />
                    <span>
                        <span className="block">Allow extended context window</span>
                        <span className="text-fog-400 mt-0.5 block text-xs">
                            Enable the provider&apos;s extended context mode for this policy.
                        </span>
                    </span>
                </label>
            )}
            {show("allowed_models") && (
                <div className="space-y-2">
                    <div className="text-fog-300 text-xs font-medium">Allowed models</div>
                    <ModelAccessEditor
                        models={modelOptions}
                        value={allowedModels}
                        onChange={onAllowedModels}
                    />
                </div>
            )}
            {show("allowed_thinking_levels") && (
                <ThinkingLevelSelector value={thinkingLevels} onChange={onThinkingLevels} />
            )}
            {show("allowed_thinking_modes") && (
                <div className="space-y-2">
                    <div className="text-fog-300 text-xs font-medium">Allowed thinking modes</div>
                    <ThinkingModesEditor value={thinkingModes} onChange={onThinkingModes} />
                </div>
            )}
            {show("model_overrides") && (
                <div className="space-y-2">
                    <div className="text-fog-300 text-xs font-medium">Model rewrites</div>
                    <ModelRewritesEditor
                        models={modelOptions}
                        value={modelOverrides}
                        onChange={onModelOverrides}
                    />
                </div>
            )}
            {(show("model_thinking_levels") || show("model_thinking_modes")) && (
                <div className="space-y-2">
                    <div className="text-fog-300 text-xs font-medium">Per-model thinking rules</div>
                    <ModelRulesEditor
                        models={modelOptions}
                        levels={modelLevels}
                        modes={modelModes}
                        onLevels={onModelLevels}
                        onModes={onModelModes}
                        showLevels={show("model_thinking_levels")}
                        showModes={show("model_thinking_modes")}
                    />
                </div>
            )}
        </div>
    );
}

export default function UsersPage() {
    const [users, setUsers] = useState<User[] | null>(null);
    const [presets, setPresets] = useState<Preset[]>([]);
    const [modelOptions, setModelOptions] = useState<string[]>([]);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [expandedId, setExpandedId] = useState<string | null>(null);
    const [userSearch, setUserSearch] = useState("");
    const [savedPriorities, setSavedPriorities] = useState<Record<string, number>>({});
    const [draggedId, setDraggedId] = useState<string | null>(null);
    const [savingPriority, setSavingPriority] = useState(false);
    const [selectedUserIds, setSelectedUserIds] = useState<string[]>([]);
    const [selectionMode, setSelectionMode] = useState(false);
    const [bulkPriority, setBulkPriority] = useState("1");
    const [applyingBulkPriority, setApplyingBulkPriority] = useState(false);

    const [showCreate, setShowCreate] = useState(false);
    const [editTarget, setEditTarget] = useState<User | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<User | null>(null);
    const [deleting, setDeleting] = useState(false);

    // One-time key reveal (after creating a key).
    const [revealed, setRevealed] = useState<{
        name: string;
        label: string;
        secret: string;
    } | null>(null);

    const load = useCallback(async (background = false) => {
        if (!background) setLoading(true);
        if (!background) setError(null);
        try {
            const [loaded, loadedPresets, loadedModels] = await Promise.all([
                api.users(),
                api.presets(),
                api.userModelOptions(),
            ]);
            setUsers(loaded);
            setPresets(loadedPresets);
            setModelOptions(loadedModels);
            setSavedPriorities(
                Object.fromEntries(loaded.map((user) => [user.id, user.priority ?? 4])),
            );
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to load users.");
        } finally {
            if (!background) setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const normalizedUserSearch = userSearch.trim().toLocaleLowerCase();
    const visibleUsers =
        users?.filter((user) => user.name.toLocaleLowerCase().includes(normalizedUserSearch)) ?? [];
    const priorityLanes = Array.from(new Set(visibleUsers.map((user) => user.priority ?? 4))).sort(
        (a, b) => a - b,
    );
    const priorityDirty =
        users?.some((user) => savedPriorities[user.id] !== (user.priority ?? 4)) ?? false;
    const toggleUserSelection = (id: string) => {
        setSelectedUserIds((current) =>
            current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
        );
    };
    const applyBulkPriority = async () => {
        const priority = Number(bulkPriority);
        if (!users || selectedUserIds.length === 0 || !Number.isInteger(priority) || priority < 1)
            return;
        setApplyingBulkPriority(true);
        setError(null);
        try {
            const updated = await api.bulkSetUserPriority(selectedUserIds, priority);
            setUsers(updated);
            setSavedPriorities(
                Object.fromEntries(updated.map((user) => [user.id, user.priority ?? 4])),
            );
            setSelectedUserIds([]);
            setSelectionMode(false);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not set user priority.");
        } finally {
            setApplyingBulkPriority(false);
        }
    };
    const exitSelectionMode = () => {
        setSelectedUserIds([]);
        setSelectionMode(false);
    };
    const toggleVisibleSelection = () => {
        if (selectedUserIds.length === visibleUsers.length) {
            setSelectedUserIds([]);
            return;
        }
        setSelectedUserIds(visibleUsers.map((user) => user.id));
    };
    const dropUser = (priority: number) => {
        if (!users || !draggedId) {
            setDraggedId(null);
            return;
        }
        setUsers(users.map((user) => (user.id === draggedId ? { ...user, priority } : user)));
        setDraggedId(null);
    };
    const savePriority = async () => {
        if (!users || !priorityDirty) return;
        setSavingPriority(true);
        setError(null);
        try {
            const changed = users.filter(
                (user) => savedPriorities[user.id] !== (user.priority ?? 4),
            );
            await Promise.all(
                changed.map((user) => api.updateUser(user.id, { priority: user.priority ?? 4 })),
            );
            setSavedPriorities(
                Object.fromEntries(users.map((user) => [user.id, user.priority ?? 4])),
            );
            await load(true);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not save user priority order.");
        } finally {
            setSavingPriority(false);
        }
    };

    const toggleActive = async (user: User) => {
        setBusyId(user.id);
        try {
            const updated = await api.updateUser(user.id, { active: !user.active });
            setUsers(
                (current) =>
                    current?.map((candidate) =>
                        candidate.id === updated.id ? updated : candidate,
                    ) ?? current,
            );
        } catch (err) {
            setError(err instanceof ApiError ? err.message : "Update failed.");
        } finally {
            setBusyId(null);
        }
    };

    const confirmDelete = async () => {
        if (!deleteTarget) return;
        setDeleting(true);
        try {
            await api.deleteUser(deleteTarget.id);
            setUsers(
                (current) => current?.filter((user) => user.id !== deleteTarget.id) ?? current,
            );
            setExpandedId((current) => (current === deleteTarget.id ? null : current));
            setDeleteTarget(null);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Delete failed.");
        } finally {
            setDeleting(false);
        }
    };

    return (
        <div className="space-y-6">
            <header className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-fog-100 font-serif text-2xl font-semibold tracking-tight">
                        Users
                    </h1>
                    <p className="text-fog-400 mt-0.5 text-sm">
                        Each user holds one or more API keys. Set per-user rate limits and budgets;
                        usage is tracked per user.
                    </p>
                </div>
                <div className="flex w-full flex-wrap items-center gap-2 sm:ml-auto sm:w-auto sm:justify-end">
                    <div className="relative min-w-52 flex-1 sm:w-64 sm:flex-none">
                        <Search
                            size={15}
                            aria-hidden="true"
                            className="text-fog-400 pointer-events-none absolute top-1/2 left-3 -translate-y-1/2"
                        />
                        <TextInput
                            type="search"
                            value={userSearch}
                            onChange={(event) => setUserSearch(event.target.value)}
                            placeholder="Search users…"
                            aria-label="Search users by name"
                            className="pl-9"
                        />
                    </div>
                    <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
                        <Button variant="ghost" onClick={() => void load(true)}>
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
                        <Button variant="primary" onClick={() => setShowCreate(true)}>
                            Create user
                        </Button>
                    </div>
                </div>
            </header>

            <PresetsPanel
                presets={presets}
                models={modelOptions}
                onChanged={() => void load(true)}
            />

            {selectionMode ? (
                <BulkPriorityBar
                    entityLabel="user"
                    selectedCount={selectedUserIds.length}
                    visibleCount={visibleUsers.length}
                    priority={bulkPriority}
                    onPriorityChange={setBulkPriority}
                    onSelectVisible={toggleVisibleSelection}
                    onApply={() => void applyBulkPriority()}
                    onCancel={() => setSelectedUserIds([])}
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
            ) : error && !users ? (
                <Card>
                    <ErrorState message={error} onRetry={() => void load()} />
                </Card>
            ) : users && users.length > 0 && visibleUsers.length === 0 ? (
                <Card>
                    <EmptyState message="No users match this search." />
                </Card>
            ) : users && users.length > 0 ? (
                <div className="space-y-8">
                    {priorityLanes.map((priority) => {
                        const laneUsers = visibleUsers.filter(
                            (user) => (user.priority ?? 4) === priority,
                        );
                        return (
                            <section
                                key={priority}
                                aria-labelledby={`user-priority-${priority}`}
                                onDragOver={(event) => event.preventDefault()}
                                onDrop={() => dropUser(priority)}
                                className={`rounded-xl border p-4 transition-colors ${draggedId ? "border-brand-500/50 bg-brand-500/5" : "border-ink-700 bg-ink-950/20"}`}
                            >
                                <div className="mb-4 flex items-center justify-between gap-3">
                                    <div>
                                        <h2
                                            id={`user-priority-${priority}`}
                                            className="text-fog-100 text-sm font-semibold tracking-[0.18em] uppercase"
                                        >
                                            Priority {priority}
                                        </h2>
                                        <p className="text-fog-500 mt-1 text-xs">
                                            {laneUsers.length} user
                                            {laneUsers.length === 1 ? "" : "s"} · drag here to
                                            assign
                                        </p>
                                    </div>
                                    {draggedId ? (
                                        <span className="text-brand-300 text-xs">
                                            Release to move
                                        </span>
                                    ) : null}
                                </div>
                                <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
                                    {laneUsers.map((user) => {
                                        const busy = busyId === user.id;
                                        const expanded = expandedId === user.id;
                                        return (
                                            <div
                                                key={user.id}
                                                draggable
                                                aria-label={`Drag ${user.name} to another priority lane`}
                                                aria-grabbed={draggedId === user.id}
                                                onDragStart={() => setDraggedId(user.id)}
                                                onDragEnd={() => setDraggedId(null)}
                                                onDragOver={(event) => event.preventDefault()}
                                                onDrop={(event) => {
                                                    event.stopPropagation();
                                                    dropUser(priority);
                                                }}
                                                className={
                                                    draggedId === user.id ? "opacity-60" : ""
                                                }
                                            >
                                                <Card
                                                    className={`cursor-grab overflow-hidden transition-shadow active:cursor-grabbing ${selectedUserIds.includes(user.id) ? "border-brand-400/80 shadow-[0_0_0_2px_color-mix(in_srgb,var(--color-brand-500)_35%,transparent)]" : ""}`}
                                                >
                                                    <div className="flex">
                                                        <div
                                                            aria-hidden
                                                            className={`w-1 shrink-0 transition-colors ${user.active ? "bg-good-500" : "bg-bad-500"}`}
                                                        />
                                                        <div className="min-w-0 flex-1">
                                                            <div className="p-5 sm:p-6">
                                                                {/* Header: name + limits + active toggle */}
                                                                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
                                                                    <div className="min-w-0">
                                                                        <div className="flex flex-wrap items-center gap-2">
                                                                            {selectionMode ? (
                                                                                <button
                                                                                    type="button"
                                                                                    onClick={(
                                                                                        event,
                                                                                    ) => {
                                                                                        event.stopPropagation();
                                                                                        toggleUserSelection(
                                                                                            user.id,
                                                                                        );
                                                                                    }}
                                                                                    aria-pressed={selectedUserIds.includes(
                                                                                        user.id,
                                                                                    )}
                                                                                    aria-label={`Select ${user.name}`}
                                                                                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border transition-colors ${selectedUserIds.includes(user.id) ? "border-brand-400 bg-brand-500 text-ink-950" : "border-ink-600 bg-ink-900 hover:border-brand-400 text-transparent"}`}
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
                                                                            <span className="text-fog-100 text-[15px] font-semibold tracking-tight">
                                                                                {user.name}
                                                                            </span>
                                                                            <PolicyBadges
                                                                                user={user}
                                                                                presetName={
                                                                                    presets.find(
                                                                                        (preset) =>
                                                                                            preset.id ===
                                                                                            user.preset_id,
                                                                                    )?.name
                                                                                }
                                                                            />
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-xs">
                                                                            Added{" "}
                                                                            {formatDateTime(
                                                                                user.created_at,
                                                                            )}
                                                                        </div>
                                                                    </div>
                                                                    <StatusToggle
                                                                        on={user.active}
                                                                        busy={busy}
                                                                        onClick={() =>
                                                                            void toggleActive(user)
                                                                        }
                                                                        srLabel={
                                                                            user.active
                                                                                ? "Deactivate user"
                                                                                : "Activate user"
                                                                        }
                                                                    />
                                                                </div>

                                                                {/* Stats row */}
                                                                <div className="bg-ink-700 mt-4 grid gap-px overflow-hidden rounded-lg sm:grid-cols-2 xl:grid-cols-2">
                                                                    <div className="bg-ink-900 flex-1 px-4 py-3">
                                                                        <div className="text-fog-100 font-mono text-base font-semibold tracking-tight tabular-nums">
                                                                            {formatTokens(
                                                                                user.total_tokens,
                                                                            )}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-[11px] tracking-wider uppercase">
                                                                            All-time tokens
                                                                        </div>
                                                                        {user.lifetime_token_budget ? (
                                                                            <div className="text-fog-500 mt-1 text-[10px]">
                                                                                Limit{" "}
                                                                                {formatTokens(
                                                                                    user.lifetime_token_budget,
                                                                                )}
                                                                            </div>
                                                                        ) : null}
                                                                    </div>
                                                                    <div className="bg-ink-900 flex-1 px-4 py-3">
                                                                        <div className="text-fog-100 font-mono text-base font-semibold tracking-tight tabular-nums">
                                                                            {formatUsd(
                                                                                user.total_spend_usd,
                                                                            )}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-[11px] tracking-wider uppercase">
                                                                            All-time spend
                                                                        </div>
                                                                        {user.lifetime_spend_budget_usd ? (
                                                                            <div className="text-fog-500 mt-1 text-[10px]">
                                                                                Limit{" "}
                                                                                {formatUsd(
                                                                                    user.lifetime_spend_budget_usd,
                                                                                )}
                                                                            </div>
                                                                        ) : null}
                                                                    </div>
                                                                    <div className="bg-ink-900 flex-1 px-4 py-3">
                                                                        <div className="text-fog-100 font-mono text-base font-semibold tracking-tight tabular-nums">
                                                                            {formatNumber(
                                                                                user.total_requests,
                                                                            )}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-[11px] tracking-wider uppercase">
                                                                            Requests
                                                                        </div>
                                                                    </div>
                                                                    <div className="bg-ink-900 flex-1 px-4 py-3">
                                                                        <div className="text-fog-100 font-mono text-base font-semibold tracking-tight tabular-nums">
                                                                            {formatDateTime(
                                                                                user.last_used_at,
                                                                            )}
                                                                        </div>
                                                                        <div className="text-fog-400 mt-0.5 text-[11px] tracking-wider uppercase">
                                                                            Last used
                                                                        </div>
                                                                    </div>
                                                                </div>

                                                                <MonthlyUsage user={user} />

                                                                {/* Keys toggle + actions */}
                                                                <div className="border-ink-700 mt-4 flex flex-wrap items-center justify-between gap-2 border-t pt-3.5">
                                                                    <button
                                                                        onClick={() =>
                                                                            setExpandedId(
                                                                                expanded
                                                                                    ? null
                                                                                    : user.id,
                                                                            )
                                                                        }
                                                                        className="border-ink-700 text-fog-300 hover:bg-ink-800 hover:text-fog-100 hover:border-ink-600 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors"
                                                                    >
                                                                        <KeyRound className="h-3.5 w-3.5" />
                                                                        <span className="font-mono tabular-nums">
                                                                            {user.key_count}
                                                                        </span>{" "}
                                                                        key
                                                                        {user.key_count === 1
                                                                            ? ""
                                                                            : "s"}
                                                                        <ChevronRight
                                                                            className={`text-fog-400 h-3 w-3 transition-transform ${expanded ? "rotate-90" : ""}`}
                                                                        />
                                                                    </button>
                                                                    <div className="flex items-center gap-1">
                                                                        {busy ? (
                                                                            <Spinner className="mr-1" />
                                                                        ) : null}
                                                                        <button
                                                                            onClick={() =>
                                                                                setEditTarget(user)
                                                                            }
                                                                            disabled={busy}
                                                                            className="text-fog-400 hover:bg-ink-800 hover:text-fog-100 inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors disabled:opacity-50"
                                                                            title="Edit user"
                                                                        >
                                                                            <Pencil className="h-[15px] w-[15px]" />
                                                                        </button>
                                                                        <button
                                                                            onClick={() =>
                                                                                setDeleteTarget(
                                                                                    user,
                                                                                )
                                                                            }
                                                                            disabled={busy}
                                                                            className="text-fog-400 hover:bg-bad-500/10 hover:text-bad-500 inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors disabled:opacity-50"
                                                                            title="Delete user"
                                                                        >
                                                                            <Trash2 className="h-[15px] w-[15px]" />
                                                                        </button>
                                                                    </div>
                                                                </div>
                                                            </div>

                                                            {expanded ? (
                                                                <div className="border-ink-700 bg-ink-900/40 border-t p-4 sm:px-6">
                                                                    <KeysPanel
                                                                        user={user}
                                                                        onChanged={() =>
                                                                            void load(true)
                                                                        }
                                                                        onRevealed={(
                                                                            label,
                                                                            secret,
                                                                        ) =>
                                                                            setRevealed({
                                                                                name: user.name,
                                                                                label,
                                                                                secret,
                                                                            })
                                                                        }
                                                                    />
                                                                </div>
                                                            ) : null}
                                                        </div>
                                                    </div>
                                                </Card>
                                            </div>
                                        );
                                    })}
                                </div>
                            </section>
                        );
                    })}
                </div>
            ) : (
                <Card>
                    <EmptyState message="No users yet. Create one, then issue API keys." />
                </Card>
            )}

            {showCreate ? (
                <CreateUserModal
                    presets={presets}
                    modelOptions={modelOptions}
                    onClose={() => setShowCreate(false)}
                    onCreated={(user) => {
                        setShowCreate(false);
                        setExpandedId(user.id);
                        setUsers((current) => (current ? [...current, user] : [user]));
                    }}
                />
            ) : null}

            {editTarget ? (
                <EditUserModal
                    user={editTarget}
                    presets={presets}
                    modelOptions={modelOptions}
                    onClose={() => setEditTarget(null)}
                    onSaved={(updated) => {
                        setEditTarget(null);
                        setUsers(
                            (current) =>
                                current?.map((user) => (user.id === updated.id ? updated : user)) ??
                                current,
                        );
                    }}
                />
            ) : null}

            {deleteTarget ? (
                <ConfirmDialog
                    title="Delete user"
                    message={`Delete "${deleteTarget.name}"? All of their API keys stop working immediately and their usage history is removed. This cannot be undone.`}
                    confirmLabel="Delete"
                    busy={deleting}
                    onConfirm={() => void confirmDelete()}
                    onCancel={() => setDeleteTarget(null)}
                />
            ) : null}

            {revealed ? (
                <KeyRevealModal
                    name={revealed.name}
                    label={revealed.label}
                    secret={revealed.secret}
                    onClose={() => setRevealed(null)}
                />
            ) : null}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Per-user key management panel (shown when a user card is expanded).
// ---------------------------------------------------------------------------

function KeysPanel({
    user,
    onChanged,
    onRevealed,
}: {
    user: User;
    onChanged: () => void;
    onRevealed: (label: string, secret: string) => void;
}) {
    const [keys, setKeys] = useState<ApiKey[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [creating, setCreating] = useState(false);
    const [newLabel, setNewLabel] = useState("");
    const [newRate, setNewRate] = useState("");
    const [newBudget, setNewBudget] = useState("");
    const [editTarget, setEditTarget] = useState<ApiKey | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<ApiKey | null>(null);

    const load = useCallback(async () => {
        setError(null);
        try {
            setKeys(await api.keys(user.id));
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to load keys.");
        }
    }, [user.id]);

    useEffect(() => {
        void load();
    }, [load]);

    const create = async () => {
        if (!newLabel.trim()) {
            setError("Enter a label before creating a key.");
            return;
        }
        setCreating(true);
        setError(null);
        try {
            const res = await api.createKey(user.id, {
                label: newLabel,
                rate_limit_per_minute: parseLimitInput(newRate),
                monthly_token_budget: parseLimitInput(newBudget),
            });
            setNewLabel("");
            setNewRate("");
            setNewBudget("");
            setKeys((current) => (current ? [...current, res.api_key] : [res.api_key]));
            onRevealed(res.api_key.label ?? "key", res.secret);
            onChanged();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to create key.");
        } finally {
            setCreating(false);
        }
    };

    const toggle = async (key: ApiKey) => {
        setBusyId(key.id);
        try {
            const updated = await api.updateKey(user.id, key.id, { active: !key.active });
            setKeys(
                (current) =>
                    current?.map((candidate) =>
                        candidate.id === updated.id ? updated : candidate,
                    ) ?? current,
            );
        } catch (err) {
            setError(err instanceof Error ? err.message : "Update failed.");
        } finally {
            setBusyId(null);
        }
    };

    const confirmDelete = async () => {
        if (!deleteTarget) return;
        setBusyId(deleteTarget.id);
        try {
            await api.deleteKey(user.id, deleteTarget.id);
            setKeys((current) => current?.filter((key) => key.id !== deleteTarget.id) ?? current);
            setDeleteTarget(null);
            onChanged();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Delete failed.");
        } finally {
            setBusyId(null);
        }
    };

    return (
        <div className="space-y-3">
            {error ? (
                <div
                    role="alert"
                    className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-xs"
                >
                    {error}
                </div>
            ) : null}

            {keys === null ? (
                <LoadingState label="Loading keys…" />
            ) : keys.length > 0 ? (
                <div className="space-y-1.5">
                    <div className="text-fog-400 mb-2 text-[11px] font-semibold tracking-wider uppercase">
                        API Keys
                    </div>
                    {keys.map((key) => (
                        <div
                            key={key.id}
                            className="border-ink-700 bg-ink-850 flex flex-col gap-3 rounded-lg border px-3.5 py-3 sm:flex-row sm:items-center sm:justify-between"
                        >
                            <div className="flex min-w-0 items-center gap-3">
                                <div className="bg-ink-800 text-fog-400 flex h-7 w-7 shrink-0 items-center justify-center rounded-md">
                                    <KeyRound className="h-3.5 w-3.5" />
                                </div>
                                <div className="min-w-0">
                                    <div className="flex flex-wrap items-center gap-2">
                                        <span className="text-fog-100 truncate text-[13px] font-medium">
                                            {key.label || "Unlabeled (legacy)"}
                                        </span>
                                        <code className="bg-ink-800 text-fog-400 rounded px-1.5 py-0.5 font-mono text-[11px]">
                                            {key.key_prefix}…
                                        </code>
                                    </div>
                                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                                        <LimitBadges
                                            rate={key.rate_limit_per_minute}
                                            budget={key.monthly_token_budget}
                                        />
                                        <span className="text-fog-400 text-[11px]">
                                            Last used {formatDateTime(key.last_used_at)}
                                        </span>
                                    </div>
                                </div>
                            </div>
                            <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                                <StatusToggle
                                    on={key.active}
                                    busy={busyId === key.id}
                                    onClick={() => void toggle(key)}
                                    onLabel="Enabled"
                                    offLabel="Disabled"
                                    srLabel={key.active ? "Disable key" : "Enable key"}
                                />
                                <button
                                    disabled={busyId === key.id}
                                    onClick={() => setEditTarget(key)}
                                    className="text-fog-400 hover:bg-ink-800 hover:text-fog-100 inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors disabled:opacity-50"
                                    title="Edit key"
                                >
                                    <Pencil className="h-3.5 w-3.5" />
                                </button>
                                <button
                                    disabled={busyId === key.id}
                                    onClick={() => setDeleteTarget(key)}
                                    className="text-fog-400 hover:bg-bad-500/10 hover:text-bad-500 inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors disabled:opacity-50"
                                    title="Delete key"
                                >
                                    <Trash2 className="h-3.5 w-3.5" />
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            ) : (
                <div className="border-ink-600 text-fog-400 rounded-lg border border-dashed px-3 py-4 text-center text-xs">
                    No keys yet. Create one below.
                </div>
            )}

            {/* New key */}
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_7rem_8rem_auto] sm:items-end">
                <TextInput
                    value={newLabel}
                    onChange={(e) => setNewLabel(e.target.value)}
                    placeholder="Key label (required, e.g. laptop, CI)"
                    aria-label="Required key label"
                />
                <TextInput
                    type="number"
                    min={0}
                    value={newRate}
                    onChange={(e) => setNewRate(e.target.value)}
                    placeholder="req/min"
                />
                <TextInput
                    type="number"
                    min={0}
                    value={newBudget}
                    onChange={(e) => setNewBudget(e.target.value)}
                    placeholder="tokens/mo"
                />
                <Button
                    variant="primary"
                    disabled={creating || !newLabel.trim()}
                    onClick={() => void create()}
                >
                    {creating ? <Spinner /> : null}
                    New key
                </Button>
            </div>
            <p className="text-fog-400 text-[11px]">
                Optional per-key limits: requests per minute and a monthly token budget. Leave blank
                to use the server default; set 0 for unlimited. Per-user limits (set on the user)
                apply on top, across all their keys.
            </p>

            {editTarget ? (
                <EditKeyModal
                    userId={user.id}
                    apiKey={editTarget}
                    onClose={() => setEditTarget(null)}
                    onSaved={(updated) => {
                        setEditTarget(null);
                        setKeys(
                            (current) =>
                                current?.map((key) => (key.id === updated.id ? updated : key)) ??
                                current,
                        );
                        onChanged();
                    }}
                />
            ) : null}

            {deleteTarget ? (
                <ConfirmDialog
                    title="Delete key"
                    message={`Delete the key "${deleteTarget.label ?? "Unlabeled (legacy)"}" (${deleteTarget.key_prefix}…)? It stops working immediately.`}
                    confirmLabel="Delete"
                    busy={busyId === deleteTarget.id}
                    onConfirm={() => void confirmDelete()}
                    onCancel={() => setDeleteTarget(null)}
                />
            ) : null}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Edit key modal (label + per-key rate limit and monthly token budget).
// ---------------------------------------------------------------------------

function EditKeyModal({
    userId,
    apiKey,
    onClose,
    onSaved,
}: {
    userId: string;
    apiKey: ApiKey;
    onClose: () => void;
    onSaved: (apiKey: ApiKey) => void;
}) {
    const [label, setLabel] = useState(apiKey.label ?? "");
    const [rate, setRate] = useState(
        apiKey.rate_limit_per_minute ? String(apiKey.rate_limit_per_minute) : "",
    );
    const [budget, setBudget] = useState(
        apiKey.monthly_token_budget ? String(apiKey.monthly_token_budget) : "",
    );
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setSubmitting(true);
        try {
            const updated = await api.updateKey(userId, apiKey.id, {
                label: label.trim(),
                // For keys, null (use server default) and 0 (unlimited) differ, so a blank
                // field must leave the limit UNCHANGED rather than silently forcing 0.
                rate_limit_per_minute: parseLimitInput(rate) ?? undefined,
                monthly_token_budget: parseLimitInput(budget) ?? undefined,
            });
            onSaved(updated);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Save failed.");
            setSubmitting(false);
        }
    };

    return (
        <Modal title="Edit key" onClose={onClose}>
            <form onSubmit={submit} className="space-y-4">
                <Field label="Label">
                    <TextInput
                        value={label}
                        onChange={(e) => setLabel(e.target.value)}
                        placeholder="e.g. laptop"
                    />
                </Field>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field
                        label="Rate limit (req / min)"
                        hint="0 = unlimited. Blank leaves it unchanged."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            value={rate}
                            onChange={(e) => setRate(e.target.value)}
                            placeholder="unchanged"
                        />
                    </Field>
                    <Field
                        label="Monthly token budget"
                        hint="0 = unlimited. Blank leaves it unchanged."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            value={budget}
                            onChange={(e) => setBudget(e.target.value)}
                            placeholder="unchanged"
                        />
                    </Field>
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
                    <Button type="submit" variant="primary" disabled={submitting}>
                        {submitting ? <Spinner /> : null}
                        Save
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Create user modal (name + optional per-user limits; keys issued afterwards).
// ---------------------------------------------------------------------------

function CreateUserModal({
    presets,
    modelOptions,
    onClose,
    onCreated,
}: {
    presets: Preset[];
    modelOptions: string[];
    onClose: () => void;
    onCreated: (user: User) => void;
}) {
    const [name, setName] = useState("");
    const [priority, setPriority] = useState("1");
    const [fallbackEnabled, setFallbackEnabled] = useState(presets[0]?.fallback_enabled ?? false);
    const [spareCapacityOnly, setSpareCapacityOnly] = useState(false);
    const [rate, setRate] = useState("");
    const [hourlyRate, setHourlyRate] = useState("");
    const [dailyRate, setDailyRate] = useState("");
    const [budget, setBudget] = useState("");
    const [lifetimeBudget, setLifetimeBudget] = useState("");
    const [monthlySpend, setMonthlySpend] = useState("");
    const [lifetimeSpend, setLifetimeSpend] = useState("");
    const [presetId, setPresetId] = useState(presets[0]?.id ?? "");
    const [allowedModels, setAllowedModels] = useState<string[] | null>(
        presets[0]?.allowed_models ?? null,
    );
    const [modelOverrides, setModelOverrides] = useState<Record<string, string>>(
        presets[0]?.model_overrides ?? {},
    );
    const [allowedThinkingLevels, setAllowedThinkingLevels] = useState<ThinkingLevel[]>(
        presets[0]?.allowed_thinking_levels ?? [...THINKING_LEVELS],
    );
    const [allowedModes, setAllowedModes] = useState<ThinkingMode[]>(
        presets[0]?.allowed_thinking_modes ?? ["disabled", "enabled", "adaptive"],
    );
    const [modelLevels, setModelLevels] = useState<Record<string, ThinkingLevel[]>>(
        presets[0]?.model_thinking_levels ?? {},
    );
    const [modelModes, setModelModes] = useState<Record<string, ThinkingMode[]>>(
        presets[0]?.model_thinking_modes ?? {},
    );
    const [allowExtendedContext, setAllowExtendedContext] = useState(
        presets[0]?.allow_extended_context ?? false,
    );
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setSubmitting(true);
        try {
            const user = await api.createUser(name.trim(), {
                priority: Number(priority) || 1,
                spare_capacity_only: spareCapacityOnly,
                rate_limit_per_minute: parseLimitInput(rate),
                rate_limit_per_hour: parseLimitInput(hourlyRate),
                rate_limit_per_day: parseLimitInput(dailyRate),
                monthly_token_budget: parseLimitInput(budget),
                lifetime_token_budget: parseLimitInput(lifetimeBudget),
                monthly_spend_budget_usd: parseMoneyInput(monthlySpend),
                lifetime_spend_budget_usd: parseMoneyInput(lifetimeSpend),
                preset_id: presetId || null,
                ...(!presetId
                    ? {
                          fallback_enabled: fallbackEnabled,
                          allowed_models: allowedModels,
                          model_overrides: modelOverrides,
                          allowed_thinking_levels: allowedThinkingLevels,
                          allowed_thinking_modes: allowedModes,
                          model_thinking_levels: modelLevels,
                          model_thinking_modes: modelModes,
                          allow_extended_context: allowExtendedContext,
                      }
                    : {}),
            });
            onCreated(user);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Create failed.");
            setSubmitting(false);
        }
    };

    return (
        <Modal title="Create user" onClose={onClose} widthClass="max-w-3xl">
            <form onSubmit={submit} className="space-y-4">
                <Field label="Name" hint="Issue one or more API keys after creating the user.">
                    <TextInput
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="e.g. Jane Doe"
                        autoFocus
                        required
                    />
                </Field>

                <div className="space-y-3">
                    <Field
                        label="User priority"
                        hint="Lower numbers are served first when requests queue."
                    >
                        <TextInput
                            type="number"
                            min={1}
                            max={1000}
                            value={priority}
                            onChange={(e) => setPriority(e.target.value)}
                        />
                    </Field>
                    <SpareCapacityToggle
                        checked={spareCapacityOnly}
                        onChange={setSpareCapacityOnly}
                    />
                </div>

                <Field label="Policy preset">
                    <SelectMenu
                        value={presetId}
                        ariaLabel="Policy preset for new user"
                        onChange={(value) => {
                            setPresetId(value);
                            const preset = presets.find((item) => item.id === value);
                            if (preset) {
                                setFallbackEnabled(preset.fallback_enabled);
                                setAllowedModels(preset.allowed_models);
                                setModelOverrides(preset.model_overrides);
                                setAllowedThinkingLevels(preset.allowed_thinking_levels);
                                setAllowedModes(preset.allowed_thinking_modes);
                                setModelLevels(preset.model_thinking_levels);
                                setModelModes(preset.model_thinking_modes);
                                setAllowExtendedContext(preset.allow_extended_context);
                            }
                        }}
                        options={[
                            { value: "", label: "No preset" },
                            ...presets.map((preset) => ({ value: preset.id, label: preset.name })),
                        ]}
                    />
                </Field>

                {!presetId && (
                    <UserPolicyFields
                        fallbackEnabled={fallbackEnabled}
                        onFallbackEnabled={setFallbackEnabled}
                        modelOptions={modelOptions}
                        allowedModels={allowedModels}
                        onAllowedModels={setAllowedModels}
                        thinkingLevels={allowedThinkingLevels}
                        onThinkingLevels={setAllowedThinkingLevels}
                        thinkingModes={allowedModes}
                        onThinkingModes={setAllowedModes}
                        modelOverrides={modelOverrides}
                        onModelOverrides={setModelOverrides}
                        modelLevels={modelLevels}
                        onModelLevels={setModelLevels}
                        modelModes={modelModes}
                        onModelModes={setModelModes}
                        allowExtendedContext={allowExtendedContext}
                        onAllowExtendedContext={setAllowExtendedContext}
                    />
                )}

                <UserRateLimitFields
                    minute={rate}
                    hour={hourlyRate}
                    day={dailyRate}
                    onMinute={setRate}
                    onHour={setHourlyRate}
                    onDay={setDailyRate}
                />

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field label="Monthly token budget" hint="Optional. Blank = unlimited.">
                        <TextInput
                            type="number"
                            min={0}
                            value={budget}
                            onChange={(e) => setBudget(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                    <Field label="Lifetime token budget" hint="One-time cap. Blank = unlimited.">
                        <TextInput
                            type="number"
                            min={0}
                            value={lifetimeBudget}
                            onChange={(e) => setLifetimeBudget(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                    <Field
                        label="Monthly spend budget (USD)"
                        hint="API-equivalent value. Blank = unlimited."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            step="0.01"
                            value={monthlySpend}
                            onChange={(e) => setMonthlySpend(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                    <Field
                        label="Lifetime spend budget (USD)"
                        hint="One-time API-equivalent cap. Blank = unlimited."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            step="0.01"
                            value={lifetimeSpend}
                            onChange={(e) => setLifetimeSpend(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
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
                    <Button
                        type="submit"
                        variant="primary"
                        disabled={
                            submitting ||
                            !name.trim() ||
                            (!presetId &&
                                (allowedModes.length === 0 ||
                                    allowedThinkingLevels.length === 0 ||
                                    (allowedModels !== null && allowedModels.length === 0)))
                        }
                    >
                        {submitting ? <Spinner /> : null}
                        Create
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Edit user modal (name + active + per-user rate limit and token budget).
// ---------------------------------------------------------------------------

function EditUserModal({
    user,
    presets,
    modelOptions,
    onClose,
    onSaved,
}: {
    user: User;
    presets: Preset[];
    modelOptions: string[];
    onClose: () => void;
    onSaved: (user: User) => void;
}) {
    const [name, setName] = useState(user.name);
    const [active, setActive] = useState(user.active);
    const [priority, setPriority] = useState(String(user.priority ?? 1));
    const [fallbackEnabled, setFallbackEnabled] = useState(user.fallback_enabled ?? false);
    const [spareCapacityOnly, setSpareCapacityOnly] = useState(user.spare_capacity_only ?? false);
    const [rate, setRate] = useState(
        user.rate_limit_per_minute ? String(user.rate_limit_per_minute) : "",
    );
    const [hourlyRate, setHourlyRate] = useState(
        user.rate_limit_per_hour ? String(user.rate_limit_per_hour) : "",
    );
    const [dailyRate, setDailyRate] = useState(
        user.rate_limit_per_day ? String(user.rate_limit_per_day) : "",
    );
    const [budget, setBudget] = useState(
        user.monthly_token_budget ? String(user.monthly_token_budget) : "",
    );
    const [lifetimeBudget, setLifetimeBudget] = useState(
        user.lifetime_token_budget ? String(user.lifetime_token_budget) : "",
    );
    const [monthlySpend, setMonthlySpend] = useState(
        user.monthly_spend_budget_usd ? String(user.monthly_spend_budget_usd) : "",
    );
    const [lifetimeSpend, setLifetimeSpend] = useState(
        user.lifetime_spend_budget_usd ? String(user.lifetime_spend_budget_usd) : "",
    );
    const [selectedPresetId, setSelectedPresetId] = useState(user.preset_id ?? "");
    const [overrideFields, setOverrideFields] = useState<string[]>(user.preset_overrides);
    const toggleOverride = (field: string) =>
        setOverrideFields((current) =>
            current.includes(field)
                ? current.filter((item) => item !== field)
                : [...current, field],
        );
    const [modelOverrides, setModelOverrides] = useState<Record<string, string>>({
        ...user.model_overrides,
    });
    const [allowedThinkingLevels, setAllowedThinkingLevels] = useState<ThinkingLevel[]>(
        user.allowed_thinking_levels,
    );
    const [allowedModels, setAllowedModels] = useState<string[] | null>(user.allowed_models);
    const [allowedModes, setAllowedModes] = useState<ThinkingMode[]>(user.allowed_thinking_modes);
    const [modelLevels, setModelLevels] = useState<Record<string, ThinkingLevel[]>>(
        user.model_thinking_levels,
    );
    const [modelModes, setModelModes] = useState<Record<string, ThinkingMode[]>>(
        user.model_thinking_modes,
    );
    const [allowExtendedContext, setAllowExtendedContext] = useState(user.allow_extended_context);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setSubmitting(true);
        try {
            const presetChanged = selectedPresetId !== (user.preset_id ?? "");
            const shouldUseOverride = (field: string) =>
                !selectedPresetId || overrideFields.includes(field);
            const updated = await api.updateUser(user.id, {
                ...(presetChanged ? { preset_id: selectedPresetId || null } : {}),
                clear_preset_overrides:
                    presetChanged || !selectedPresetId
                        ? []
                        : user.preset_overrides.filter((field) => !overrideFields.includes(field)),
                name: name.trim(),
                active,
                priority: Number(priority) || 1,
                spare_capacity_only: spareCapacityOnly,
                rate_limit_per_minute: parseLimitInput(rate) ?? 0,
                rate_limit_per_hour: parseLimitInput(hourlyRate) ?? 0,
                rate_limit_per_day: parseLimitInput(dailyRate) ?? 0,
                monthly_token_budget: parseLimitInput(budget) ?? 0,
                lifetime_token_budget: parseLimitInput(lifetimeBudget) ?? 0,
                monthly_spend_budget_usd: parseMoneyInput(monthlySpend) ?? 0,
                lifetime_spend_budget_usd: parseMoneyInput(lifetimeSpend) ?? 0,
                ...(shouldUseOverride("model_overrides")
                    ? { model_overrides: modelOverrides }
                    : {}),
                ...(shouldUseOverride("allowed_thinking_levels")
                    ? { allowed_thinking_levels: allowedThinkingLevels }
                    : {}),
                ...(shouldUseOverride("fallback_enabled")
                    ? { fallback_enabled: fallbackEnabled }
                    : {}),
                ...(shouldUseOverride("allowed_models") ? { allowed_models: allowedModels } : {}),
                ...(shouldUseOverride("allowed_thinking_modes")
                    ? { allowed_thinking_modes: allowedModes }
                    : {}),
                ...(shouldUseOverride("model_thinking_levels")
                    ? { model_thinking_levels: modelLevels }
                    : {}),
                ...(shouldUseOverride("model_thinking_modes")
                    ? { model_thinking_modes: modelModes }
                    : {}),
                ...(shouldUseOverride("allow_extended_context")
                    ? { allow_extended_context: allowExtendedContext }
                    : {}),
            });
            onSaved(updated);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Save failed.");
            setSubmitting(false);
        }
    };

    return (
        <Modal title={`Edit ${user.name}`} onClose={onClose} widthClass="max-w-3xl">
            <form onSubmit={submit} className="space-y-4">
                <Field label="Name">
                    <TextInput value={name} onChange={(e) => setName(e.target.value)} required />
                </Field>

                <div className="space-y-3">
                    <Field
                        label="User priority"
                        hint="Lower numbers are served first when requests queue."
                    >
                        <TextInput
                            type="number"
                            min={1}
                            max={1000}
                            value={priority}
                            onChange={(e) => setPriority(e.target.value)}
                        />
                    </Field>
                    <SpareCapacityToggle
                        checked={spareCapacityOnly}
                        onChange={setSpareCapacityOnly}
                    />
                </div>

                <UserRateLimitFields
                    minute={rate}
                    hour={hourlyRate}
                    day={dailyRate}
                    onMinute={setRate}
                    onHour={setHourlyRate}
                    onDay={setDailyRate}
                />

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field
                        label="Monthly token budget"
                        hint="Across all the user's keys. 0 or blank = unlimited."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            value={budget}
                            onChange={(e) => setBudget(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                    <Field
                        label="Lifetime token budget"
                        hint="One-time cap. 0 or blank = unlimited."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            value={lifetimeBudget}
                            onChange={(e) => setLifetimeBudget(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                    <Field
                        label="Monthly spend budget (USD)"
                        hint="API-equivalent value. 0 or blank = unlimited."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            step="0.01"
                            value={monthlySpend}
                            onChange={(e) => setMonthlySpend(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                    <Field
                        label="Lifetime spend budget (USD)"
                        hint="One-time API-equivalent cap. 0 or blank = unlimited."
                    >
                        <TextInput
                            type="number"
                            min={0}
                            step="0.01"
                            value={lifetimeSpend}
                            onChange={(e) => setLifetimeSpend(e.target.value)}
                            placeholder="unlimited"
                        />
                    </Field>
                </div>

                <Field label="Policy preset">
                    <SelectMenu
                        value={selectedPresetId}
                        ariaLabel="Policy preset"
                        onChange={(value) => {
                            if (value === selectedPresetId) return;
                            if (
                                value &&
                                overrideFields.length > 0 &&
                                !window.confirm(
                                    "Changing presets will clear this user's policy overrides. Continue?",
                                )
                            )
                                return;
                            const preset = presets.find((item) => item.id === value);
                            setSelectedPresetId(value);
                            setOverrideFields([]);
                            if (preset) {
                                setFallbackEnabled(preset.fallback_enabled);
                                setAllowedModels(preset.allowed_models);
                                setModelOverrides(preset.model_overrides);
                                setAllowedThinkingLevels(preset.allowed_thinking_levels);
                                setAllowedModes(preset.allowed_thinking_modes);
                                setModelLevels(preset.model_thinking_levels);
                                setModelModes(preset.model_thinking_modes);
                                setAllowExtendedContext(preset.allow_extended_context);
                            }
                        }}
                        options={[
                            { value: "", label: "No preset" },
                            ...presets.map((preset) => ({
                                value: preset.id,
                                label: preset.name,
                            })),
                        ]}
                    />
                </Field>
                {selectedPresetId && (
                    <div className="border-ink-700 bg-ink-950/30 space-y-3 rounded-lg border p-4">
                        <div className="text-fog-200 text-sm font-medium">User overrides</div>
                        {(
                            [
                                ["fallback_enabled", "API fallback providers"],
                                ["allowed_models", "Allowed models"],
                                ["allowed_thinking_levels", "Thinking levels"],
                                ["allowed_thinking_modes", "Thinking modes"],
                                ["model_overrides", "Model rewrites"],
                                ["model_thinking_levels", "Per-model thinking levels"],
                                ["model_thinking_modes", "Per-model thinking modes"],
                                ["allow_extended_context", "Extended context window"],
                            ] as const
                        ).map(([field, label]) => (
                            <label
                                key={field}
                                className="text-fog-200 flex items-center gap-2.5 text-sm"
                            >
                                <input
                                    type="checkbox"
                                    checked={overrideFields.includes(field)}
                                    onChange={() => toggleOverride(field)}
                                    className="accent-brand-500 h-4 w-4"
                                />
                                Override {label.toLowerCase()}
                            </label>
                        ))}
                    </div>
                )}

                {(!selectedPresetId || overrideFields.length > 0) && (
                    <UserPolicyFields
                        fallbackEnabled={fallbackEnabled}
                        onFallbackEnabled={setFallbackEnabled}
                        modelOptions={modelOptions}
                        visible={selectedPresetId ? overrideFields : undefined}
                        allowedModels={allowedModels}
                        onAllowedModels={setAllowedModels}
                        thinkingLevels={allowedThinkingLevels}
                        onThinkingLevels={setAllowedThinkingLevels}
                        thinkingModes={allowedModes}
                        onThinkingModes={setAllowedModes}
                        modelOverrides={modelOverrides}
                        onModelOverrides={setModelOverrides}
                        modelLevels={modelLevels}
                        onModelLevels={setModelLevels}
                        modelModes={modelModes}
                        onModelModes={setModelModes}
                        allowExtendedContext={allowExtendedContext}
                        onAllowExtendedContext={setAllowExtendedContext}
                    />
                )}

                <label className="border-ink-700 bg-ink-900/50 flex items-center gap-2.5 rounded-md border px-3 py-2.5">
                    <input
                        type="checkbox"
                        checked={active}
                        onChange={(e) => setActive(e.target.checked)}
                        className="border-ink-600 bg-ink-900 accent-brand-500 h-4 w-4 rounded"
                    />
                    <span className="text-fog-200 text-sm">
                        Active (deactivating disables all of this user&apos;s keys)
                    </span>
                </label>

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
                    <Button
                        type="submit"
                        variant="primary"
                        disabled={
                            submitting ||
                            !name.trim() ||
                            (!selectedPresetId &&
                                (allowedModes.length === 0 ||
                                    allowedThinkingLevels.length === 0 ||
                                    (allowedModels !== null && allowedModels.length === 0)))
                        }
                    >
                        {submitting ? <Spinner /> : null}
                        Save
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// One-time key reveal: shows the secret once, with shell + settings.json setup.
// ---------------------------------------------------------------------------

function KeyRevealModal({
    name,
    label,
    secret,
    onClose,
}: {
    name: string;
    label: string;
    secret: string;
    onClose: () => void;
}) {
    const [os, setOs] = useState<"unix" | "windows">("unix");

    // API_BASE_URL is relative ("/api") in production; prefix the current origin for a full URL.
    const baseUrl =
        API_BASE_URL.startsWith("http") || typeof window === "undefined"
            ? API_BASE_URL
            : `${window.location.origin}${API_BASE_URL}`;

    const origin = typeof window === "undefined" ? "" : window.location.origin;
    // The one-time secret is deliberately not interpolated into either command:
    // shell command text and process arguments are commonly retained in history
    // and can be observed by other local processes. Both installers prompt for
    // the key without echoing it.
    const unixCmd = `curl -fsSL ${origin}/install.sh | bash -s -- ${baseUrl}`;
    const winCmd = `$env:CC_PROXY_URL = '${baseUrl.replaceAll("'", "''")}'; irm '${origin.replaceAll("'", "''")}/install.ps1' | iex`;
    const installCmd = os === "unix" ? unixCmd : winCmd;

    return (
        <Modal title="API key" onClose={onClose} widthClass="max-w-xl">
            <div className="space-y-4">
                <div
                    role="status"
                    className="border-warn-500/30 bg-warn-500/10 text-warn-500 rounded-md border px-3 py-2 text-sm"
                >
                    Copy this key now — it is shown only once and cannot be retrieved later.
                </div>

                <div>
                    <div className="text-fog-300 mb-1.5 text-xs font-medium">
                        Key &quot;{label}&quot; for {name}
                    </div>
                    <div className="flex items-center gap-2">
                        <code className="border-ink-600 bg-ink-900 text-fog-100 block flex-1 overflow-x-auto rounded-md border px-3 py-2 font-mono text-xs whitespace-nowrap">
                            {secret}
                        </code>
                        <CopyButton text={secret} label="Copy" />
                    </div>
                </div>

                <div>
                    <div className="mb-1.5 flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <span className="text-fog-300 text-xs font-medium">Quick setup</span>
                            <div className="border-ink-600 flex overflow-hidden rounded-md border">
                                <button
                                    onClick={() => setOs("unix")}
                                    className={`px-2 py-0.5 text-[11px] font-medium transition-colors ${
                                        os === "unix"
                                            ? "bg-ink-700 text-fog-100"
                                            : "bg-ink-900 text-fog-400 hover:text-fog-200"
                                    }`}
                                >
                                    macOS / Linux
                                </button>
                                <button
                                    onClick={() => setOs("windows")}
                                    className={`border-ink-600 border-l px-2 py-0.5 text-[11px] font-medium transition-colors ${
                                        os === "windows"
                                            ? "bg-ink-700 text-fog-100"
                                            : "bg-ink-900 text-fog-400 hover:text-fog-200"
                                    }`}
                                >
                                    Windows
                                </button>
                            </div>
                        </div>
                        <CopyButton text={installCmd} label="Copy" />
                    </div>
                    <pre className="border-ink-600 bg-ink-900 text-fog-200 overflow-x-auto rounded-md border px-3 py-2 font-mono text-xs leading-relaxed">
                        {installCmd}
                    </pre>
                    <p className="text-fog-400 mt-1.5 text-[11px] leading-relaxed">
                        {os === "unix" ? (
                            <>
                                Configures{" "}
                                <code className="font-mono">~/.claude/settings.json</code> with the
                                proxy URL and API key while preserving every unrelated setting. It
                                prompts for the API key with input hidden. Requires{" "}
                                <code className="font-mono">python3</code> or{" "}
                                <code className="font-mono">jq</code>.
                            </>
                        ) : (
                            <>
                                Paste into PowerShell. Configures{" "}
                                <code className="font-mono">
                                    %USERPROFILE%\.claude\settings.json
                                </code>{" "}
                                with the proxy URL and API key while preserving every unrelated
                                setting. It prompts for the API key with input hidden. Requires
                                PowerShell 5.1 or later.
                            </>
                        )}
                    </p>
                </div>

                <div className="flex justify-end pt-1">
                    <Button variant="primary" onClick={onClose}>
                        Done
                    </Button>
                </div>
            </div>
        </Modal>
    );
}

function SpareCapacityToggle({
    checked,
    onChange,
}: {
    checked: boolean;
    onChange: (checked: boolean) => void;
}) {
    return (
        <label className="border-ink-700 bg-ink-900/50 text-fog-200 flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm">
            <input
                type="checkbox"
                checked={checked}
                onChange={(e) => onChange(e.target.checked)}
                className="accent-brand-500 mt-0.5 h-4 w-4 shrink-0"
            />
            <span>
                Only use spare capacity
                <span className="text-fog-400 block text-xs">
                    Holds back what everyone else, including use outside the proxy, is on pace to
                    use in each 5-hour and weekly window.
                </span>
            </span>
        </label>
    );
}
