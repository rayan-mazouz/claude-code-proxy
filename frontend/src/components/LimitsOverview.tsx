"use client";

// ---------------------------------------------------------------------------
// Provider-limit panels for the overview: each account's windows, the pool's
// state at a glance, and how much of the accounts' limits each user or API key
// consumed (with their limit budgets). Shares are in percent of one account's
// window; summed across accounts, 200% means two full accounts.
// ---------------------------------------------------------------------------

import { Fragment, useState } from "react";
import { ChevronRight } from "lucide-react";
import {
    AccountLimits,
    AccountLimitWindow,
    LimitsReport,
    OwnerLimitUsage,
    QuotaShare,
    QuotaWindowKey,
} from "@/lib/types";
import { formatCountdown, formatNumber, tierLabel } from "@/lib/format";
import { Badge, Card, Segmented } from "@/components/ui";

export function formatShare(fraction: number): string {
    const percent = fraction * 100;
    return `${percent >= 10 || percent === 0 ? percent.toFixed(0) : percent.toFixed(1)}%`;
}

function tone(fraction: number): string {
    return fraction >= 0.9 ? "bg-bad-500" : fraction >= 0.7 ? "bg-warn-500" : "bg-brand-500";
}

function Bar({ fraction, marker }: { fraction: number; marker?: number | null }) {
    const width = Math.min(Math.max(fraction, 0), 1);
    return (
        <div className="bg-ink-700 relative h-1.5 w-full overflow-hidden rounded-full">
            <div
                className={`h-full rounded-full transition-all ${tone(width)}`}
                style={{ width: `${width * 100}%` }}
            />
            {marker != null && marker > 0 && marker < 1 ? (
                <div
                    aria-hidden
                    className="bg-fog-100/80 absolute top-0 h-full w-px"
                    style={{ left: `${marker * 100}%` }}
                />
            ) : null}
        </div>
    );
}

function resetText(resetAt: string | null): string {
    const countdown = formatCountdown(resetAt);
    return countdown === "unknown" ? "reset unknown" : countdown;
}

// --- Account windows ------------------------------------------------------

function accountState(account: AccountLimits): { label: string; tone: "good" | "warn" | "bad" } {
    if (account.status === "DISABLED") return { label: "Disabled", tone: "bad" };
    const windows = [account.five_hour, account.weekly, account.monthly].filter(
        (window): window is AccountLimitWindow => window !== null,
    );
    if (windows.some((window) => (window.used_pct ?? 0) >= window.threshold))
        return { label: "Limit reached", tone: "bad" };
    if (account.cooldown_until && Date.parse(account.cooldown_until) > Date.now())
        return { label: "Cooldown", tone: "warn" };
    return account.usable
        ? { label: "Usable", tone: "good" }
        : { label: "Unavailable", tone: "warn" };
}

function WindowCell({
    window,
    proxyShare,
    empty,
}: {
    window: AccountLimitWindow | null;
    proxyShare: QuotaShare | null;
    empty: string;
}) {
    if (!window) return <div className="text-fog-500 text-xs">{empty}</div>;
    if (window.used_pct === null)
        return <div className="text-fog-400 text-xs">Not reported yet</div>;
    return (
        <div className="min-w-0">
            <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="text-fog-100 font-semibold tabular-nums">
                    {formatShare(window.used_pct)}
                </span>
                <span className="text-fog-400 truncate">{resetText(window.reset_at)}</span>
            </div>
            <div className="mt-1.5">
                <Bar
                    fraction={window.used_pct}
                    marker={window.threshold < 1 ? window.threshold : null}
                />
            </div>
            {proxyShare && proxyShare.used_pct > 0 ? (
                <div className="text-fog-500 mt-1 text-[11px] tabular-nums">
                    {formatShare(Math.min(proxyShare.used_pct, window.used_pct))} via proxy
                </div>
            ) : null}
        </div>
    );
}

