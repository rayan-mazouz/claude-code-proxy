export const OTHERS_ID = "__others__";

/** Group users by their share over the complete selected range, not per bucket. */
export function groupRequestUsers(
    buckets: Record<string, number>[],
    cutoff = 0.05,
): Record<string, number>[] {
    const totals = new Map<string, number>();
    for (const bucket of buckets) {
        for (const [id, count] of Object.entries(bucket))
            totals.set(id, (totals.get(id) ?? 0) + count);
    }
    const total = [...totals.values()].reduce((sum, count) => sum + count, 0);
    const smaller = new Set(
        [...totals].filter(([, count]) => total > 0 && count / total < cutoff).map(([id]) => id),
    );
    return buckets.map((bucket) => {
        const result: Record<string, number> = {};
        for (const [id, count] of Object.entries(bucket)) {
            const key = smaller.has(id) ? OTHERS_ID : id;
            result[key] = (result[key] ?? 0) + count;
        }
        return result;
    });
}

export const REQUEST_CHART_X_KEY = "timestamp";

/** Recharts interprets dots/brackets as object paths. Encode the identity,
 * never its current ranking, so one line cannot inherit another series' data. */
export function requestSeriesKey(view: string, id: string): string {
    return `${view}_${Array.from(id, (character) => character.codePointAt(0)!.toString(16)).join("_")}`;
}

export function requestChartRow(
    timestamp: string,
    values: Record<string, number> | undefined,
    series: readonly { id: string; key: string }[],
    missing: number | null,
): Record<string, number | string | null> {
    return {
        [REQUEST_CHART_X_KEY]: timestamp,
        ...Object.fromEntries(series.map((item) => [item.key, values?.[item.id] ?? missing])),
    };
}

/** Use the exact row plotted by the lines, rather than cached tooltip registrations. */
export function requestTooltipEntries(
    row: Record<string, unknown>,
    series: readonly { id: string; key: string; label: string; color: string }[],
    hidden: ReadonlySet<string>,
    view: string,
) {
    return series.flatMap((item) => {
        const value = row[item.key];
        return !hidden.has(`${view}:${item.id}`) &&
            typeof value === "number" &&
            Number.isFinite(value)
            ? [{ ...item, value }]
            : [];
    });
}
