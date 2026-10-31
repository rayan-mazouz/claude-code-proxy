import assert from "node:assert/strict";
import test from "node:test";
import { groupRequestUsers, OTHERS_ID } from "../src/lib/requestAnalytics.ts";

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
