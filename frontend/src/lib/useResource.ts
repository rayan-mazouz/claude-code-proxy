"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";

export interface Resource<T> {
    /** The most recent successful result, kept while a newer request is in flight. */
    data: T | undefined;
    /** The error from the most recent request, cleared when a newer one succeeds. */
    error: string | null;
    /** True while the request for the current key has not settled yet. */
    loading: boolean;
    /** Re-run the request for the same key. */
    reload: () => void;
}

interface Settled<T> {
    request: string;
    data: T | undefined;
    error: string | null;
}

function message(error: unknown): string {
    return error instanceof Error ? error.message : "Request failed.";
}

/**
 * Load data whenever `key` changes (or `reload` is called).
 *
 * `key` identifies the request: build it from every input `load` reads, for example
 * `JSON.stringify([range, userId])`. Loading is derived from whether the request for the
 * current key has settled, so callers never set state synchronously inside an effect, and
 * responses to superseded requests are discarded instead of overwriting newer data.
 * Pass `enabled: false` to hold off until the inputs are ready.
 */
export function useResource<T>(
    load: () => Promise<T>,
    key: string,
    { enabled = true }: { enabled?: boolean } = {},
): Resource<T> {
    const [generation, setGeneration] = useState(0);
    const [settled, setSettled] = useState<Settled<T> | null>(null);
    const request = `${generation}\u0000${key}`;
    const run = useEffectEvent(load);

    useEffect(() => {
        if (!enabled) return;
        let current = true;
        run().then(
            (data) => {
                if (current) setSettled({ request, data, error: null });
            },
            (error: unknown) => {
                if (current) {
                    setSettled((previous) => ({
                        request,
                        data: previous?.data,
                        error: message(error),
                    }));
                }
            },
        );
        return () => {
            current = false;
        };
    }, [request, enabled]);

    const reload = useCallback(() => setGeneration((value) => value + 1), []);

    return {
        data: settled?.data,
        error: settled?.error ?? null,
        loading: enabled && settled?.request !== request,
        reload,
    };
}
