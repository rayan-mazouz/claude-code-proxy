"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { DashboardMember } from "@/lib/types";
import {
    Button,
    Card,
    ErrorState,
    Field,
    LoadingState,
    StatusToggle,
    TextInput,
} from "@/components/ui";

type PermissionGroup = { label: string; permissions: string[] };

const PERMISSION_GROUPS: PermissionGroup[] = [
    { label: "Analytics", permissions: ["analytics:read"] },
    { label: "Events", permissions: ["analytics:events:archive:read"] },
    {
        label: "Accounts",
        permissions: ["accounts:read", "accounts:write", "accounts:delete"],
    },
    {
        label: "Proxy users",
        permissions: ["proxy_users:read", "proxy_users:write", "proxy_users:delete"],
    },
    {
        label: "API keys",
        permissions: ["api_keys:read", "api_keys:write", "api_keys:delete"],
    },
    {
        label: "Fallbacks",
        permissions: ["fallbacks:read", "fallbacks:write", "fallbacks:delete"],
    },
    {
        label: "Notifications",
        permissions: ["notifications:read", "notifications:write"],
    },
    {
        label: "Team members",
        permissions: ["team:members:read", "team:members:write", "team:members:delete"],
    },
] as const;

const PERMISSION_ORDER = new Map<string, number>(
    PERMISSION_GROUPS.flatMap((group, groupIndex) =>
        group.permissions.map((permission, permissionIndex) => [
            permission,
            groupIndex * 100 + permissionIndex,
        ]),
    ),
);

function sortPermissions(permissions: string[]) {
    return [...permissions].sort(
        (a, b) =>
            (PERMISSION_ORDER.get(a) ?? Number.MAX_SAFE_INTEGER) -
                (PERMISSION_ORDER.get(b) ?? Number.MAX_SAFE_INTEGER) || a.localeCompare(b),
    );
}

function PermissionChecks({
    available,
    selected,
    onChange,
}: {
    available: string[];
    selected: string[];
    onChange: (next: string[]) => void;
}) {
    const selectedSet = new Set(selected);
    const toggle = (permission: string) => {
        const next = new Set(selectedSet);
        if (next.has(permission)) next.delete(permission);
        else next.add(permission);
        onChange(available.filter((item) => next.has(item)));
    };
    const knownPermissions = new Set<string>();
    const groups = PERMISSION_GROUPS.map((group) => {
        const permissions = group.permissions.filter((permission) => {
            knownPermissions.add(permission);
            return available.includes(permission);
        });
        return { ...group, permissions };
    }).filter((group) => group.permissions.length > 0);
    const otherPermissions = sortPermissions(
        available.filter((permission) => !knownPermissions.has(permission)),
    );
    if (otherPermissions.length > 0) groups.push({ label: "Other", permissions: otherPermissions });

    return (
        <div className="space-y-4">
            {groups.map((group) => (
                <fieldset key={group.label}>
                    <legend className="text-fog-400 mb-2 text-[11px] font-semibold tracking-wider uppercase">
                        {group.label}
                    </legend>
                    <div className="grid gap-2 sm:grid-cols-2">
                        {group.permissions.map((permission) => (
                            <label
                                key={permission}
                                className="border-ink-700 bg-ink-900/60 text-fog-200 hover:border-brand-400/60 flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-xs"
                            >
                                <input
                                    type="checkbox"
                                    checked={selectedSet.has(permission)}
                                    onChange={() => toggle(permission)}
                                    className="accent-brand-500 h-4 w-4"
                                />
                                <span className="font-mono break-all">{permission}</span>
                            </label>
                        ))}
                    </div>
                </fieldset>
            ))}
        </div>
    );
}

