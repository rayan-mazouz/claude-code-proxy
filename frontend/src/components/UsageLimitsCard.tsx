import { OverviewStats } from "@/lib/types";
import { formatCountdown, formatNumber } from "@/lib/format";
import { Card } from "@/components/ui";

function nextReset(resetAt: string | null): string {
    const countdown = formatCountdown(resetAt);
    if (countdown.startsWith("resets in ")) return `Next reset in ${countdown.slice(10)}`;
    return countdown === "resetting…" ? "Resetting…" : "Next reset unavailable";
}

export function UsageLimitsCard({ stats }: { stats: OverviewStats }) {
    const hasResetCredits = stats.total_reset_credits_available != null;
    const metrics: {
        label: string;
        value: number;
        decimals?: number;
        reset?: string;
        count?: boolean;
    }[] = [
        { label: "Input rate", value: stats.input_rate_pct, decimals: 2 },
        { label: "Cache hit rate", value: stats.cache_hit_rate_pct, decimals: 1 },
        {
            label: "Average 5-hour limit",
            value: stats.five_hour_average_pct,
            decimals: 1,
            reset: nextReset(stats.five_hour_reset_at),
        },
        {
            label: "Average weekly limit",
            value: stats.weekly_average_pct,
            decimals: 1,
            reset: nextReset(stats.weekly_reset_at),
        },
    ];
    if (stats.total_reset_credits_available != null) {
        metrics.push({
            label: "Available limit resets",
            value: stats.total_reset_credits_available,
            count: true,
        });
    }

    return (
        <Card className="@container overflow-hidden">
            <h2 className="text-fog-100 px-5 pt-5 text-sm font-semibold @min-[640px]:px-6">
                Usage &amp; limits
            </h2>
            <dl
                className={`grid grid-cols-2 ${hasResetCredits ? "@min-[900px]:grid-cols-5" : "@min-[640px]:grid-cols-4"}`}
            >
                {metrics.map(({ label, value, decimals, reset, count }, index) => (
                    <div
                        key={label}
                        className={`border-ink-700 min-w-0 px-4 py-5 @min-[640px]:px-6 ${index % 2 ? "border-l" : ""} ${index >= 2 ? `border-t ${hasResetCredits ? "@min-[900px]:border-t-0" : "@min-[640px]:border-t-0"}` : ""} ${index > 0 ? (hasResetCredits ? "@min-[900px]:border-l" : "@min-[640px]:border-l") : ""} ${count ? "col-span-2 flex items-center justify-between gap-4 @min-[900px]:col-span-1 @min-[900px]:block" : ""}`}
                    >
                        <dt className="text-fog-400 text-xs font-medium">{label}</dt>
                        <dd
                            className={`text-fog-100 font-mono text-[1.75rem] leading-tight font-semibold tracking-tight whitespace-nowrap tabular-nums @min-[640px]:text-[2rem] @min-[1000px]:text-4xl ${count ? "@min-[900px]:mt-3" : "mt-3"}`}
                        >
                            {count
                                ? formatNumber(Math.max(0, value))
                                : Math.max(0, Math.min(100, value)).toFixed(decimals)}
                            {!count ? (
                                <span className="text-[1.25rem] @min-[1000px]:text-2xl">%</span>
                            ) : null}
                        </dd>
                        {reset ? <dd className="text-fog-400 mt-2 text-xs">{reset}</dd> : null}
                    </div>
                ))}
            </dl>
            <div className="border-ink-700 text-fog-400 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-t px-5 py-3 text-xs @min-[640px]:px-6">
                <p>
                    Quota averages across{" "}
                    <span className="text-fog-100 font-mono tabular-nums">
                        {formatNumber(stats.active_accounts)} / {formatNumber(stats.total_accounts)}
                    </span>{" "}
                    authenticated and enabled accounts
                </p>
            </div>
        </Card>
    );
}
