import { useCallback, useEffect, useState } from 'react';
import { STORAGE_KEYS, readRaw, writeRaw } from './storage';

export type ThemeMode = 'light' | 'dark' | 'auto';
export type ResolvedTheme = 'light' | 'dark';

const MODES: readonly ThemeMode[] = ['light', 'dark', 'auto'];

export function isThemeMode(value: unknown): value is ThemeMode {
  return typeof value === 'string' && (MODES as readonly string[]).includes(value);
}

export function readStoredTheme(): ThemeMode {
  const raw = readRaw(STORAGE_KEYS.theme);
  return isThemeMode(raw) ? raw : 'auto';
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : false;
}

function resolveTheme(mode: ThemeMode, prefersDark: boolean): ResolvedTheme {
  if (mode === 'auto') return prefersDark ? 'dark' : 'light';
  return mode;
}

export interface ThemeController {
  mode: ThemeMode;
  resolved: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
  cycleMode: () => void;
}

/** Тема: light | dark | auto. Авто следит за системной настройкой в реальном времени. */
export function useTheme(): ThemeController {
  const [mode, setModeState] = useState<ThemeMode>(() => readStoredTheme());
  const [prefersDark, setPrefersDark] = useState<boolean>(() => systemPrefersDark());
  const resolved = resolveTheme(mode, prefersDark);

  useEffect(() => {
    if (!window.matchMedia) return undefined;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (event: MediaQueryListEvent) => setPrefersDark(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    writeRaw(STORAGE_KEYS.theme, mode);
  }, [mode, resolved]);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
  }, []);

  const cycleMode = useCallback(() => {
    setModeState((previous) => {
      const index = MODES.indexOf(previous);
      return MODES[(index + 1) % MODES.length];
    });
  }, []);

  return { mode, resolved, setMode, cycleMode };
}

export const THEME_LABELS: Record<ThemeMode, string> = {
  light: 'Светлая',
  dark: 'Тёмная',
  auto: 'Авто',
};
