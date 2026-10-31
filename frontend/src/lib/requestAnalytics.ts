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
