import { useCallback, useEffect, useState } from 'react';
import { readJson, writeJson } from '../../lib/storage';

/* ─────────────────────────────────────────────────────────────
   Мелкие хуки плеера: настройки с запоминанием, reduced-motion,
   Wake Lock и блокировка прокрутки под полноэкранным слоем.
   ───────────────────────────────────────────────────────────── */

export interface PracticePrefs {
  sound: boolean;
  vibration: boolean;
  voice: boolean;
}

const PREFS_KEY = 'lifeos.practice.prefs';
const PREFS_DEFAULT: PracticePrefs = { sound: true, vibration: true, voice: false };

export function usePracticePrefs(): {
  prefs: PracticePrefs;
  setPref: (key: keyof PracticePrefs, value: boolean) => void;
} {
  const [prefs, setPrefs] = useState<PracticePrefs>(() => {
    const stored = readJson<Partial<PracticePrefs>>(PREFS_KEY, {});
    return {
      sound: typeof stored.sound === 'boolean' ? stored.sound : PREFS_DEFAULT.sound,
      vibration: typeof stored.vibration === 'boolean' ? stored.vibration : PREFS_DEFAULT.vibration,
      voice: typeof stored.voice === 'boolean' ? stored.voice : PREFS_DEFAULT.voice,
    };
  });

  const setPref = useCallback((key: keyof PracticePrefs, value: boolean) => {
    setPrefs((current) => {
      const next = { ...current, [key]: value };
      writeJson(PREFS_KEY, next);
      return next;
    });
  }, []);

  return { prefs, setPref };
}

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = () => setReduced(query.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  return reduced;
}

interface WakeLockSentinelLike {
  release: () => Promise<void>;
}

type WakeLockNavigator = Navigator & {
  wakeLock?: { request: (type: 'screen') => Promise<WakeLockSentinelLike> };
};

/** Держим экран включённым, пока идёт практика. Отказ — молча продолжаем. */
export function useWakeLock(active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const scope = navigator as WakeLockNavigator;
    if (!scope.wakeLock) return undefined;

    let sentinel: WakeLockSentinelLike | null = null;
    let disposed = false;

    const request = () => {
      if (disposed || sentinel) return;
      scope
        .wakeLock?.request('screen')
        .then((lock) => {
          if (disposed) {
            void lock.release().catch(() => undefined);
            return;
          }
          sentinel = lock;
        })
        .catch(() => undefined);
    };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') request();
    };

    request();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      disposed = true;
      document.removeEventListener('visibilitychange', onVisibility);
      const lock = sentinel;
      sentinel = null;
      void lock?.release().catch(() => undefined);
    };
  }, [active]);
}

/** Пока открыт полноэкранный плеер, страница под ним не прокручивается. */
export function useScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [active]);
}
