"use client";

// ---------------------------------------------------------------------------
// Client-side auth gate. Redirects to /login when no token is present.
// Wraps the authenticated dashboard area.
// ---------------------------------------------------------------------------

import { useEffect, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/api";
import { LoadingState } from "./ui";

// Tokens live in localStorage; re-check when another tab signs in or out.
function subscribe(onChange: () => void) {
    window.addEventListener("storage", onChange);
    return () => window.removeEventListener("storage", onChange);
}

const hasToken = () => Boolean(getToken());
// During server rendering the token is unknown, so render the loading state.
const unknownOnServer = () => null;

export function AuthGuard({ children }: { children: React.ReactNode }) {
    const router = useRouter();
    const signedIn = useSyncExternalStore(subscribe, hasToken, unknownOnServer);

    useEffect(() => {
        if (signedIn === false) router.replace("/login");
    }, [router, signedIn]);

    // Never flash protected content before the token check has run.
    if (!signedIn) {
        return (
            <div className="flex min-h-screen items-center justify-center">
                <LoadingState label="Checking session…" />
            </div>
        );
    }

    return <>{children}</>;
}
