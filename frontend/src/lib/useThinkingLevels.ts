// The effort levels the proxy offers (its THINKING_LEVELS setting), shared by every policy editor. Fetched when an
// editor mounts and the last fetch is older than STALE_MS, so a change on the Settings page shows up without a
// reload. Until the first request settles, and if it fails, the built-in default list is used.

import { useSyncExternalStore } from "react";
import { api } from "@/lib/api";
import { DEFAULT_THINKING_LEVELS, ThinkingLevel } from "@/lib/types";

const STALE_MS = 15_000;
let levels: ThinkingLevel[] = DEFAULT_THINKING_LEVELS;
let requested = false;
let fetchedAt = 0;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    if (!requested && Date.now() - fetchedAt > STALE_MS) {
        requested = true;
        api.thinkingLevels()
            .then((next) => {
                requested = false;
                fetchedAt = Date.now();
                if (next.length > 0 && next.join(",") !== levels.join(",")) {
                    levels = next;
                    listeners.forEach((notify) => notify());
                }
            })
            .catch(() => {
                // Keep the defaults; allow a later mount to retry.
                requested = false;
            });
    }
    return () => listeners.delete(listener);
}

export function useThinkingLevels(): ThinkingLevel[] {
    return useSyncExternalStore(
        subscribe,
        () => levels,
        () => DEFAULT_THINKING_LEVELS,
    );
}
