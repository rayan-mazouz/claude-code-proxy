"use client";

// ---------------------------------------------------------------------------
// Settings: view and edit the deployment's .env config file. Each key shows
// when a change takes effect (live, after a backend restart, or on the next
// redeploy); secrets are write-only. Security, database and egress changes
// ask for the current owner password.
// ---------------------------------------------------------------------------

import { useEffect, useMemo, useState } from "react";
import {
    Eraser,
    FileLock2,
    KeyRound,
    Lock,
    Network,
    RefreshCw,
    RotateCcw,
    Rocket,
    Save,
    Search,
    TriangleAlert,
} from "lucide-react";
import {
    Badge,
    Button,
    Card,
    ConfirmDialog,
    ErrorState,
    LoadingState,
    SelectMenu,
    Spinner,
    StatusToggle,
    TextInput,
} from "@/components/ui";
import { useResource } from "@/lib/useResource";
import { ApiError } from "@/lib/api";
import {
    SettingApply,
    SettingEntry,
    SettingsState,
    SettingsValidationError,
    settingsApi,
} from "@/lib/settingsApi";

const APPLY_BADGE: Record<SettingApply, { label: string; tone: "good" | "warn" | "brand" }> = {
    live: { label: "Live", tone: "good" },
    restart: { label: "Restart", tone: "warn" },
    redeploy: { label: "Redeploy", tone: "brand" },
};

const APPLY_HINT: Record<SettingApply, string> = {
    live: "Takes effect within a few seconds.",
    restart: "Takes effect after the backend restarts; sidecars restart on their own.",
    redeploy: "Read by Docker Compose; applies on the next update or docker compose up.",
};

const SOURCE_LABEL: Record<SettingEntry["source"], string> = {
    file: "from .env",
    environment: "from environment",
    default: "built-in default",
};

const LEAVE_PROMPT = "You have unsaved settings. Leave this page and discard them?";

const TEXTAREA_CLASS =
    "border-ink-600 bg-ink-900 text-fog-100 placeholder:text-fog-400 focus:border-brand-400 focus:ring-brand-500/25 w-full resize-y rounded-md border px-3 py-2.5 font-mono text-xs leading-relaxed outline-none focus:ring-2";

function jsonError(value: string): string | null {
    if (!value.trim()) return null;
    try {
        JSON.parse(value);
        return null;
    } catch (error) {
        return error instanceof Error ? `Invalid JSON: ${error.message}` : "Invalid JSON";
    }
}

function prettyJson(value: string | null): string {
    if (!value?.trim()) return value ?? "";
    try {
        return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
        return value;
    }
}

function numberError(entry: SettingEntry, value: string): string | null {
    if (entry.type !== "int" && entry.type !== "float") return null;
    const text = value.trim();
    if (!text) return "Enter a number.";
    if (entry.type === "int" ? !/^[+-]?\d+$/.test(text) : !Number.isFinite(Number(text))) {
        return entry.type === "int" ? "Enter a whole number." : "Enter a number.";
    }
    const number = Number(text);
    if (entry.minimum !== null && number < entry.minimum) return `At least ${entry.minimum}.`;
    if (entry.maximum !== null && number > entry.maximum) return `At most ${entry.maximum}.`;
    return null;
}

function Banner({
    tone,
    icon,
    title,
    children,
    action,
}: {
    tone: "warn" | "brand" | "bad";
    icon: React.ReactNode;
    title: string;
    children: React.ReactNode;
    action?: React.ReactNode;
}) {
    const styles = {
        warn: "border-warn-500/30 bg-warn-500/10",
        brand: "border-brand-400/30 bg-brand-500/10",
        bad: "border-bad-500/30 bg-bad-500/10",
    }[tone];
    const accent = { warn: "text-warn-500", brand: "text-brand-300", bad: "text-bad-500" }[tone];
    return (
        <div
            role="status"
            className={`flex flex-col gap-3 rounded-lg border px-4 py-3 sm:flex-row sm:items-start sm:justify-between ${styles}`}
        >
            <div className="flex min-w-0 gap-3">
                <span className={`mt-0.5 shrink-0 ${accent}`}>{icon}</span>
                <div className="min-w-0">
                    <div className={`text-sm font-semibold ${accent}`}>{title}</div>
                    <div className="text-fog-300 mt-1 text-xs leading-relaxed">{children}</div>
                </div>
            </div>
            {action ? <div className="shrink-0">{action}</div> : null}
        </div>
    );
}

