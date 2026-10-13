"use client";

import { useCallback, useSyncExternalStore } from "react";

// Same-tab writes do not fire "storage" events, so notify subscribers directly.
const LOCAL_EVENT = "claude-proxy:stored-state";

function subscribe(onChange: () => void) {
    window.addEventListener("storage", onChange);
    window.addEventListener(LOCAL_EVENT, onChange);
    return () => {
        window.removeEventListener("storage", onChange);
        window.removeEventListener(LOCAL_EVENT, onChange);
    };
}

function read(key: string): string | null {
    try {
        return window.localStorage.getItem(key);
    } catch {
        return null; // Storage can be unavailable (privacy modes, disabled cookies).
    }
}

/**
 * A preference persisted in localStorage. Server rendering and the first client render use
 * `fallback`, so hydration never mismatches; the stored value applies right after.
 */
export function useStoredState<T>(
    key: string,
    fallback: T,
    parse: (raw: string) => T | undefined,
    serialize: (value: T) => string = String,
): [T, (value: T) => void] {
    const raw = useSyncExternalStore(
        subscribe,
        () => read(key),
        () => null,
    );
    const parsed = raw === null ? undefined : parse(raw);
    const value = parsed === undefined ? fallback : parsed;

    const setValue = useCallback(
        (next: T) => {
            try {
                window.localStorage.setItem(key, serialize(next));
            } catch {
                /* Keep working without persistence. */
            }
            window.dispatchEvent(new Event(LOCAL_EVENT));
        },
        [key, serialize],
    );

    return [value, setValue];
}
