// ---------------------------------------------------------------------------
// Typed client for the owner-only Settings API (/v1/settings), which reads and
// rewrites the deployment's .env config file.
// ---------------------------------------------------------------------------

import { API_BASE_URL, ApiError, clearToken, getToken, request } from "./api";

export type SettingApply = "live" | "restart" | "redeploy";

export type SettingType =
    "string" | "int" | "float" | "bool" | "json" | "list" | "choice" | "secret";

export interface SettingEntry {
    key: string;
    group: string;
    label: string;
    help: string;
    type: SettingType;
    apply: SettingApply;
    choices: string[];
    read_only: boolean;
    secret: boolean;
    minimum: number | null;
    maximum: number | null;
    /** Current value as text; always null for secrets. */
    value: string | null;
    is_set: boolean;
    source: "file" | "environment" | "default";
    /** Changing it needs the current owner password. */
    requires_password: boolean;
    /** An optional secret that may be cleared by saving an empty value. */
    clearable: boolean;
    /** Also read by the egress relay, which only reads it when it starts. */
    relay_restart: boolean;
}

export interface SettingsState {
    path: string;
    exists: boolean;
    readable: boolean;
    writable: boolean;
    groups: string[];
    settings: SettingEntry[];
    pending_restart: string[];
    pending_redeploy: string[];
    /** Relay keys saved with a value the running egress relay may not have yet. */
    pending_relay: string[];
    relay_restart_command: string;
    restart_supported: boolean;
}

/** A 422 from PUT /v1/settings, with one message per rejected key. */
export class SettingsValidationError extends ApiError {
    errors: Record<string, string>;

    constructor(errors: Record<string, string>) {
        const count = Object.keys(errors).length;
        super(`${count} setting${count === 1 ? "" : "s"} could not be saved.`, 422);
        this.name = "SettingsValidationError";
        this.errors = errors;
    }
}

function fieldErrors(data: unknown): Record<string, string> | null {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
    if (!Array.isArray(detail)) return null;
    const errors: Record<string, string> = {};
    for (const item of detail) {
        if (!item || typeof item !== "object") continue;
        const { loc, msg } = item as { loc?: unknown; msg?: unknown };
        const key = Array.isArray(loc) && loc.length ? String(loc[loc.length - 1]) : "_form";
        if (!(key in errors)) errors[key] = String(msg ?? "Invalid value");
    }
    return Object.keys(errors).length ? errors : null;
}

// PUT needs the structured 422 body (per-key messages), which the shared
// `request` helper flattens into one string, so it calls fetch directly with
// the same token and 401 handling. `currentPassword` is required (403
// otherwise) when a change touches a key with `requires_password`.
async function saveSettings(
    values: Record<string, string>,
    currentPassword?: string,
): Promise<SettingsState> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const res = await fetch(`${API_BASE_URL}/v1/settings`, {
        method: "PUT",
        headers,
        body: JSON.stringify(
            currentPassword ? { values, current_password: currentPassword } : { values },
        ),
        cache: "no-store",
    });

    if (res.status === 401) {
        clearToken();
        if (typeof window !== "undefined") {
            window.location.replace(new URL("/login", window.location.origin));
        }
        throw new ApiError("Session expired. Please sign in again.", 401);
    }

    let data: unknown = null;
    const text = await res.text();
    if (text) {
        try {
            data = JSON.parse(text);
        } catch {
            data = text;
        }
    }

    if (res.status === 422) {
        const errors = fieldErrors(data);
        if (errors) throw new SettingsValidationError(errors);
    }
    if (!res.ok) {
        const detail =
            data && typeof data === "object" && "detail" in data && typeof data.detail === "string"
                ? data.detail
                : `Request failed (${res.status})`;
        throw new ApiError(detail, res.status);
    }
    return data as SettingsState;
}

export const settingsApi = {
    get() {
        return request<SettingsState>("/v1/settings");
    },
    save: saveSettings,
    restartBackend() {
        return request<{ status: "restarting" }>("/v1/settings/restart", { method: "POST" });
    },
};
