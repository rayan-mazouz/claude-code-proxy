import assert from "node:assert/strict";
import test from "node:test";
import {
    groupRequestUsers,
    OTHERS_ID,
    REQUEST_CHART_X_KEY,
    requestSeriesKey,
    requestChartRow,
    requestTooltipEntries,
} from "../src/lib/requestAnalytics.ts";

test("groups users below five percent and preserves every bucket total", () => {
    const buckets = [
        { large: 90, boundary: 5, smallA: 3, smallB: 2 },
        { large: 90, boundary: 5, smallA: 2, smallB: 3 },
    ];
    const grouped = groupRequestUsers(buckets);
    assert.deepEqual(grouped, [
        { large: 90, boundary: 5, [OTHERS_ID]: 5 },
        { large: 90, boundary: 5, [OTHERS_ID]: 5 },
    ]);
    grouped.forEach((bucket, i) =>
        assert.equal(
            Object.values(bucket).reduce((a, b) => a + b, 0),
            Object.values(buckets[i]).reduce((a, b) => a + b, 0),
        ),
    );
});
test("uses selected-range share, not each bucket's share", () => {
    assert.deepEqual(groupRequestUsers([{ major: 1000 }, { burst: 10 }]), [
        { major: 1000 },
        { [OTHERS_ID]: 10 },
    ]);
});
test("handles empty histories and all-small cohorts", () => {
    assert.deepEqual(groupRequestUsers([{}, {}]), [{}, {}]);
    const many = Object.fromEntries(Array.from({ length: 25 }, (_, i) => ["u" + i, 1]));
    assert.deepEqual(groupRequestUsers([many]), [{ [OTHERS_ID]: 25 }]);
});

const definitions = (ids, view = "users") =>
    ids.map((id) => ({ id, key: requestSeriesKey(view, id), label: id, color: id + "-color" }));
test("series keep the same data key when user rankings change", () => {
    const before = definitions(["devasheesh", "osama", "sanket"]);
    const after = definitions(["sanket", "devasheesh", "osama"]);
    for (const item of before)
        assert.equal(after.find((other) => other.id === item.id).key, item.key);
    const row = requestChartRow("2026-11-01T08:00:00Z", { devasheesh: 174 }, after, 0);
    assert.equal(row[before[0].key], 174);
    assert.deepEqual(
        requestTooltipEntries(row, after, new Set(), "users").map(({ id, value }) => [id, value]),
        [
            ["sanket", 0],
            ["devasheesh", 174],
            ["osama", 0],
        ],
    );
});
test("hour labels repeat across days but plotted bucket identities do not", () => {
    const series = definitions(["osama"]);
    const first = requestChartRow("2026-11-01T08:00:00Z", { osama: 0 }, series, 0);
    const second = requestChartRow("2026-11-02T08:00:00Z", { osama: 44 }, series, 0);
    assert.notEqual(first[REQUEST_CHART_X_KEY], second[REQUEST_CHART_X_KEY]);
    assert.equal(requestTooltipEntries(first, series, new Set(), "users")[0].value, 0);
    assert.equal(requestTooltipEntries(second, series, new Set(), "users")[0].value, 44);
});
test("model dots are not object paths; missing TPS stays absent and hidden series stay hidden", () => {
    const series = definitions(["gpt-6.1-sol", "gpt-6-luna"], "tps");
    assert.ok(!series[0].key.includes("."));
    assert.notEqual(requestSeriesKey("users", "same"), requestSeriesKey("models", "same"));
    const row = requestChartRow("2026-11-01T08:00:00Z", { "gpt-6.1-sol": 25.5 }, series, null);
    assert.equal(row[series[1].key], null);
    assert.equal(requestTooltipEntries(row, series, new Set(), "tps").length, 1);
    assert.deepEqual(requestTooltipEntries(row, series, new Set(["tps:gpt-6.1-sol"]), "tps"), []);
});
