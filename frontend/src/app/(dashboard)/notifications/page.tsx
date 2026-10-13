"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
    Bot,
    Check,
    ChevronDown,
    RotateCcw,
    Search,
    Send,
    ShieldCheck,
    Waypoints,
} from "lucide-react";
import { api } from "@/lib/api";
import { NotificationRule, NotificationSettings } from "@/lib/types";
import { formatDateTime } from "@/lib/format";
import { Button, Card, ErrorState, Field, LoadingState, Spinner, TextInput } from "@/components/ui";

const ACCOUNT_EVENTS = new Set([
    "account_added",
    "account_authentication_expired",
    "account_quota_threshold",
    "account_hard_limit",
    "pool_unavailable",
]);
const SYSTEM_EVENTS = new Set(["notifier_installed"]);
const HEALTH_EVENTS = new Set(["elevated_503"]);
const REPORT_EVENTS = new Set([
    "daily_usage_report",
    "weekly_usage_report",
    "monthly_usage_report",
]);

const TIMEZONE_OPTIONS = [
    ["UTC", "Coordinated Universal Time"],
    ["America/Los_Angeles", "Pacific Time (US & Canada)"],
    ["America/Denver", "Mountain Time (US & Canada)"],
    ["America/Chicago", "Central Time (US & Canada)"],
    ["America/New_York", "Eastern Time (US & Canada)"],
    ["America/Toronto", "Eastern Canada"],
    ["America/Mexico_City", "Central Mexico"],
    ["America/Sao_Paulo", "Brasilia Time"],
    ["Europe/London", "United Kingdom"],
    ["Europe/Berlin", "Central Europe"],
    ["Europe/Paris", "France"],
    ["Europe/Moscow", "Moscow"],
    ["Africa/Cairo", "Egypt"],
    ["Africa/Johannesburg", "South Africa"],
    ["Asia/Dubai", "Gulf Standard Time"],
    ["Asia/Riyadh", "Arabia Standard Time"],
    ["Asia/Kolkata", "India Standard Time"],
    ["Asia/Dhaka", "Bangladesh"],
    ["Asia/Bangkok", "Indochina Time"],
    ["Asia/Jakarta", "Western Indonesia"],
    ["Asia/Singapore", "Singapore"],
    ["Asia/Shanghai", "China Standard Time"],
    ["Asia/Tokyo", "Japan Standard Time"],
    ["Asia/Seoul", "Korea Standard Time"],
    ["Australia/Perth", "Western Australia"],
    ["Australia/Sydney", "Eastern Australia"],
    ["Pacific/Auckland", "New Zealand"],
] as const;

