import { useSyncExternalStore } from 'react';

export type ToastTone = 'default' | 'success' | 'error';

export interface ToastItem {
  id: number;
  text: string;
  tone: ToastTone;
}

let items: ToastItem[] = [];
let sequence = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): ToastItem[] {
  return items;
}

function dismiss(id: number): void {
  const next = items.filter((item) => item.id !== id);
  if (next.length === items.length) return;
  items = next;
  emit();
}

/** Показать короткое уведомление. Без alert/confirm — только собственный тост. */
export function toast(text: string, tone: ToastTone = 'default'): void {
  const id = ++sequence;
  items = [...items.slice(-2), { id, text, tone }];
  emit();
  window.setTimeout(() => dismiss(id), 3200);
}

export function useToasts(): ToastItem[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