export default function TeamPage() {
    const [members, setMembers] = useState<DashboardMember[]>([]);
    const [availablePermissions, setAvailablePermissions] = useState<string[]>([]);
    const [selectedPermissions, setSelectedPermissions] = useState<string[]>([]);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editingPermissions, setEditingPermissions] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [username, setUsername] = useState("");
    const [password, setPassword] = useState("");

    const load = useCallback(
        () =>
            Promise.all([api.teamMembers(), api.teamPermissions()]).then(
                ([memberResponse, permissionResponse]) => {
                    setError(null);
                    setMembers(memberResponse.members);
                    setAvailablePermissions(permissionResponse.permissions);
                    setLoading(false);
                },
                (err: unknown) => {
                    setError(err instanceof Error ? err.message : "Unable to load team members.");
                    setLoading(false);
                },
            ),
        [],
    );

    useEffect(() => {
        void load();
    }, [load]);

    const create = async (event: FormEvent) => {
        event.preventDefault();
        if (!selectedPermissions.length) {
            setError("Select at least one permission before creating a member.");
            return;
        }
        setSaving(true);
        setError(null);
        try {
            await api.createTeamMember({ username, password, permissions: selectedPermissions });
            setUsername("");
            setPassword("");
            setSelectedPermissions([]);
            await load();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Unable to create team member.");
        } finally {
            setSaving(false);
        }
    };

    const toggle = async (member: DashboardMember) => {
        try {
            const result = await api.updateTeamMember(member.id, { active: !member.active });
            setMembers((current) =>
                current.map((item) => (item.id === member.id ? result.member : item)),
            );
        } catch (err) {
            setError(err instanceof Error ? err.message : "Unable to update team member.");
        }
    };

    const savePermissions = async (member: DashboardMember) => {
        if (!editingPermissions.length) {
            setError("A member must retain at least one permission.");
            return;
        }
        try {
            const result = await api.updateTeamMember(member.id, {
                permissions: editingPermissions,
            });
            setMembers((current) =>
                current.map((item) => (item.id === member.id ? result.member : item)),
            );
            setEditingId(null);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Unable to update permissions.");
        }
    };

    const permissionCountLabel = useMemo(
        () => (count: number) => `${count} permission${count === 1 ? "" : "s"}`,
        [],
    );

    return (
        <div className="space-y-5">
            <header>
                <h1 className="text-fog-100 text-2xl font-semibold tracking-tight">Team access</h1>
                <p className="text-fog-400 mt-0.5 text-sm">
                    Create members and grant exact dashboard permissions. No display names or role
                    presets are required.
                </p>
            </header>
            {error ? (
                <ErrorState
                    message={error}
                    onRetry={() => {
                        setLoading(true);
                        setError(null);
                        void load();
                    }}
                />
            ) : null}
            <Card className="p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                        <h2 className="text-fog-100 text-base font-semibold">Add team member</h2>
                        <p className="text-fog-400 mt-1 text-xs">
                            Choose the exact permission strings this account may use.
                        </p>
                    </div>
                    <span className="text-fog-400 text-xs">
                        {permissionCountLabel(selectedPermissions.length)} selected
                    </span>
                </div>
                <form onSubmit={create} className="mt-4 space-y-4">
                    <div className="grid gap-4 md:grid-cols-2">
                        <Field label="Username">
                            <TextInput
                                value={username}
                                onChange={(event) => setUsername(event.target.value)}
                                required
                            />
                        </Field>
                        <Field label="Password">
                            <TextInput
                                type="password"
                                minLength={12}
                                value={password}
                                onChange={(event) => setPassword(event.target.value)}
                                required
                            />
                        </Field>
                    </div>
                    <Field label="Permissions">
                        <PermissionChecks
                            available={availablePermissions}
                            selected={selectedPermissions}
                            onChange={setSelectedPermissions}
                        />
                    </Field>
                    <Button type="submit" variant="primary" disabled={saving}>
                        {saving ? "Creating…" : "Create member"}
                    </Button>
                </form>
            </Card>
            {loading ? (
                <Card>
                    <LoadingState />
                </Card>
            ) : (
                <Card>
                    <div className="border-ink-700 text-fog-400 flex items-center justify-between border-b px-5 py-3 text-xs font-medium tracking-wider uppercase">
                        <span>Dashboard members</span>
                        <span>{members.length} members</span>
                    </div>
                    <div className="divide-ink-800 divide-y">
                        {members.length === 0 ? (
                            <div className="text-fog-400 p-5 text-sm">
                                No database-backed members yet.
                            </div>
                        ) : (
                            members.map((member) => (
                                <div key={member.id} className="space-y-3 px-5 py-4">
                                    <div className="flex flex-wrap items-center justify-between gap-3">
                                        <div>
                                            <div className="text-fog-100 font-medium">
                                                {member.username}
                                            </div>
                                            <div className="text-fog-400 text-xs">
                                                {permissionCountLabel(member.permissions.length)}
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-3">
                                            <StatusToggle
                                                on={member.active}
                                                onClick={() => void toggle(member)}
                                                srLabel={`Toggle ${member.username}`}
                                            />
                                            <Button
                                                variant="ghost"
                                                onClick={() => {
                                                    setEditingId(member.id);
                                                    setEditingPermissions(member.permissions);
                                                }}
                                            >
                                                Edit permissions
                                            </Button>
                                            <Button
                                                variant="danger"
                                                onClick={() => {
                                                    if (
                                                        !window.confirm(
                                                            `Delete ${member.username}?`,
                                                        )
                                                    )
                                                        return;
                                                    void api
                                                        .deleteTeamMember(member.id)
                                                        .then(() =>
                                                            setMembers((current) =>
                                                                current.filter(
                                                                    (item) => item.id !== member.id,
                                                                ),
                                                            ),
                                                        )
                                                        .catch((err) =>
                                                            setError(
                                                                err instanceof Error
                                                                    ? err.message
                                                                    : "Unable to delete team member.",
                                                            ),
                                                        );
                                                }}
                                            >
                                                Delete
                                            </Button>
                                        </div>
                                    </div>
                                    {editingId === member.id ? (
                                        <div className="border-ink-700 bg-ink-900/40 space-y-3 rounded-lg border p-3">
                                            <PermissionChecks
                                                available={availablePermissions}
                                                selected={editingPermissions}
                                                onChange={setEditingPermissions}
                                            />
                                            <div className="flex justify-end gap-2">
                                                <Button
                                                    variant="ghost"
                                                    onClick={() => setEditingId(null)}
                                                >
                                                    Cancel
                                                </Button>
                                                <Button
                                                    variant="primary"
                                                    onClick={() => void savePermissions(member)}
                                                >
                                                    Save permissions
                                                </Button>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="text-fog-400 flex flex-wrap gap-1.5 text-xs">
                                            {sortPermissions(member.permissions).map(
                                                (permission) => (
                                                    <span
                                                        key={permission}
                                                        className="border-ink-700 bg-ink-900 rounded border px-2 py-1 font-mono"
                                                    >
                                                        {permission}
                                                    </span>
                                                ),
                                            )}
                                        </div>
                                    )}
                                </div>
                            ))
                        )}
                    </div>
                </Card>
            )}
        </div>
    );
}