function TextToggle({
    on,
    onClick,
    onLabel = "On",
    offLabel = "Off",
    srLabel,
}: {
    on: boolean;
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
            onClick={onClick}
            className="group inline-flex items-center gap-2 disabled:cursor-not-allowed disabled:opacity-60"
        >
            <span
                aria-hidden="true"
                className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors ${on ? "border-brand-400/60 bg-brand-500/20" : "border-ink-500 bg-ink-800 group-hover:border-ink-400"}`}
            >
                <span
                    className={`absolute h-3.5 w-3.5 rounded-full shadow-sm transition-transform ${on ? "bg-brand-300 translate-x-[1.1rem]" : "bg-fog-300 translate-x-0.5"}`}
                />
            </span>
            <span
                className={`text-xs font-medium ${on ? "text-brand-300" : "text-fog-400 group-hover:text-fog-200"}`}
            >
                {on ? onLabel : offLabel}
            </span>
        </button>
    );
}

function TimezoneSelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState("");
    const containerRef = useRef<HTMLDivElement>(null);
    const selected = TIMEZONE_OPTIONS.find(([timezone]) => timezone === value);
    const filtered = TIMEZONE_OPTIONS.filter(([timezone, label]) =>
        `${timezone} ${label}`.toLowerCase().includes(query.trim().toLowerCase()),
    );

    useEffect(() => {
        if (!open) return;
        const closeOnOutsideClick = (event: MouseEvent) => {
            if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
        };
        const closeOnEscape = (event: KeyboardEvent) => {
            if (event.key === "Escape") setOpen(false);
        };
        document.addEventListener("mousedown", closeOnOutsideClick);
        document.addEventListener("keydown", closeOnEscape);
        return () => {
            document.removeEventListener("mousedown", closeOnOutsideClick);
            document.removeEventListener("keydown", closeOnEscape);
        };
    }, [open]);

    return (
        <div ref={containerRef} className="relative">
            <button
                type="button"
                role="combobox"
                aria-expanded={open}
                aria-controls="notification-timezone-options"
                onClick={() => setOpen((current) => !current)}
                className="border-ink-600 bg-ink-900 text-fog-100 focus:border-brand-400 focus:ring-brand-500/25 flex w-full items-center justify-between rounded-md border px-3 py-2.5 text-sm transition outline-none focus:ring-2"
            >
                <span className="min-w-0 truncate text-left">
                    <span className="font-mono">{value}</span>
                    {selected ? <span className="text-fog-400 ml-2">· {selected[1]}</span> : null}
                </span>
                <ChevronDown
                    size={16}
                    className={`text-fog-400 ml-3 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
                />
            </button>
            {open ? (
                <div
                    id="notification-timezone-options"
                    role="listbox"
                    className="border-ink-600 bg-ink-850 absolute z-20 mt-2 w-full overflow-hidden rounded-lg border shadow-[var(--shadow-pop)]"
                >
                    <div className="border-ink-700 border-b p-2">
                        <div className="relative">
                            <Search
                                size={14}
                                className="text-fog-400 pointer-events-none absolute top-1/2 left-3 -translate-y-1/2"
                            />
                            <input
                                value={query}
                                onChange={(event) => setQuery(event.target.value)}
                                placeholder="Search timezones…"
                                autoFocus
                                className="border-ink-600 bg-ink-900 text-fog-100 placeholder:text-fog-500 focus:border-brand-400 w-full rounded-md border py-2 pr-3 pl-9 text-xs outline-none"
                            />
                        </div>
                    </div>
                    <div className="max-h-64 overflow-y-auto p-1">
                        {filtered.length ? (
                            filtered.map(([timezone, label]) => (
                                <button
                                    key={timezone}
                                    type="button"
                                    role="option"
                                    aria-selected={timezone === value}
                                    onClick={() => {
                                        onChange(timezone);
                                        setQuery("");
                                        setOpen(false);
                                    }}
                                    className="hover:bg-ink-700 flex w-full items-center justify-between rounded px-3 py-2 text-left transition-colors"
                                >
                                    <span>
                                        <span className="text-fog-100 block font-mono text-xs">
                                            {timezone}
                                        </span>
                                        <span className="text-fog-400 mt-0.5 block text-[11px]">
                                            {label}
                                        </span>
                                    </span>
                                    {timezone === value ? (
                                        <Check size={14} className="text-brand-300 shrink-0" />
                                    ) : null}
                                </button>
                            ))
                        ) : (
                            <p className="text-fog-400 px-3 py-4 text-center text-xs">
                                No matching timezones.
                            </p>
                        )}
                    </div>
                </div>
            ) : null}
        </div>
    );
}

function RuleGroup({
    title,
    description,
    rules,
    onSaved,
}: {
    title: string;
    description: string;
    rules: NotificationRule[];
    onSaved: (settings: NotificationSettings) => void;
}) {
    const [open, setOpen] = useState(true);
    return (
        <section className="space-y-3">
            <button
                type="button"
                onClick={() => setOpen((current) => !current)}
                aria-expanded={open}
                className="group flex w-full items-start justify-between gap-4 px-1 text-left"
            >
                <span>
                    <span className="text-fog-200 block text-xs font-semibold tracking-[0.16em] uppercase">
                        {title}
                    </span>
                    <span className="text-fog-400 mt-1 block text-xs">{description}</span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                    <span className="text-fog-400 text-[10px] font-medium tracking-[0.14em] uppercase">
                        {rules.filter((rule) => rule.enabled).length} active
                    </span>
                    <ChevronDown
                        size={16}
                        className={`text-fog-400 transition-transform ${open ? "rotate-180" : ""}`}
                    />
                </span>
            </button>
            {open
                ? rules.map((rule) => (
                      <RuleEditor key={rule.event_type} rule={rule} onSaved={onSaved} />
                  ))
                : null}
        </section>
    );
}