function KeyList({ keys }: { keys: string[] }) {
    return (
        <span className="mt-1.5 flex flex-wrap gap-1">
            {keys.map((key) => (
                <code
                    key={key}
                    className="border-ink-600 bg-ink-900 text-fog-200 rounded border px-1.5 py-0.5 font-mono text-[11px]"
                >
                    {key}
                </code>
            ))}
        </span>
    );
}

function SettingControl({
    entry,
    value,
    disabled,
    cleared,
    onChange,
    onClear,
}: {
    entry: SettingEntry;
    value: string;
    disabled: boolean;
    cleared: boolean;
    onChange: (value: string) => void;
    onClear: () => void;
}) {
    const id = `setting-${entry.key}`;

    if (entry.secret) {
        const canClear = entry.clearable && entry.is_set && !entry.read_only && !cleared;
        return (
            <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                    {cleared ? (
                        <Badge tone="warn">
                            <Eraser size={11} /> Will be cleared
                        </Badge>
                    ) : (
                        <Badge tone={entry.is_set ? "good" : "neutral"}>
                            <KeyRound size={11} /> {entry.is_set ? "Set" : "Not set"}
                        </Badge>
                    )}
                    {entry.read_only ? (
                        <span className="text-fog-400 inline-flex items-center gap-1 text-xs">
                            <Lock size={12} /> Edit .env by hand to change it
                        </span>
                    ) : null}
                    {canClear ? (
                        <button
                            type="button"
                            onClick={onClear}
                            disabled={disabled}
                            className="text-fog-400 hover:text-fog-100 inline-flex items-center gap-1 text-xs disabled:opacity-50"
                        >
                            <Eraser size={12} /> Clear
                        </button>
                    ) : null}
                </div>
                {entry.read_only ? null : (
                    <TextInput
                        id={id}
                        type="password"
                        autoComplete="new-password"
                        placeholder={
                            cleared
                                ? "Saving removes the value"
                                : entry.is_set
                                  ? "Enter a new value to replace it"
                                  : "Enter a value"
                        }
                        value={value}
                        disabled={disabled}
                        onChange={(event) => onChange(event.target.value)}
                    />
                )}
            </div>
        );
    }

    if (entry.type === "bool") {
        return (
            <StatusToggle
                on={value === "true"}
                busy={disabled || entry.read_only}
                onClick={() => onChange(value === "true" ? "false" : "true")}
                onLabel="Enabled"
                offLabel="Disabled"
                srLabel={entry.label}
            />
        );
    }

    if (entry.type === "choice") {
        const options = entry.choices.map((choice) => ({ value: choice, label: choice }));
        if (value && !entry.choices.includes(value)) {
            options.unshift({ value, label: `${value} (unsupported)` });
        }
        return (
            <SelectMenu
                value={value}
                options={options}
                onChange={onChange}
                ariaLabel={entry.label}
                className={disabled ? "pointer-events-none opacity-50" : ""}
            />
        );
    }

    if (entry.type === "list" && entry.choices.length) {
        const selected = new Set(
            value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean),
        );
        const toggle = (choice: string) => {
            const next = new Set(selected);
            if (next.has(choice)) next.delete(choice);
            else next.add(choice);
            onChange(entry.choices.filter((item) => next.has(item)).join(","));
        };
        return (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={entry.label}>
                {entry.choices.map((choice) => {
                    const active = selected.has(choice);
                    return (
                        <button
                            key={choice}
                            type="button"
                            aria-pressed={active}
                            disabled={disabled}
                            onClick={() => toggle(choice)}
                            className={`rounded-md border px-2.5 py-1 font-mono text-xs transition-colors disabled:opacity-50 ${
                                active
                                    ? "border-brand-400/40 bg-brand-500/15 text-brand-300"
                                    : "border-ink-600 bg-ink-900 text-fog-400 hover:text-fog-200"
                            }`}
                        >
                            {choice}
                        </button>
                    );
                })}
            </div>
        );
    }

    if (entry.type === "json") {
        return (
            <textarea
                id={id}
                value={value}
                disabled={disabled}
                onChange={(event) => onChange(event.target.value)}
                rows={Math.max(3, Math.min(14, value.split("\n").length + 1))}
                className={TEXTAREA_CLASS}
                spellCheck={false}
                placeholder="Blank to leave unset"
            />
        );
    }

    if (entry.type === "list") {
        return (
            <textarea
                id={id}
                value={value}
                disabled={disabled}
                onChange={(event) => onChange(event.target.value.replace(/\n/g, ","))}
                rows={Math.max(2, Math.min(5, Math.ceil(value.length / 60)))}
                className={TEXTAREA_CLASS}
                spellCheck={false}
                placeholder="Comma-separated"
            />
        );
    }

    return (
        <TextInput
            id={id}
            value={value}
            disabled={disabled}
            inputMode={
                entry.type === "int" ? "numeric" : entry.type === "float" ? "decimal" : undefined
            }
            className={entry.type === "string" ? "" : "font-mono"}
            onChange={(event) => onChange(event.target.value)}
        />
    );
}

