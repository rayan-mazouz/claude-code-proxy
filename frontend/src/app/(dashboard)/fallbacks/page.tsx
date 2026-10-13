"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Gauge, KeyRound, RefreshCw, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { AccountStatus, AnthropicFallback, EgressTarget, ProviderHealth } from "@/lib/types";
import { formatDateTime, formatUsd } from "@/lib/format";
import {
    Badge,
    Button,
    Card,
    ConfirmDialog,
    EmptyState,
    ErrorState,
    Field,
    LoadingState,
    Modal,
    Spinner,
    StatusToggle,
    SelectMenu,
    TextInput,
    UsageBar,
} from "@/components/ui";

const DEFAULT_BASE_URL = "https://api.anthropic.com";

function healthBadge(health: ProviderHealth) {
    if (health === "HEALTHY") return <Badge tone="good">Healthy</Badge>;
    if (health === "REAUTH_REQUIRED") return <Badge tone="bad">Key rejected</Badge>;
    if (health === "DEGRADED") return <Badge tone="warn">Temporarily unavailable</Badge>;
    return <Badge tone="neutral">Not tested</Badge>;
}

function statusBadge(status: AccountStatus) {
    if (status === "DISABLED") return <Badge tone="neutral">Disabled</Badge>;
    if (status === "COOLDOWN") return <Badge tone="warn">Cooldown</Badge>;
    return <Badge tone="good">Enabled</Badge>;
}

function spendFraction(provider: AnthropicFallback): number | null {
    if (!provider.monthly_spend_limit_usd) return null;
    return provider.monthly_spend_usd / provider.monthly_spend_limit_usd;
}

function egressTargetLabel(provider: AnthropicFallback, targets: EgressTarget[]): string {
    const target = targets.find((candidate) => candidate.id === provider.egress_target_id);
    if (!target) return "First configured path";
    if (target.interface_name && target.private_ip) {
        return `${target.interface_name} · ${target.private_ip}${target.public_ip ? ` → ${target.public_ip}` : ""}`;
    }
    return target.label;
}