function RuleEditor({
    rule,
    onSaved,
}: {
    rule: NotificationRule;
    onSaved: (settings: NotificationSettings) => void;
}) {
    const [enabled, setEnabled] = useState(rule.enabled);
    const [template, setTemplate] = useState(rule.template);
    const [cooldown, setCooldown] = useState(rule.cooldown_seconds);
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Follow the saved rule when the parent passes a new one (after a save or reload),
    // adjusting state during render rather than in an effect.
    const [syncedRule, setSyncedRule] = useState(rule);
    if (rule !== syncedRule) {
        setSyncedRule(rule);
        setEnabled(rule.enabled);
        setTemplate(rule.template);
        setCooldown(rule.cooldown_seconds);
    }

    const dirty =
        enabled !== rule.enabled ||
        template !== rule.template ||
        cooldown !== rule.cooldown_seconds;

    const save = async () => {
        setSaving(true);
        setError(null);
        try {
            const settings = await api.updateNotificationRule(rule.event_type, {
                enabled,
                template,
                cooldown_seconds: cooldown,
            });
            onSaved(settings);
            setSaved(true);
            window.setTimeout(() => setSaved(false), 1600);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not save this event.");
        } finally {
            setSaving(false);
        }
    };

    const insertVariable = (variable: string) => {
        const suffix = template.endsWith(" ") || template.endsWith("\n") || !template ? "" : " ";
        setTemplate(`${template}${suffix}{{ ${variable} }}`);
    };

    return (
        <Card className="group overflow-hidden">
            <div className="border-ink-700 flex flex-col gap-4 border-b px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                    <h3 className="text-fog-100 text-sm font-semibold">{rule.title}</h3>
                    <p className="text-fog-400 mt-1.5 max-w-2xl text-xs leading-relaxed">
                        {rule.description}
                    </p>
                </div>
                <TextToggle
                    on={enabled}
                    onClick={() => setEnabled((value) => !value)}
                    onLabel="Sending"
                    offLabel="Muted"
                    srLabel={`Toggle ${rule.title} notifications`}
                />
            </div>

            <div className="space-y-4 p-5">
                <Field
                    label="Message template"
                    hint="Plain text is sent exactly as shown. Variables are filled at delivery time."
                >
                    <textarea
                        value={template}
                        onChange={(event) => setTemplate(event.target.value)}
                        rows={Math.max(5, Math.min(9, template.split("\n").length + 1))}
                        className="border-ink-600 bg-ink-900 text-fog-100 placeholder:text-fog-400 focus:border-brand-400 focus:ring-brand-500/25 w-full resize-y rounded-md border px-3 py-2.5 font-mono text-xs leading-relaxed outline-none focus:ring-2"
                        spellCheck={false}
                    />
                </Field>

                <div>
                    <div className="text-fog-400 mb-2 text-[11px] font-medium tracking-wider uppercase">
                        Insert variable
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                        {rule.variables.map((variable) => (
                            <button
                                key={variable}
                                type="button"
                                onClick={() => insertVariable(variable)}
                                className="border-ink-600 bg-ink-900 text-fog-300 hover:border-brand-400/60 hover:text-brand-300 rounded border px-2 py-1 font-mono text-[10px] transition-colors"
                            >
                                {`{{ ${variable} }}`}
                            </button>
                        ))}
                    </div>
                </div>

                <div className="flex flex-col gap-3 border-t border-dashed pt-4 sm:flex-row sm:items-end sm:justify-between">
                    <div className="w-full sm:w-52">
                        <Field label="Repeat cooldown" hint="0 sends once per unique event.">
                            <div className="relative">
                                <TextInput
                                    type="number"
                                    min={0}
                                    max={86400}
                                    value={cooldown}
                                    onChange={(event) =>
                                        setCooldown(Number(event.target.value) || 0)
                                    }
                                    className="pr-20 font-mono"
                                />
                                <span className="text-fog-400 pointer-events-none absolute inset-y-0 right-3 flex items-center text-xs">
                                    seconds
                                </span>
                            </div>
                        </Field>
                    </div>
                    <div className="flex items-center justify-end gap-2">
                        <Button
                            variant="ghost"
                            onClick={() => setTemplate(rule.default_template)}
                            disabled={template === rule.default_template || saving}
                        >
                            <RotateCcw size={14} /> Default
                        </Button>
                        <Button
                            variant="primary"
                            onClick={() => void save()}
                            disabled={!dirty || saving}
                        >
                            {saving ? <Spinner /> : saved ? <Check size={14} /> : null}
                            {saved ? "Saved" : "Save event"}
                        </Button>
                    </div>
                </div>
                {error ? <p className="text-bad-500 text-xs">{error}</p> : null}
            </div>
        </Card>
    );
}