function SettingRow({
    entry,
    value,
    dirty,
    error,
    pending,
    pendingRelay,
    disabled,
    onChange,
    onClear,
    onReset,
}: {
    entry: SettingEntry;
    value: string;
    dirty: boolean;
    error: string | null;
    pending: boolean;
    pendingRelay: boolean;
    disabled: boolean;
    onChange: (value: string) => void;
    onClear: () => void;
    onReset: () => void;
}) {
    const badge = APPLY_BADGE[entry.apply];
    return (
        <div
            className={`grid gap-3 px-5 py-4 md:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] md:gap-6 ${dirty ? "bg-brand-500/[0.04]" : ""}`}
        >
            <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                    <label
                        htmlFor={`setting-${entry.key}`}
                        className="text-fog-100 text-sm font-medium"
                    >
                        {entry.label}
                    </label>
                    <span title={APPLY_HINT[entry.apply]}>
                        <Badge tone={badge.tone}>{badge.label}</Badge>
                    </span>
                    {dirty ? <Badge tone="brand">Unsaved</Badge> : null}
                    {pending ? <Badge tone="warn">Awaiting {entry.apply}</Badge> : null}
                    {pendingRelay ? <Badge tone="warn">Awaiting relay restart</Badge> : null}
                    {entry.requires_password && !entry.read_only ? (
                        <span title="Changing this asks for the current owner password.">
                            <Badge tone="neutral">
                                <Lock size={10} /> Password
                            </Badge>
                        </span>
                    ) : null}
                </div>
                <div className="text-fog-400 mt-1 flex flex-wrap items-center gap-x-2 font-mono text-[11px]">
                    <span>{entry.key}</span>
                    <span aria-hidden="true">·</span>
                    <span className="font-sans">{SOURCE_LABEL[entry.source]}</span>
                </div>
                <p className="text-fog-400 mt-1.5 text-xs leading-relaxed">{entry.help}</p>
            </div>
            <div className="min-w-0">
                <SettingControl
                    entry={entry}
                    value={value}
                    disabled={disabled}
                    cleared={entry.secret && dirty && value === ""}
                    onChange={onChange}
                    onClear={onClear}
                />
                <div className="mt-1.5 flex items-start justify-between gap-3">
                    {error ? (
                        <span role="alert" className="text-bad-500 text-xs">
                            {error}
                        </span>
                    ) : (
                        <span />
                    )}
                    {dirty ? (
                        <button
                            type="button"
                            onClick={onReset}
                            className="text-fog-400 hover:text-fog-100 inline-flex shrink-0 items-center gap-1 text-xs"
                        >
                            <RotateCcw size={12} /> Revert
                        </button>
                    ) : null}
                </div>
            </div>
        </div>
    );
}

