"use client";

// ---------------------------------------------------------------------------
// Responsive shell for the authenticated dashboard.
//   - md and up: fixed left sidebar, content offset by its width.
//   - below md: a sticky top bar with a hamburger that opens a slide-in drawer.
// ---------------------------------------------------------------------------

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { Menu, X } from "lucide-react";
import { api } from "@/lib/api";
import { BrandMark, NavLinks, SignOutButton } from "./Sidebar";

export function AppShell({ children }: { children: React.ReactNode }) {
    const [permissions, setPermissions] = useState<string[] | null>(null);
    const pathname = usePathname();
    // The drawer remembers the route it was opened on, so any navigation (links,
    // back/forward) closes it without an extra effect.
    const [openOn, setOpenOn] = useState<string | null>(null);
    const open = openOn === pathname;
    const setOpen = (next: boolean) => setOpenOn(next ? pathname : null);

    useEffect(() => {
        api.authProfile()
            .then((profile) => setPermissions(profile.permissions))
            .catch(() => setPermissions([]));
    }, []);

    // Close on Escape and lock body scroll while the drawer is open.
    useEffect(() => {
        if (!open) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === "Escape") setOpenOn(null);
        };
        window.addEventListener("keydown", onKey);
        document.body.style.overflow = "hidden";
        return () => {
            window.removeEventListener("keydown", onKey);
            document.body.style.overflow = "";
        };
    }, [open]);

    return (
        <div className="min-h-screen">
            {/* Desktop rail */}
            <aside className="border-ink-700 bg-ink-900 fixed inset-y-0 left-0 hidden w-60 flex-col border-r md:flex">
                <div className="px-5 py-5">
                    <BrandMark />
                </div>
                <div className="flex-1 px-3 py-2">
                    <NavLinks permissions={permissions} />
                </div>
                <div className="border-ink-700 border-t p-3">
                    <SignOutButton />
                </div>
            </aside>

            {/* Mobile top bar */}
            <header className="border-ink-700 bg-ink-900/90 sticky top-0 z-30 flex items-center justify-between border-b px-4 py-3 backdrop-blur md:hidden">
                <BrandMark />
                <button
                    onClick={() => setOpen(true)}
                    aria-label="Open navigation"
                    className="text-fog-300 hover:bg-ink-800 hover:text-fog-100 -mr-1 rounded-lg p-2 transition-colors"
                >
                    <Menu size={22} strokeWidth={1.75} />
                </button>
            </header>

            {/* Mobile drawer */}
            {open ? (
                <div className="fixed inset-0 z-50 md:hidden">
                    <div
                        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
                        onClick={() => setOpen(false)}
                        aria-hidden
                    />
                    <div className="border-ink-700 bg-ink-900 absolute inset-y-0 left-0 flex w-72 max-w-[80%] flex-col border-r shadow-[var(--shadow-pop)]">
                        <div className="flex items-center justify-between px-5 py-4">
                            <BrandMark />
                            <button
                                onClick={() => setOpen(false)}
                                aria-label="Close navigation"
                                className="text-fog-400 hover:bg-ink-800 hover:text-fog-100 rounded-lg p-1.5 transition-colors"
                            >
                                <X size={20} strokeWidth={1.75} />
                            </button>
                        </div>
                        <div className="flex-1 px-3 py-2">
                            <NavLinks onNavigate={() => setOpen(false)} permissions={permissions} />
                        </div>
                        <div className="border-ink-700 border-t p-3">
                            <SignOutButton />
                        </div>
                    </div>
                </div>
            ) : null}

            {/* Content */}
            <main className="md:ml-60">
                <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
                    {children}
                </div>
            </main>
        </div>
    );
}