export default function NotificationsPage() {
    const [settings, setSettings] = useState<NotificationSettings | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [enabled, setEnabled] = useState(false);
    const [botToken, setBotToken] = useState("");
    const [chatId, setChatId] = useState("");
    const [threadId, setThreadId] = useState("");
    const [notificationTimezone, setNotificationTimezone] = useState("Asia/Kolkata");
    const [savingChannel, setSavingChannel] = useState(false);
    const [testing, setTesting] = useState(false);
    const [notice, setNotice] = useState<string | null>(null);

    const applySettings = useCallback((next: NotificationSettings) => {
        setSettings(next);
        setEnabled(next.telegram.enabled);
        setChatId(next.telegram.chat_id ?? "");
        setThreadId(next.telegram.message_thread_id?.toString() ?? "");
        setNotificationTimezone(next.telegram.timezone || "Asia/Kolkata");
        setBotToken("");
    }, []);

    const load = useCallback(
        () =>
            api.notificationSettings().then(
                (next) => {
                    setError(null);
                    applySettings(next);
                    setLoading(false);
                },
                (err: unknown) => {
                    setError(
                        err instanceof Error
                            ? err.message
                            : "Could not load notification settings.",
                    );
                    setLoading(false);
                },
            ),
        [applySettings],
    );

    useEffect(() => {
        void load();
    }, [load]);

    const saveChannel = async () => {
        setSavingChannel(true);
        setError(null);
        setNotice(null);
        try {
            const payload: {
                enabled: boolean;
                bot_token?: string;
                chat_id?: string;
                message_thread_id?: number | null;
                timezone?: string;
            } = {
                enabled,
                chat_id: chatId.trim(),
                message_thread_id: threadId.trim() ? Number(threadId) : null,
                timezone: notificationTimezone.trim(),
            };
            if (botToken.trim()) payload.bot_token = botToken.trim();
            applySettings(await api.updateTelegram(payload));
            setNotice("Telegram destination saved.");
        } catch (err) {
            setError(err instanceof Error ? err.message : "Could not save Telegram settings.");
        } finally {
            setSavingChannel(false);
        }
    };

    const testChannel = async () => {
        setTesting(true);
        setError(null);
        setNotice(null);
        try {
            const result = await api.testTelegram();
            setNotice(result.detail);
            void load();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Telegram test failed.");
        } finally {
            setTesting(false);
        }
    };

    const channelDirty = useMemo(() => {
        if (!settings) return false;
        return (
            botToken.length > 0 ||
            enabled !== settings.telegram.enabled ||
            chatId !== (settings.telegram.chat_id ?? "") ||
            threadId !== (settings.telegram.message_thread_id?.toString() ?? "") ||
            notificationTimezone !== settings.telegram.timezone
        );
    }, [settings, botToken, enabled, chatId, threadId, notificationTimezone]);

    if (loading && !settings) {
        return (
            <Card className="p-5">
                <LoadingState label="Loading notification routes…" />
            </Card>
        );
    }
    if (!settings) {
        return (
            <ErrorState
                message={error ?? "Could not load notification settings."}
                onRetry={() => {
                    setLoading(true);
                    setError(null);
                    void load();
                }}
            />
        );
    }

    const systemRules = settings.rules.filter((rule) => SYSTEM_EVENTS.has(rule.event_type));
    const healthRules = settings.rules.filter((rule) => HEALTH_EVENTS.has(rule.event_type));
    const accountRules = settings.rules.filter((rule) => ACCOUNT_EVENTS.has(rule.event_type));
    const reportRules = settings.rules.filter((rule) => REPORT_EVENTS.has(rule.event_type));
    const clientRules = settings.rules.filter(
        (rule) =>
            !ACCOUNT_EVENTS.has(rule.event_type) &&
            !SYSTEM_EVENTS.has(rule.event_type) &&
            !HEALTH_EVENTS.has(rule.event_type) &&
            !REPORT_EVENTS.has(rule.event_type),
    );

    return (
        <div className="space-y-7">
            <header>
                <h1 className="text-fog-100 text-2xl font-semibold tracking-tight">
                    Notification routing
                </h1>
                <p className="text-fog-400 mt-0.5 text-sm">
                    Choose which operational signals leave the proxy, where they land, and the exact
                    words they carry.
                </p>
            </header>

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

            <div className="grid items-start gap-6 xl:grid-cols-[minmax(300px,0.72fr)_minmax(520px,1.28fr)]">
                <div className="space-y-4 xl:sticky xl:top-6">
                    <div className="flex items-center gap-2 px-1">
                        <Waypoints size={15} className="text-brand-300" />
                        <h2 className="text-fog-200 text-xs font-semibold tracking-[0.16em] uppercase">
                            Destination
                        </h2>
                    </div>
                    <Card className="overflow-hidden">
                        <div className="border-ink-700 bg-ink-900/50 flex items-start justify-between gap-4 border-b px-5 py-4">
                            <div className="flex items-center gap-3">
                                <div className="border-ink-600 bg-ink-800 text-fog-200 flex h-9 w-9 items-center justify-center rounded-lg border">
                                    <Bot size={18} />
                                </div>
                                <div>
                                    <div className="text-fog-100 text-sm font-semibold">
                                        Telegram bot
                                    </div>
                                    <div className="text-fog-400 mt-0.5 text-xs">
                                        First-party Bot API delivery
                                    </div>
                                </div>
                            </div>
                            <TextToggle
                                on={enabled}
                                onClick={() => setEnabled((value) => !value)}
                                onLabel="On"
                                offLabel="Off"
                                srLabel="Enable Telegram notifications"
                            />
                        </div>
                        <div className="space-y-5 p-5">
                            <Field
                                label="Bot token"
                                hint={
                                    settings.telegram.configured
                                        ? "A token is saved. Leave blank to keep it."
                                        : "Create one with BotFather, then paste it here once."
                                }
                            >
                                <TextInput
                                    type="password"
                                    value={botToken}
                                    onChange={(event) => setBotToken(event.target.value)}
                                    autoComplete="new-password"
                                    placeholder={
                                        settings.telegram.configured
                                            ? "••••••••••••••••  saved"
                                            : "123456789:AA…"
                                    }
                                />
                            </Field>

                            <Field
                                label="Group or chat ID"
                                hint="Groups and channels usually use a negative numeric ID."
                            >
                                <TextInput
                                    value={chatId}
                                    onChange={(event) => setChatId(event.target.value)}
                                    placeholder="-1001234567890"
                                    spellCheck={false}
                                    className="font-mono"
                                />
                            </Field>

                            <Field
                                label="Topic ID (optional)"
                                hint="Use this only when the group has Topics enabled."
                            >
                                <TextInput
                                    type="number"
                                    min={1}
                                    value={threadId}
                                    onChange={(event) => setThreadId(event.target.value)}
                                    placeholder="General topic"
                                    className="font-mono"
                                />
                            </Field>

                            <Field
                                label="Timezone"
                                hint="IANA timezone used for daily, weekly, and monthly report boundaries and delivery timing."
                            >
                                <TimezoneSelect
                                    value={notificationTimezone}
                                    onChange={setNotificationTimezone}
                                />
                            </Field>

                            <div className="flex flex-wrap justify-end gap-2 pt-1">
                                <Button
                                    variant="ghost"
                                    onClick={() => void testChannel()}
                                    disabled={
                                        !settings.telegram.configured || testing || channelDirty
                                    }
                                >
                                    {testing ? <Spinner /> : <Send size={14} />} Send test
                                </Button>
                                <Button
                                    variant="primary"
                                    onClick={() => void saveChannel()}
                                    disabled={!channelDirty || savingChannel}
                                >
                                    {savingChannel ? <Spinner /> : <ShieldCheck size={14} />} Save
                                    destination
                                </Button>
                            </div>
                        </div>
                        <div className="border-ink-700 bg-ink-900/45 space-y-1 border-t px-5 py-3 text-[11px]">
                            <div className="flex justify-between gap-3">
                                <span className="text-fog-400">Last delivered</span>
                                <span className="text-fog-300 text-right">
                                    {settings.telegram.last_success_at
                                        ? formatDateTime(settings.telegram.last_success_at)
                                        : "Never"}
                                </span>
                            </div>
                            {settings.telegram.last_error ? (
                                <div className="text-bad-500 mt-2 break-words">
                                    {settings.telegram.last_error}
                                </div>
                            ) : null}
                        </div>
                    </Card>
                    <div className="border-ink-700 bg-ink-900/60 text-fog-400 rounded-lg border px-4 py-3 text-xs leading-relaxed">
                        Bot tokens are encrypted before storage and are never returned to this page.
                        Delivery failures never interrupt proxy traffic.
                    </div>
                    <div className="border-ink-700 bg-ink-900/60 rounded-lg border px-4 py-3">
                        <div className="text-fog-200 text-[11px] font-semibold tracking-wider uppercase">
                            Telegram bot command
                        </div>
                        <code className="text-brand-300 mt-2 block text-xs">
                            /claude status usable
                        </code>
                        <p className="text-fog-400 mt-2 text-[11px] leading-relaxed">
                            Replace <code>usable</code> with <code>available</code> or{" "}
                            <code>all</code>. Replies are restricted to the saved chat and topic.
                        </p>
                    </div>
                </div>

                <div className="space-y-7">
                    <RuleGroup
                        title="Delivery lifecycle"
                        description="Confirms when a destination becomes ready to receive alerts."
                        rules={systemRules}
                        onSaved={applySettings}
                    />
                    <RuleGroup
                        title="Scheduled reports"
                        description="Completed-period usage summaries with request, token, and active-user distribution."
                        rules={reportRules}
                        onSaved={applySettings}
                    />
                    <RuleGroup
                        title="Account & pool signals"
                        description="High-value operational events, enabled by default."
                        rules={accountRules}
                        onSaved={applySettings}
                    />
                    <RuleGroup
                        title="Client guardrails"
                        description="User and API-key alerts; muted by default to keep the group quiet."
                        rules={clientRules}
                        onSaved={applySettings}
                    />
                    <RuleGroup
                        title="System health"
                        description="Aggregated service-health alerts when multiple users encounter elevated 503 errors."
                        rules={healthRules}
                        onSaved={applySettings}
                    />
                </div>
            </div>
        </div>
    );
}
