"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, type LucideIcon } from "lucide-react";

type EventTypeFilterProps = {
    value: string;
    options: string[];
    onChange: (value: string) => void;
    label?: string;
    allLabel?: string;
    idPrefix?: string;
    compact?: boolean;
    showArrow?: boolean;
    icon?: LucideIcon;
};

export function EventTypeFilter({
    value,
    options,
    onChange,
    label = "Event type",
    allLabel = "All events",
    idPrefix = "event-type-options",
    compact = false,
    showArrow = true,
    icon: Icon,
}: EventTypeFilterProps) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const selectedLabel = value || allLabel;

    useEffect(() => {
        if (!open) return;
        const closeOnOutsideClick = (event: MouseEvent) => {
            if (!ref.current?.contains(event.target as Node)) setOpen(false);
        };
        const closeOnEscape = (event: KeyboardEvent) => {
            if (event.key === "Escape") setOpen(false);
        };
        document.addEventListener("mousedown", closeOnOutsideClick);
        document.addEventListener("keydown", closeOnEscape);
        return () => {
            document.removeEventListener("mousedown", closeOnOutsideClick);
            document.removeEventListener("keydown", closeOnEscape);
        };
    }, [open]);

    return (
        <div ref={ref} className={`relative ${compact ? "" : "min-w-[220px]"}`}>
            {!compact ? <span className="text-fog-400 block text-xs">{label}</span> : null}
            <button
                type="button"
                role="combobox"
                aria-haspopup="listbox"
                aria-expanded={open}
                aria-controls={idPrefix}
                aria-label={compact ? label : undefined}
                onClick={() => setOpen((current) => !current)}
                className={
                    compact
                        ? "border-ink-700 bg-ink-900 text-fog-100 hover:bg-ink-850 inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-sm font-medium"
                        : "border-ink-600 bg-ink-800 text-fog-100 focus:border-brand-400 focus:ring-brand-500/25 mt-1 flex w-full items-center justify-between rounded-lg border px-3 py-2 text-sm transition outline-none focus:ring-2"
                }
            >
                {Icon ? <Icon size={14} className="text-fog-400 shrink-0" /> : null}
                <span className={compact ? "max-w-[14rem] truncate" : "truncate"}>
                    {selectedLabel}
                </span>
                {showArrow ? (
                    <ChevronDown
                        size={16}
                        className={`text-fog-400 ml-3 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
                    />
                ) : null}
            </button>
            {open ? (
                <div
                    id={idPrefix}
                    role="listbox"
                    className="border-ink-600 bg-ink-850 fixed inset-x-3 top-3 z-50 max-h-[calc(100dvh-1.5rem)] overflow-y-auto rounded-xl border py-1 shadow-[var(--shadow-pop)] sm:absolute sm:inset-x-auto sm:top-full sm:right-0 sm:mt-2 sm:max-h-80 sm:min-w-full"
                >
                    {["", ...options].map((option) => {
                        const selected = option === value;
                        return (
                            <button
                                key={option || "all"}
                                type="button"
                                role="option"
                                aria-selected={selected}
                                onClick={() => {
                                    onChange(option);
                                    setOpen(false);
                                }}
                                className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors ${selected ? "bg-ink-700 text-fog-100 font-medium" : "text-fog-200 hover:bg-ink-800"}`}
                            >
                                {selected ? (
                                    <Check size={14} className="text-brand-300 shrink-0" />
                                ) : (
                                    <span className="w-3.5 shrink-0" />
                                )}
                                <span className="break-words">{option || allLabel}</span>
                            </button>
                        );
                    })}
                </div>
            ) : null}
        </div>
    );
}
