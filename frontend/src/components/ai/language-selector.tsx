"use client";

/**
 * Language control for the AI workspace.
 *
 * The brief asks for three behaviours and this component provides exactly
 * those: auto-detect by default, allow a manual override, and remember the
 * choice for future sessions.
 *
 * "Auto" is the default and is deliberately the first option. A selector that
 * defaults to a named language would defeat the point — the platform detects
 * the language of every question, and forcing a choice up front is the
 * friction the brief explicitly removes.
 *
 * The preference is persisted through `PUT /auth/me/language`, which writes to
 * the existing `users.preferences` JSON column, and mirrored into
 * `localStorage` so the control renders correctly on first paint rather than
 * flickering from Auto to the saved value once the session request resolves.
 */

import { aiApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Globe, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

const STORAGE_KEY = "ierp.ai.language";

export interface LanguageSelectorProps {
  value: string;
  onChange: (language: string) => void;
  /** Language the backend actually detected on the last response. */
  detected?: string | null;
  /** Whether the last response was genuinely translated. */
  translated?: boolean | null;
  /** Explanation when a response could not be rendered in the target. */
  note?: string | null;
  className?: string;
}

/** Read the remembered language before first paint. */
export function storedLanguage(): string {
  if (typeof window === "undefined") return "auto";
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "auto";
  } catch {
    // Private browsing throws on localStorage access. Auto is a safe default,
    // and the server-side preference still applies.
    return "auto";
  }
}

export function LanguageSelector({
  value, onChange, detected, translated, note, className,
}: LanguageSelectorProps) {
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);

  const registry = useQuery({
    queryKey: ["ai-languages"],
    queryFn: () => aiApi.languages(),
    staleTime: 60 * 60 * 1000, // the registry changes on deploy, not on use
  });

  const save = useMutation({
    mutationFn: (language: string) => aiApi.saveLanguage(language),
  });

  useEffect(() => {
    const onDocumentClick = (event: MouseEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocumentClick);
    return () => document.removeEventListener("mousedown", onDocumentClick);
  }, []);

  const choose = (language: string) => {
    onChange(language);
    setOpen(false);
    try {
      window.localStorage.setItem(STORAGE_KEY, language);
    } catch {
      /* non-fatal: the server-side preference is the durable copy */
    }
    // Fire and forget. A failed preference write must not block the chat —
    // the language still applies to this session.
    save.mutate(language);
  };

  const supported = (registry.data?.languages ?? []).filter(
    (l) => l.status === "supported",
  );
  const planned = (registry.data?.languages ?? []).filter(
    (l) => l.status === "planned",
  );

  const active = supported.find((l) => l.code === value);
  const label = value === "auto" ? "Auto" : active?.native_label ?? value;

  return (
    <div ref={wrapper} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Response language"
        className={cn(
          "flex min-h-[44px] items-center gap-2 rounded-lg border",
          "border-slate-700 bg-slate-900/60 px-3 py-2 text-sm text-slate-200",
          "transition hover:border-slate-600 hover:bg-slate-800/60",
        )}
      >
        {save.isPending
          ? <Loader2 className="h-4 w-4 shrink-0 animate-spin text-slate-400" />
          : <Globe className="h-4 w-4 shrink-0 text-slate-400" />}
        <span className="truncate">{label}</span>
        {value === "auto" && detected && (
          <span className="hidden truncate text-xs text-slate-500 sm:inline">
            · detected {detected}
          </span>
        )}
      </button>

      {open && (
        <div
          role="listbox"
          className={cn(
            "absolute right-0 z-50 mt-2 w-64 overflow-hidden rounded-xl",
            "border border-slate-700 bg-slate-900 shadow-2xl",
          )}
        >
          <Option
            code="auto"
            label="Auto-detect"
            hint="Answer in whatever language you write in"
            selected={value === "auto"}
            onSelect={choose}
          />

          <div className="border-t border-slate-800" />

          {supported.map((language) => (
            <Option
              key={language.code}
              code={language.code}
              label={language.native_label}
              hint={language.label}
              selected={value === language.code}
              onSelect={choose}
            />
          ))}

          {planned.length > 0 && (
            <>
              <div className="border-t border-slate-800 px-3 pb-1 pt-2 text-[11px] uppercase tracking-wide text-slate-500">
                Coming soon
              </div>
              {planned.map((language) => (
                <div
                  key={language.code}
                  className="flex items-center justify-between px-3 py-2 text-sm text-slate-600"
                  title={`${language.label} is declared in the architecture; a translation module enables it.`}
                >
                  <span>{language.native_label}</span>
                  <span className="text-[11px] text-slate-700">{language.label}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}

      {/* Stated plainly when the answer came back in English anyway. Silently
          serving English to someone who asked for Hindi is the failure mode
          most likely to erode trust in the feature. */}
      {translated === false && note && (
        <p className="mt-1 max-w-xs text-xs text-amber-400/80">{note}</p>
      )}
    </div>
  );
}

function Option({
  code, label, hint, selected, onSelect,
}: {
  code: string; label: string; hint: string; selected: boolean;
  onSelect: (code: string) => void;
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      onClick={() => onSelect(code)}
      className={cn(
        "flex w-full min-h-[44px] items-center justify-between gap-3 px-3 py-2",
        "text-left text-sm transition",
        selected
          ? "bg-sky-500/10 text-sky-300"
          : "text-slate-200 hover:bg-slate-800/70",
      )}
    >
      <span className="min-w-0">
        <span className="block truncate">{label}</span>
        <span className="block truncate text-xs text-slate-500">{hint}</span>
      </span>
      {selected && <Check className="h-4 w-4 shrink-0" />}
    </button>
  );
}