export default function FallbacksPage() {
    const [providers, setProviders] = useState<AnthropicFallback[] | null>(null);
    const [egressTargets, setEgressTargets] = useState<EgressTarget[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [showAdd, setShowAdd] = useState(false);
    const [editTarget, setEditTarget] = useState<AnthropicFallback | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<AnthropicFallback | null>(null);
    const loadRequestId = useRef(0);

    const load = useCallback(() => {
        const requestId = ++loadRequestId.current;
        const isCurrent = () => requestId === loadRequestId.current;
        return Promise.all([api.fallbacks(), api.egressTargets()]).then(
            ([loaded, targets]) => {
                if (!isCurrent()) return;
                setError(null);
                setProviders(loaded);
                setEgressTargets(targets);
                setLoading(false);
            },
            (err: unknown) => {
                if (!isCurrent()) return;
                setError(err instanceof Error ? err.message : "Could not load API fallbacks.");
                setLoading(false);
            },
        );
    }, []);

    const applyProvider = useCallback((updated: AnthropicFallback) => {
        setProviders(
            (current) =>
                current?.map((provider) => (provider.id === updated.id ? updated : provider)) ??
                current,
        );
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const summary = useMemo(() => {
        const values = providers ?? [];
        return {
            enabled: values.filter((provider) => provider.status !== "DISABLED").length,
            healthy: values.filter(
                (provider) =>
                    provider.status === "ACTIVE" && provider.provider_health === "HEALTHY",
            ).length,
            spend: values.reduce((total, provider) => total + provider.monthly_spend_usd, 0),
        };
    }, [providers]);

    const act = async (provider: AnthropicFallback, action: "test" | "toggle") => {
        setBusyId(provider.id);
        setError(null);
        try {
            let updated: AnthropicFallback;
            if (action === "test") {
                updated = await api.testFallback(provider.id);
            } else if (provider.status === "DISABLED") {
                updated = await api.enableFallback(provider.id);
            } else {
                updated = await api.disableFallback(provider.id);
            }
            applyProvider(updated);
            void load();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Fallback update failed.");
        } finally {
            setBusyId(null);
        }
    };

    const remove = async () => {
        if (!deleteTarget) return;
        setBusyId(deleteTarget.id);
        try {
            await api.deleteFallback(deleteTarget.id);
            setProviders(
                (current) =>
                    current?.filter((provider) => provider.id !== deleteTarget.id) ?? current,
            );
            setDeleteTarget(null);
            void load();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not delete fallback.");
        } finally {
            setBusyId(null);
        }
    };

    return (
        <div className="space-y-5">
            <header className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <h1 className="text-fog-100 text-2xl font-semibold tracking-tight">
                        API fallbacks
                    </h1>
                    <p className="text-fog-400 mt-1 max-w-2xl text-sm">
                        Pay-as-you-go Anthropic-compatible providers are used only after every
                        eligible Claude subscription account is unavailable.
                    </p>
                </div>
                <div className="flex gap-2">
                    <Button variant="ghost" onClick={() => void load()} disabled={loading}>
                        <RefreshCw size={15} /> Refresh
                    </Button>
                    <Button variant="primary" onClick={() => setShowAdd(true)}>
                        Add fallback
                    </Button>
                </div>
            </header>

            <Card className="grid grid-cols-1 overflow-hidden sm:grid-cols-3">
                <SummaryCell
                    icon={<ShieldCheck size={17} />}
                    label="Ready"
                    value={`${summary.healthy}`}
                    hint={`${summary.enabled} enabled`}
                />
                <SummaryCell
                    icon={<Gauge size={17} />}
                    label="Month-to-date"
                    value={formatUsd(summary.spend)}
                    hint="Locally metered API spend"
                    divided
                />
                <SummaryCell
                    icon={<KeyRound size={17} />}
                    label="Credentials"
                    value={`${providers?.length ?? 0}`}
                    hint="Encrypted at rest"
                    divided
                />
            </Card>

            <div className="border-ink-700 bg-ink-900/60 text-fog-400 rounded-lg border px-4 py-3 text-xs leading-relaxed">
                Spend caps are enforced from token usage returned by the provider and current local
                model pricing. The request that crosses a cap can slightly exceed it; following
                requests move to the next fallback. Unknown-priced models cannot add an estimated
                charge.
            </div>

            {error ? (
                <ErrorState
                    message={error}
                    onRetry={() => {
                        setLoading(true);
                        setError(null);
                        void load();
                    }}
                />
            ) : null}
            {loading && providers === null ? (
                <Card>
                    <LoadingState label="Loading fallback providers…" />
                </Card>
            ) : providers?.length === 0 ? (
                <Card>
                    <EmptyState message="No API fallback is connected. Subscription accounts remain the only route." />
                </Card>
            ) : (
                <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                    {providers?.map((provider) => {
                        const enabled = provider.status !== "DISABLED";
                        const busy = busyId === provider.id;
                        return (
                            <Card key={provider.id} className="relative overflow-hidden p-5">
                                <div
                                    className={`absolute inset-y-0 left-0 w-1 ${enabled ? "bg-brand-500" : "bg-ink-600"}`}
                                />
                                <div className="flex items-start justify-between gap-4 pl-2">
                                    <div className="min-w-0">
                                        <div className="flex flex-wrap items-center gap-2">
                                            <h2 className="text-fog-100 truncate text-base font-semibold">
                                                {provider.label}
                                            </h2>
                                            {statusBadge(provider.status)}
                                            {healthBadge(provider.provider_health)}
                                            <Badge tone="neutral">
                                                Priority {provider.priority}
                                            </Badge>
                                        </div>
                                        <div className="text-fog-400 mt-2 flex min-w-0 items-center gap-2 font-mono text-xs">
                                            <span className="truncate">{provider.base_url}</span>
                                            <span aria-hidden>·</span>
                                            <span className="shrink-0">{provider.key_hint}</span>
                                        </div>
                                        <div className="text-fog-500 mt-1 truncate text-[11px]">
                                            Egress · {egressTargetLabel(provider, egressTargets)}
                                        </div>
                                    </div>
                                    <StatusToggle
                                        on={enabled}
                                        busy={busy}
                                        onClick={() => void act(provider, "toggle")}
                                        onLabel="On"
                                        offLabel="Off"
                                        srLabel={`${enabled ? "Disable" : "Enable"} ${provider.label}`}
                                    />
                                </div>

                                <div className="border-ink-700 bg-ink-900/55 mt-5 rounded-lg border p-4">
                                    <UsageBar
                                        label="Monthly spend cap"
                                        fraction={spendFraction(provider)}
                                    />
                                    <div className="text-fog-400 mt-2 flex flex-wrap justify-between gap-2 text-xs">
                                        <span>
                                            {formatUsd(provider.monthly_spend_usd)} used
                                            {provider.monthly_spend_limit_usd
                                                ? ` of ${formatUsd(provider.monthly_spend_limit_usd)}`
                                                : " · no cap"}
                                        </span>
                                        <span>
                                            Resets {formatDateTime(provider.monthly_spend_reset_at)}
                                        </span>
                                    </div>
                                </div>

                                <dl className="text-fog-400 mt-4 grid grid-cols-2 gap-x-4 gap-y-3 pl-2 text-xs">
                                    <div>
                                        <dt>Models discovered</dt>
                                        <dd className="text-fog-200 mt-1">
                                            {provider.model_count || "Not tested"}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>Last used</dt>
                                        <dd className="text-fog-200 mt-1">
                                            {formatDateTime(provider.last_used_at)}
                                        </dd>
                                    </div>
                                    {provider.provider_health_message ? (
                                        <div className="col-span-2">
                                            <dt>Provider status</dt>
                                            <dd className="text-warn-500 mt-1 break-words">
                                                {provider.provider_health_message}
                                            </dd>
                                        </div>
                                    ) : null}
                                </dl>

                                <div className="border-ink-700 mt-5 flex flex-wrap gap-2 border-t pt-4 pl-2">
                                    <Button variant="ghost" onClick={() => setEditTarget(provider)}>
                                        Edit
                                    </Button>
                                    <Button
                                        variant="subtle"
                                        onClick={() => void act(provider, "test")}
                                        disabled={busy}
                                    >
                                        {busy ? <Spinner /> : null} Test &amp; refresh models
                                    </Button>
                                    <Button
                                        variant="danger"
                                        onClick={() => setDeleteTarget(provider)}
                                    >
                                        Delete
                                    </Button>
                                </div>
                            </Card>
                        );
                    })}
                </div>
            )}

            {showAdd ? (
                <FallbackModal
                    egressTargets={egressTargets}
                    onClose={() => setShowAdd(false)}
                    onDone={(created) => {
                        setShowAdd(false);
                        setProviders((current) => (current ? [...current, created] : [created]));
                        void load();
                    }}
                />
            ) : null}
            {editTarget ? (
                <FallbackModal
                    provider={editTarget}
                    egressTargets={egressTargets}
                    onClose={() => setEditTarget(null)}
                    onDone={(updated) => {
                        setEditTarget(null);
                        applyProvider(updated);
                        void load();
                    }}
                />
            ) : null}
            {deleteTarget ? (
                <ConfirmDialog
                    title="Delete API fallback"
                    message={`Delete “${deleteTarget.label}”? Its encrypted API key will be removed permanently; historical usage remains detached.`}
                    confirmLabel="Delete"
                    busy={busyId === deleteTarget.id}
                    onConfirm={() => void remove()}
                    onCancel={() => setDeleteTarget(null)}
                />
            ) : null}
        </div>
    );
}

function SummaryCell({
    icon,
    label,
    value,
    hint,
    divided = false,
}: {
    icon: React.ReactNode;
    label: string;
    value: string;
    hint: string;
    divided?: boolean;
}) {
    return (
        <div
            className={`p-5 ${divided ? "border-ink-700 border-t sm:border-t-0 sm:border-l" : ""}`}
        >
            <div className="text-fog-400 flex items-center gap-2 text-xs font-medium tracking-wider uppercase">
                {icon}
                {label}
            </div>
            <div className="text-fog-100 mt-2 text-2xl font-semibold tabular-nums">{value}</div>
            <div className="text-fog-400 mt-1 text-xs">{hint}</div>
        </div>
    );
}

function FallbackModal({
    provider,
    egressTargets,
    onClose,
    onDone,
}: {
    provider?: AnthropicFallback;
    egressTargets: EgressTarget[];
    onClose: () => void;
    onDone: (provider: AnthropicFallback) => void;
}) {
    const [label, setLabel] = useState(provider?.label ?? "");
    const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? DEFAULT_BASE_URL);
    const [apiKey, setApiKey] = useState("");
    const [spendLimit, setSpendLimit] = useState(
        provider?.monthly_spend_limit_usd?.toString() ?? "",
    );
    const [priority, setPriority] = useState(provider?.priority.toString() ?? "1");
    const defaultEgressTargetId = egressTargets.find((target) => target.enabled)?.id ?? "";
    const [egressTargetId, setEgressTargetId] = useState(
        provider?.egress_target_id ?? defaultEgressTargetId,
    );
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const save = async (event: React.FormEvent) => {
        event.preventDefault();
        setSaving(true);
        setError(null);
        const parsedLimit = spendLimit.trim() ? Number(spendLimit) : null;
        const parsedPriority = Number(priority);
        try {
            const saved = provider
                ? await api.updateFallback(provider.id, {
                      label: label.trim(),
                      base_url: baseUrl.trim(),
                      ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
                      ...(parsedLimit === null
                          ? { clear_monthly_spend_limit: true }
                          : { monthly_spend_limit_usd: parsedLimit }),
                      priority: parsedPriority,
                      egress_target_id: egressTargetId || defaultEgressTargetId || null,
                  })
                : await api.createFallback({
                      label: label.trim(),
                      base_url: baseUrl.trim(),
                      api_key: apiKey.trim(),
                      monthly_spend_limit_usd: parsedLimit,
                      priority: parsedPriority,
                      egress_target_id: egressTargetId || defaultEgressTargetId || null,
                  });
            onDone(saved);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not save fallback.");
            setSaving(false);
        }
    };

    return (
        <Modal
            title={provider ? `Edit ${provider.label}` : "Connect API fallback"}
            onClose={onClose}
            widthClass="max-w-xl"
        >
            <form onSubmit={save} className="space-y-4">
                <Field label="Label" hint="A private name shown only in this console.">
                    <TextInput
                        value={label}
                        onChange={(event) => setLabel(event.target.value)}
                        required
                        maxLength={200}
                        placeholder="Production API reserve"
                    />
                </Field>
                <Field
                    label="Base URL"
                    hint="Anthropic-compatible origin. /v1/messages and /v1/models are appended automatically."
                >
                    <TextInput
                        type="url"
                        value={baseUrl}
                        onChange={(event) => setBaseUrl(event.target.value)}
                        required
                        placeholder={DEFAULT_BASE_URL}
                    />
                </Field>
                <Field
                    label={provider ? "Replace API key" : "API key"}
                    hint={
                        provider
                            ? `Leave blank to keep ${provider.key_hint}. Saved secrets can never be viewed again.`
                            : "Encrypted before storage. It can never be viewed again after saving."
                    }
                >
                    <TextInput
                        type="password"
                        autoComplete="new-password"
                        value={apiKey}
                        onChange={(event) => setApiKey(event.target.value)}
                        required={!provider}
                        placeholder={provider ? "Leave blank to keep current key" : "sk-ant-…"}
                    />
                </Field>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field
                        label="Egress network path"
                        hint="Choose the source interface/IP used for this fallback."
                    >
                        <SelectMenu
                            value={egressTargetId}
                            onChange={setEgressTargetId}
                            ariaLabel="Select fallback egress network path"
                            options={egressTargets
                                .filter((target) => target.enabled)
                                .map((target) => ({
                                    value: target.id,
                                    label:
                                        target.interface_name && target.private_ip
                                            ? `${target.interface_name} · ${target.private_ip}${target.public_ip ? ` → ${target.public_ip}` : ""}`
                                            : target.label,
                                }))}
                        />
                    </Field>
                    <Field label="Monthly spend cap (USD)" hint="Blank means no proxy-side cap.">
                        <TextInput
                            type="number"
                            min="0.01"
                            step="0.01"
                            value={spendLimit}
                            onChange={(event) => setSpendLimit(event.target.value)}
                            placeholder="100.00"
                        />
                    </Field>
                    <Field label="Fallback priority" hint="Lower numbers are attempted first.">
                        <TextInput
                            type="number"
                            min="1"
                            step="1"
                            value={priority}
                            onChange={(event) => setPriority(event.target.value)}
                            required
                        />
                    </Field>
                </div>
                {error ? (
                    <div className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-sm">
                        {error}
                    </div>
                ) : null}
                <div className="flex justify-end gap-2 pt-2">
                    <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>
                        Cancel
                    </Button>
                    <Button
                        type="submit"
                        variant="primary"
                        disabled={
                            saving ||
                            !label.trim() ||
                            !baseUrl.trim() ||
                            (!provider && !apiKey.trim())
                        }
                    >
                        {saving ? <Spinner /> : null} Save fallback
                    </Button>
                </div>
            </form>
        </Modal>
    );
}
