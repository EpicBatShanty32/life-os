/** Безопасная обёртка над localStorage: приватный режим и квоты не должны ронять UI. */

const memory = new Map<string, string>();

export function readRaw(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return memory.get(key) ?? null;
  }
}

export function writeRaw(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    memory.set(key, value);
  }
}

export function removeRaw(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    memory.delete(key);
  }
}

export function readJson<T>(key: string, fallback: T): T {
  const raw = readRaw(key);
  if (raw === null) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    writeRaw(key, JSON.stringify(value));
  } catch {
    /* значение не сериализуется — молча игнорируем */
  }
}

export function readFlag(key: string, fallback = false): boolean {
  const raw = readRaw(key);
  if (raw === null) return fallback;
  return raw === '1' || raw === 'true';
}

export function writeFlag(key: string, value: boolean): void {
  writeRaw(key, value ? '1' : '0');
}

export function readNumber(key: string, fallback = 0): number {
  const raw = readRaw(key);
  if (raw === null) return fallback;
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const STORAGE_KEYS = {
  sidebarCollapsed: 'lifeos.sidebar.collapsed',
  theme: 'lifeos.theme',
  token: 'lifeos.token',
  waterExtra: (date: string) => `lifeos.demo.water.${date}`,
  localQuick: 'lifeos.demo.quick-entries',
} as const;
