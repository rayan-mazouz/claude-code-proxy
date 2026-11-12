import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
function compile(path, aliases = {}) {
    const source = readFileSync(new URL(path, import.meta.url), "utf8");
    const { outputText } = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    });
    const compiled = { exports: {} };
    new Function("require", "module", "exports", outputText)(
        (name) => aliases[name] ?? require(name),
        compiled,
        compiled.exports,
    );
    return compiled.exports;
}
const format = compile("../src/lib/format.ts");
const { UsageLimitsCard } = compile("../src/components/UsageLimitsCard.tsx", {
    "@/lib/format": format,
    "@/components/ui": { Card: ({ children }) => React.createElement("div", null, children) },
});
const stats = {
    input_rate_pct: 99.86,
    cache_hit_rate_pct: 95.5,
    five_hour_average_pct: 75.5,
    weekly_average_pct: 71.8,
    active_accounts: 10,
    total_accounts: 26,
    five_hour_reset_at: new Date(Date.now() + 19 * 60000 + 1000).toISOString(),
    weekly_reset_at: new Date(Date.now() + 3 * 86400000).toISOString(),
};
const render = (values = {}) =>
    renderToStaticMarkup(React.createElement(UsageLimitsCard, { stats: { ...stats, ...values } }));
const text = (html) => html.replace(/<[^>]*>/g, "").replace(/\s+/g, " ");

test("quota account-count line remains plain text with live counts", () => {
    const html = render({ active_accounts: 7, total_accounts: 23 });
    assert.match(text(html), /Quota averages across 7 \/ 23 authenticated and enabled accounts/);
    assert.doesNotMatch(html, /rounded-full|role="progressbar"|185\.21M|193\.83M/);
});
test("all four approved metrics use real data without removed helper text", () => {
    const content = text(render());
    for (const label of [
        "Usage &amp; limits",
        "Input rate",
        "Cache hit rate",
        "Average 5-hour limit",
        "Average weekly limit",
    ])
        assert.ok(content.includes(label), label);
    for (const value of ["99.86%", "95.5%", "75.5%", "71.8%", "Next reset in 19m"])
        assert.ok(content.includes(value), value);
    assert.doesNotMatch(content, /remaining|of input tokens cached|input \/|average used/);
});
test("missing or expired reset timestamps have honest fallback text", () => {
    assert.match(
        text(render({ five_hour_reset_at: null, weekly_reset_at: "invalid" })),
        /Next reset unavailable/,
    );
    assert.match(text(render({ five_hour_reset_at: "2020-01-01T00:00:00Z" })), /Resetting…/);
});

test("shows banked reset totals only when the provider supports them", () => {
    assert.match(text(render({ total_reset_credits_available: 13 })), /Available limit resets13/);
    assert.match(text(render({ total_reset_credits_available: 0 })), /Available limit resets0/);
    assert.doesNotMatch(
        text(render({ total_reset_credits_available: null })),
        /Available limit resets/,
    );
});

test("reset credits are the fifth metric, a count, and never repeated in the footer", () => {
    const html = render({ total_reset_credits_available: 345 });
    const labels = [...html.matchAll(/<dt[^>]*>(.*?)<\/dt>/g)].map((match) => match[1]);
    assert.equal(labels.length, 5);
    assert.equal(labels[4], "Available limit resets");
    const values = [...html.matchAll(/<dd[^>]*>(.*?)<\/dd>/g)].map((match) => text(match[1]));
    assert.equal(values.at(-1), "345");
    assert.equal((html.match(/Available limit resets/g) ?? []).length, 1);
    const unsupported = render({ total_reset_credits_available: null });
    assert.equal([...unsupported.matchAll(/<dt[^>]*>/g)].length, 4);
});