/** Every account's 5-hour, weekly and scoped windows, one row per account. */
export function AccountLimitsTable({ accounts }: { accounts: AccountLimits[] }) {
    if (accounts.length === 0)
        return <div className="text-fog-400 px-5 py-6 text-sm">No accounts yet.</div>;
    return (
        <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="text-fog-400 text-xs">
                    <tr>
                        <th className="px-5 py-2.5 font-medium">Account</th>
                        <th className="w-[22%] px-3 py-2.5 font-medium">5-hour</th>
                        <th className="w-[22%] px-3 py-2.5 font-medium">Weekly</th>
                        <th className="w-[20%] px-3 py-2.5 font-medium">Model allowances</th>
                        <th className="px-5 py-2.5 text-right font-medium">State</th>
                    </tr>
                </thead>
                <tbody>
                    {accounts.map((account) => {
                        const state = accountState(account);
                        return (
                            <tr key={account.id} className="border-ink-700 border-t align-top">
                                <td className="px-5 py-3">
                                    <div className="text-fog-100 font-medium">{account.label}</div>
                                    <div className="text-fog-400 mt-0.5 text-xs">
                                        {tierLabel(account.tier)}
                                    </div>
                                </td>
                                <td className="px-3 py-3">
                                    <WindowCell
                                        window={account.five_hour}
                                        proxyShare={account.proxy_share.five_hour}
                                        empty="No 5-hour limit"
                                    />
                                </td>
                                <td className="px-3 py-3">
                                    <WindowCell
                                        window={account.weekly}
                                        proxyShare={account.proxy_share.weekly}
                                        empty="Not reported yet"
                                    />
                                </td>
                                <td className="px-3 py-3">
                                    {account.scoped.length === 0 ? (
                                        <span className="text-fog-500 text-xs">None reported</span>
                                    ) : (
                                        <div className="space-y-2">
                                            {account.scoped.map((limit) => (
                                                <div key={limit.model} className="text-xs">
                                                    <div className="flex items-baseline justify-between gap-2">
                                                        <span className="text-fog-300">
                                                            {limit.model}
                                                        </span>
                                                        <span className="text-fog-100 font-semibold tabular-nums">
                                                            {limit.used_pct === null
                                                                ? "—"
                                                                : formatShare(limit.used_pct)}
                                                        </span>
                                                    </div>
                                                    {limit.used_pct !== null ? (
                                                        <div className="mt-1">
                                                            <Bar fraction={limit.used_pct} />
                                                        </div>
                                                    ) : null}
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                </td>
                                <td className="px-5 py-3 text-right">
                                    <Badge tone={state.tone}>{state.label}</Badge>
                                    {account.monthly?.used_pct != null ? (
                                        <div className="text-fog-400 mt-1.5 text-xs tabular-nums">
                                            Monthly {formatShare(account.monthly.used_pct)}
                                        </div>
                                    ) : null}
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}

// --- Pool strip -----------------------------------------------------------

/** Pool state at a glance, from the per-account windows. */
export function PoolStatusStrip({ report }: { report: LimitsReport }) {
    const accounts = report.accounts;
    const enabled = accounts.filter((account) => account.status !== "DISABLED");
    const states = enabled.map(accountState);
    const usable = enabled.filter((account) => account.usable).length;
    const limited = states.filter((state) => state.label === "Limit reached").length;
    const cooling = states.filter((state) => state.label === "Cooldown").length;
    const noFiveHour = accounts.filter((account) => !account.has_five_hour_limit).length;
    const scopedExhausted = accounts.filter((account) =>
        account.scoped.some((limit) => limit.exhausted),
    ).length;
    const weeklyKnown = enabled.filter((account) => account.weekly.used_pct !== null);
    const weeklyHeadroom = weeklyKnown.reduce(
        (sum, account) =>
            sum + Math.max(0, account.weekly.threshold - (account.weekly.used_pct ?? 0)),
        0,
    );
    const budgeted = [...report.users, ...report.keys].filter(
        (owner) => owner.budget.five_hour || owner.budget.weekly,
    );
    const overBudget = budgeted.filter((owner) => budgetPressure(owner) >= 1).length;
    const tiles: { label: string; value: string; hint: string }[] = [
        {
            label: "Usable accounts",
            value: `${formatNumber(usable)} / ${formatNumber(enabled.length)}`,
            hint: `${formatNumber(accounts.length - enabled.length)} disabled`,
        },
        {
            label: "At a limit",
            value: formatNumber(limited),
            hint: cooling ? `${formatNumber(cooling)} cooling down` : "None cooling down",
        },
        {
            label: "Weekly headroom",
            value: formatShare(weeklyHeadroom),
            hint: "Of one account, summed to each threshold",
        },
        {
            label: "No 5-hour limit",
            value: formatNumber(noFiveHour),
            hint: noFiveHour ? "Weekly limit only" : "Every account has one",
        },
        {
            label: "Model allowance used up",
            value: formatNumber(scopedExhausted),
            hint: "Accounts skipped for that model",
        },
        {
            label: "Budgets reached",
            value: `${formatNumber(overBudget)} / ${formatNumber(budgeted.length)}`,
            hint: "Users and keys with a budget",
        },
    ];
    return (
        <Card className="overflow-hidden">
            <dl className="bg-ink-700 grid grid-cols-2 gap-px sm:grid-cols-3 xl:grid-cols-6">
                {tiles.map((tile) => (
                    <div key={tile.label} className="bg-ink-850 min-w-0 px-4 py-4 sm:px-5">
                        <dt className="text-fog-400 text-xs font-medium">{tile.label}</dt>
                        <dd className="text-fog-100 mt-2 text-2xl font-semibold tabular-nums">
                            {tile.value}
                        </dd>
                        <dd className="text-fog-500 mt-1 text-[11px]">{tile.hint}</dd>
                    </div>
                ))}
            </dl>
        </Card>
    );
}

// --- Limit usage by user / key -------------------------------------------

const BUDGET_WINDOWS: { key: Exclude<QuotaWindowKey, "monthly">; label: string }[] = [
    { key: "five_hour", label: "5-hour" },
    { key: "weekly", label: "Weekly" },
];

/** Highest used/budget ratio across the windows that have a budget (0 when none). */
function budgetPressure(owner: OwnerLimitUsage): number {
    return Math.max(
        0,
        ...BUDGET_WINDOWS.map(({ key }) => {
            const budget = owner.budget[key];
            const share = owner.usage[key];
            return budget && share ? share.used_pct / budget : 0;
        }),
    );
}

function ShareWithBudget({ share, budget }: { share: QuotaShare | null; budget: number | null }) {
    if (!share) return <span className="text-fog-500 text-xs">{budget ? "—" : "No window"}</span>;
    const scale = budget && budget > 0 ? budget : 1;
    return (
        <div className="min-w-0">
            <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="text-fog-100 font-semibold tabular-nums">
                    {formatShare(share.used_pct)}
                    {budget ? (
                        <span className="text-fog-400 font-normal"> / {formatShare(budget)}</span>
                    ) : null}
                </span>
                <span className="text-fog-500 truncate">{resetText(share.reset_at)}</span>
            </div>
            <div className="mt-1.5">
                <Bar fraction={share.used_pct / scale} />
            </div>
        </div>
    );
}

type OwnerView = "users" | "keys";

/** Each user's or key's share of the accounts' open windows, with limit budgets and a per-account split. */
export function LimitSharesCard({ report }: { report: LimitsReport }) {
    const [view, setView] = useState<OwnerView>("users");
    const [expanded, setExpanded] = useState<string | null>(null);
    const owners = (view === "users" ? report.users : report.keys)
        .filter(
            (owner) =>
                owner.usage.five_hour?.used_pct ||
                owner.usage.weekly?.used_pct ||
                owner.usage.monthly?.used_pct ||
                owner.budget.five_hour ||
                owner.budget.weekly,
        )
        .sort(
            (a, b) =>
                budgetPressure(b) - budgetPressure(a) ||
                (b.usage.weekly?.used_pct ?? 0) - (a.usage.weekly?.used_pct ?? 0) ||
                (b.usage.five_hour?.used_pct ?? 0) - (a.usage.five_hour?.used_pct ?? 0) ||
                a.name.localeCompare(b.name),
        );
    return (
        <Card className="overflow-hidden">
            <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-5">
                <div>
                    <h2 className="text-fog-100 text-sm font-semibold">Limit usage</h2>
                    <p className="text-fog-400 mt-0.5 text-xs">
                        Share of the accounts&apos; open windows, in percent of one account&apos;s
                        limit, summed across accounts. Budgets show as used / cap.
                    </p>
                </div>
                <Segmented
                    options={[
                        { value: "users", label: "By user" },
                        { value: "keys", label: "By key" },
                    ]}
                    value={view}
                    onChange={(next) => {
                        setView(next);
                        setExpanded(null);
                    }}
                    label="Group limit usage"
                />
            </div>
            {owners.length === 0 ? (
                <div className="text-fog-400 px-5 py-6 text-sm">
                    No {view === "users" ? "user" : "key"} has used the accounts&apos; current
                    windows yet.
                </div>
            ) : (
                <div className="mt-3 overflow-x-auto">
                    <table className="w-full min-w-[680px] text-left text-sm">
                        <thead className="text-fog-400 text-xs">
                            <tr>
                                <th className="px-5 py-2.5 font-medium">
                                    {view === "users" ? "User" : "Key"}
                                </th>
                                <th className="w-[26%] px-3 py-2.5 font-medium">5-hour</th>
                                <th className="w-[26%] px-3 py-2.5 font-medium">Weekly</th>
                                <th className="px-5 py-2.5 text-right font-medium">Accounts</th>
                            </tr>
                        </thead>
                        <tbody>
                            {owners.map((owner) => {
                                const open = expanded === owner.id;
                                return (
                                    <Fragment key={owner.id}>
                                        <tr className="border-ink-700 border-t align-top">
                                            <td className="px-5 py-3">
                                                <div className="text-fog-100 flex items-center gap-2 font-medium">
                                                    {owner.name}
                                                    {!owner.active ? (
                                                        <Badge tone="neutral">Disabled</Badge>
                                                    ) : null}
                                                    {budgetPressure(owner) >= 1 ? (
                                                        <Badge tone="bad">Budget reached</Badge>
                                                    ) : null}
                                                </div>
                                                {owner.user_name ? (
                                                    <div className="text-fog-400 mt-0.5 text-xs">
                                                        {owner.user_name}
                                                    </div>
                                                ) : null}
                                            </td>
                                            {BUDGET_WINDOWS.map(({ key }) => (
                                                <td key={key} className="px-3 py-3">
                                                    <ShareWithBudget
                                                        share={owner.usage[key]}
                                                        budget={owner.budget[key]}
                                                    />
                                                </td>
                                            ))}
                                            <td className="px-5 py-3 text-right">
                                                {owner.by_account.length > 0 ? (
                                                    <button
                                                        type="button"
                                                        aria-expanded={open}
                                                        onClick={() =>
                                                            setExpanded(open ? null : owner.id)
                                                        }
                                                        className="text-fog-300 hover:text-fog-100 inline-flex items-center gap-1 text-xs"
                                                    >
                                                        {owner.by_account.length}
                                                        <ChevronRight
                                                            size={14}
                                                            aria-hidden
                                                            className={`transition-transform ${open ? "rotate-90" : ""}`}
                                                        />
                                                    </button>
                                                ) : (
                                                    <span className="text-fog-500 text-xs">0</span>
                                                )}
                                            </td>
                                        </tr>
                                        {open
                                            ? owner.by_account.map((entry) => (
                                                  <tr
                                                      key={`${owner.id}:${entry.account_id}`}
                                                      className="bg-ink-900/50 text-xs"
                                                  >
                                                      <td className="text-fog-300 py-2 pr-5 pl-9">
                                                          {entry.account_label}
                                                      </td>
                                                      {BUDGET_WINDOWS.map(({ key }) => (
                                                          <td
                                                              key={key}
                                                              className="text-fog-200 px-3 py-2 tabular-nums"
                                                          >
                                                              {entry[key]
                                                                  ? formatShare(entry[key].used_pct)
                                                                  : "—"}
                                                          </td>
                                                      ))}
                                                      <td className="text-fog-500 px-5 py-2 text-right tabular-nums">
                                                          {entry.monthly
                                                              ? `Monthly ${formatShare(entry.monthly.used_pct)}`
                                                              : ""}
                                                      </td>
                                                  </tr>
                                              ))
                                            : null}
                                    </Fragment>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            )}
        </Card>
    );
}