export default function SettingsPage() {
    const resource = useResource(settingsApi.get, "settings");
    // A state fetched after a save or restart replaces the loaded one until the next reload.
    const [saved, setSaved] = useState<SettingsState | null>(null);
    const state = saved ?? resource.data;

    const [draft, setDraft] = useState<Record<string, string>>({});
    const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
    const [query, setQuery] = useState("");
    const [saving, setSaving] = useState(false);
    const [restarting, setRestarting] = useState(false);
    const [confirmRestart, setConfirmRestart] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const [password, setPassword] = useState("");

    const byKey = useMemo(
        () => new Map((state?.settings ?? []).map((entry) => [entry.key, entry])),
        [state],
    );

    const original = (entry: SettingEntry) =>
        entry.secret ? "" : entry.type === "json" ? prettyJson(entry.value) : (entry.value ?? "");

    const dirtyKeys = Object.keys(draft).filter((key) => byKey.has(key));
    const dirty = dirtyKeys.length > 0;
    const passwordKeys = dirtyKeys.filter((key) => byKey.get(key)?.requires_password);
    const needsPassword = passwordKeys.length > 0;

    const clientErrors = useMemo(() => {
        const errors: Record<string, string> = {};
        for (const [key, value] of Object.entries(draft)) {
            const entry = byKey.get(key);
            if (!entry) continue;
            const message =
                entry.type === "json"
                    ? jsonError(value)
                    : entry.type === "int" || entry.type === "float"
                      ? numberError(entry, value)
                      : /[\r\n]/.test(value)
                        ? "Line breaks are not allowed."
                        : null;
            if (message) errors[key] = message;
        }
        return errors;
    }, [draft, byKey]);
    const hasClientErrors = Object.keys(clientErrors).length > 0;

    // Warn before leaving the page with unsaved edits: beforeunload covers reloads
    // and full navigations; a capturing click listener covers in-app links, which
    // the Next.js router follows without unloading the page.
    useEffect(() => {
        if (!dirty) return;
        const onBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault();
        const onClick = (event: MouseEvent) => {
            if (event.defaultPrevented || event.button !== 0) return;
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
            if (!(anchor instanceof HTMLAnchorElement) || anchor.hasAttribute("download")) return;
            if (anchor.target && anchor.target !== "_self") return;
            const url = new URL(anchor.href, window.location.href);
            // Other origins unload the page, so beforeunload already asks.
            if (url.origin !== window.location.origin) return;
            const here = window.location;
            if (url.pathname === here.pathname && url.search === here.search) return;
            if (!window.confirm(LEAVE_PROMPT)) {
                event.preventDefault();
                event.stopPropagation();
            }
        };
        window.addEventListener("beforeunload", onBeforeUnload);
        document.addEventListener("click", onClick, true);
        return () => {
            window.removeEventListener("beforeunload", onBeforeUnload);
            document.removeEventListener("click", onClick, true);
        };
    }, [dirty]);

    const setValue = (entry: SettingEntry, value: string) => {
        setNotice(null);
        setServerErrors((current) => {
            if (!(entry.key in current)) return current;
            const next = { ...current };
            delete next[entry.key];
            return next;
        });
        setDraft((current) => {
            const next = { ...current };
            if (value === original(entry)) delete next[entry.key];
            else next[entry.key] = value;
            return next;
        });
    };

    // Optional secrets are cleared by saving an empty value, which the draft keeps
    // even though it equals the (always empty) displayed value.
    const clearSecret = (entry: SettingEntry) => {
        setNotice(null);
        setDraft((current) => ({ ...current, [entry.key]: "" }));
    };

    const revert = (key: string) =>
        setDraft((current) => {
            const next = { ...current };
            delete next[key];
            return next;
        });

    const reload = () => {
        setSaved(null);
        setNotice(null);
        resource.reload();
    };

    // Re-read the state so the restart/redeploy/relay banners are current.
    const refresh = async (fallback?: SettingsState) => {
        try {
            setSaved(await settingsApi.get());
        } catch {
            if (fallback) setSaved(fallback);
        }
    };

    const onSave = async () => {
        if (!dirty || hasClientErrors || (needsPassword && !password)) return;
        setSaving(true);
        setError(null);
        setNotice(null);
        try {
            const values: Record<string, string> = {};
            for (const key of dirtyKeys) values[key] = draft[key];
            const next = await settingsApi.save(values, needsPassword ? password : undefined);
            setDraft({});
            setServerErrors({});
            setPassword("");
            const count = dirtyKeys.length;
            setNotice(`Saved ${count} setting${count === 1 ? "" : "s"} to ${next.path}.`);
            await refresh(next);
        } catch (err) {
            if (err instanceof SettingsValidationError) {
                setServerErrors(err.errors);
                setError(err.errors._form ?? err.message);
            } else {
                if (err instanceof ApiError && err.status === 403) setPassword("");
                setError(err instanceof Error ? err.message : "Could not save settings.");
            }
        } finally {
            setSaving(false);
        }
    };

    const onDiscard = () => {
        setDraft({});
        setServerErrors({});
        setError(null);
        setPassword("");
    };

    const onRestart = async () => {
        setRestarting(true);
        setError(null);
        try {
            await settingsApi.restartBackend();
            setConfirmRestart(false);
            setNotice(
                "Restart requested. Fresh workers are starting while the old ones finish their requests.",
            );
            // Give gunicorn a moment to replace the workers before re-reading the state.
            window.setTimeout(() => void refresh(), 4000);
        } catch (err) {
            setConfirmRestart(false);
            setError(err instanceof Error ? err.message : "Could not restart the backend.");
        } finally {
            setRestarting(false);
        }
    };

    if (!state) {
        if (resource.error) return <ErrorState message={resource.error} onRetry={reload} />;
        return <LoadingState label="Loading settings…" />;
    }

    const needle = query.trim().toLowerCase();
    const visible = state.settings.filter(
        (entry) =>
            !needle ||
            entry.key.toLowerCase().includes(needle) ||
            entry.label.toLowerCase().includes(needle) ||
            entry.help.toLowerCase().includes(needle),
    );
    const groups = state.groups
        .map((group) => ({ group, entries: visible.filter((entry) => entry.group === group) }))
        .filter(({ entries }) => entries.length > 0);
    const pendingRestart = new Set(state.pending_restart);
    const pendingRedeploy = new Set(state.pending_redeploy);
    const pendingRelay = new Set(state.pending_relay);
    const editable = state.writable && !saving;

    return (
        <div className="space-y-6 pb-24">
            <header className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <h1 className="text-fog-100 text-2xl font-semibold tracking-tight">Settings</h1>
                    <p className="text-fog-400 mt-1 max-w-2xl text-sm">
                        Everything in the deployment&apos;s config file,{" "}
                        <code className="text-fog-300 font-mono text-xs">{state.path}</code>. Saving
                        rewrites the file in place and keeps its comments and order.
                    </p>
                    <p className="text-fog-400 mt-1 max-w-2xl text-xs leading-relaxed">
                        Outside this page, edit the file in place only (
                        <code className="font-mono">server-install.sh</code> or an editor that
                        overwrites it): tools that replace the file, such as{" "}
                        <code className="font-mono">sed -i</code>, break the containers&apos;
                        single-file bind mount until the next update, and the file must keep the
                        group and mode the installer set.
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <div className="relative">
                        <Search
                            size={14}
                            className="text-fog-400 pointer-events-none absolute top-1/2 left-3 -translate-y-1/2"
                        />
                        <TextInput
                            aria-label="Filter settings"
                            placeholder="Filter settings"
                            value={query}
                            onChange={(event) => setQuery(event.target.value)}
                            className="w-56 pl-8"
                        />
                    </div>
                    <Button variant="ghost" onClick={reload} disabled={resource.loading}>
                        <RefreshCw size={15} className={resource.loading ? "animate-spin" : ""} />{" "}
                        Refresh
                    </Button>
                </div>
            </header>

            <div className="text-fog-400 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
                {(Object.keys(APPLY_BADGE) as SettingApply[]).map((apply) => (
                    <span key={apply} className="inline-flex items-center gap-1.5">
                        <Badge tone={APPLY_BADGE[apply].tone}>{APPLY_BADGE[apply].label}</Badge>
                        {APPLY_HINT[apply]}
                    </span>
                ))}
            </div>

            {!state.writable ? (
                <Banner
                    tone="bad"
                    icon={<FileLock2 size={17} />}
                    title={
                        !state.exists
                            ? "The config file does not exist"
                            : state.readable
                              ? "The config file is read-only for the backend"
                              : "The config file cannot be read by the backend"
                    }
                >
                    {state.exists && !state.readable ? (
                        <>The values below come from the container environment, not the file. </>
                    ) : null}
                    Settings are shown but cannot be saved. The backend runs as{" "}
                    <code className="font-mono">appuser</code> (uid/gid 10001) and needs read and
                    write access to <code className="font-mono">{state.path}</code>. On the
                    single-server install, run{" "}
                    <code className="font-mono">sudo scripts/server-install.sh update</code> to fix
                    the permissions. Otherwise give the host file to that group, e.g.{" "}
                    <code className="font-mono">sudo chgrp 10001 .env && sudo chmod 0660 .env</code>{" "}
                    (under rootless Docker, use the host subgid that container gid 10001 maps to:
                    the start of the user&apos;s <code className="font-mono">/etc/subgid</code>{" "}
                    range + 10000).
                </Banner>
            ) : null}

            {state.pending_restart.length ? (
                <Banner
                    tone="warn"
                    icon={<TriangleAlert size={17} />}
                    title="Saved changes are waiting for a backend restart"
                    action={
                        <Button
                            variant="primary"
                            onClick={() => setConfirmRestart(true)}
                            disabled={!state.restart_supported || restarting}
                            title={
                                state.restart_supported
                                    ? undefined
                                    : "The backend is not running under gunicorn; restart it manually."
                            }
                        >
                            {restarting ? <Spinner /> : <RotateCcw size={15} />} Restart backend
                        </Button>
                    }
                >
                    The backend still uses the values it started with for these keys. A restart
                    replaces its workers one by one without dropping traffic; the sidecars restart
                    themselves within a few seconds.
                    {state.restart_supported ? null : (
                        <> This backend is not running under gunicorn, so restart it manually.</>
                    )}
                    <KeyList keys={state.pending_restart} />
                </Banner>
            ) : null}

            {state.pending_redeploy.length ? (
                <Banner
                    tone="brand"
                    icon={<Rocket size={17} />}
                    title="Some changes apply on the next redeploy"
                >
                    Docker Compose reads these keys when it creates the containers (ports, routing,
                    mounts). They take effect on the next{" "}
                    <code className="font-mono">sudo scripts/server-install.sh update</code> or{" "}
                    <code className="font-mono">docker compose up -d</code>.
                    <KeyList keys={state.pending_redeploy} />
                </Banner>
            ) : null}

            {state.pending_relay.length ? (
                <Banner
                    tone="brand"
                    icon={<Network size={17} />}
                    title="Also restart the egress relay"
                >
                    The egress relay is a separate Compose project and reads these keys only when it
                    starts. In the deployment directory, run{" "}
                    <code className="font-mono">{state.relay_restart_command}</code>.
                    <KeyList keys={state.pending_relay} />
                </Banner>
            ) : null}

            {error ? (
                <div
                    role="alert"
                    className="border-bad-500/30 bg-bad-500/10 text-bad-500 rounded-md border px-3 py-2 text-sm"
                >
                    {error}
                </div>
            ) : null}
            {notice ? (
                <div
                    role="status"
                    className="border-good-500/30 bg-good-500/10 text-good-500 rounded-md border px-3 py-2 text-sm"
                >
                    {notice}
                </div>
            ) : null}

            {groups.length === 0 ? (
                <Card className="text-fog-400 px-5 py-10 text-center text-sm">
                    No settings match &ldquo;{query}&rdquo;.
                </Card>
            ) : null}

            {groups.map(({ group, entries }) => (
                <section key={group} className="space-y-2">
                    <h2 className="text-fog-200 px-1 text-xs font-semibold tracking-[0.16em] uppercase">
                        {group}
                    </h2>
                    <Card className="divide-ink-700 divide-y overflow-hidden">
                        {entries.map((entry) => {
                            const isDirty = entry.key in draft;
                            const value = isDirty ? draft[entry.key] : original(entry);
                            return (
                                <SettingRow
                                    key={entry.key}
                                    entry={entry}
                                    value={value}
                                    dirty={isDirty}
                                    error={
                                        clientErrors[entry.key] ?? serverErrors[entry.key] ?? null
                                    }
                                    pending={
                                        pendingRestart.has(entry.key) ||
                                        pendingRedeploy.has(entry.key)
                                    }
                                    pendingRelay={pendingRelay.has(entry.key)}
                                    disabled={!editable || entry.read_only}
                                    onChange={(next) => setValue(entry, next)}
                                    onClear={() => clearSecret(entry)}
                                    onReset={() => revert(entry.key)}
                                />
                            );
                        })}
                    </Card>
                </section>
            ))}

            {dirty ? (
                <div className="fixed inset-x-0 bottom-0 z-40 px-4 pb-4 md:left-60">
                    <div className="border-ink-600 bg-ink-850/95 mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3 shadow-[var(--shadow-pop)] backdrop-blur">
                        <div className="text-fog-200 text-sm">
                            <span className="font-semibold">{dirtyKeys.length}</span> unsaved change
                            {dirtyKeys.length === 1 ? "" : "s"}
                            {hasClientErrors ? (
                                <span className="text-bad-500 ml-2 text-xs">
                                    Fix the highlighted fields to save.
                                </span>
                            ) : needsPassword ? (
                                <span className="text-fog-400 ml-2 text-xs">
                                    Changing {passwordKeys.join(", ")} needs the current owner
                                    password.
                                </span>
                            ) : null}
                        </div>
                        <form
                            className="flex flex-wrap items-center gap-2"
                            onSubmit={(event) => {
                                event.preventDefault();
                                void onSave();
                            }}
                        >
                            {needsPassword ? (
                                <TextInput
                                    type="password"
                                    aria-label="Current owner password"
                                    autoComplete="current-password"
                                    placeholder="Current owner password"
                                    value={password}
                                    disabled={saving}
                                    onChange={(event) => setPassword(event.target.value)}
                                    className="w-56"
                                />
                            ) : null}
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={onDiscard}
                                disabled={saving}
                            >
                                Discard
                            </Button>
                            <Button
                                type="submit"
                                variant="primary"
                                disabled={
                                    saving ||
                                    hasClientErrors ||
                                    !state.writable ||
                                    (needsPassword && !password)
                                }
                            >
                                {saving ? <Spinner /> : <Save size={15} />} Save
                            </Button>
                        </form>
                    </div>
                </div>
            ) : null}

            {confirmRestart ? (
                <ConfirmDialog
                    title="Restart the backend?"
                    message="Gunicorn starts fresh workers with the saved settings and lets the current ones finish their requests (up to its graceful timeout). Very long streaming requests may be cut off."
                    confirmLabel="Restart backend"
                    busy={restarting}
                    onConfirm={() => void onRestart()}
                    onCancel={() => setConfirmRestart(false)}
                />
            ) : null}
        </div>
    );
}
