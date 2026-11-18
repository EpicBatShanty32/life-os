import type { PhaseKind, Practice, PracticePhase, PracticeRound } from '../../api/practices';

/* ─────────────────────────────────────────────────────────────
   Сценарий практики → плоская шкала шагов.

   Всё считается от настенного времени: шаги знают только смещения
   в миллисекундах, поэтому пауза, пропуск раунда и возврат из
   погашенного экрана не «убегают» — мы каждый кадр заново смотрим,
   какой шаг соответствует прошедшему времени.
   ───────────────────────────────────────────────────────────── */

export interface PracticeStep {
  index: number;
  roundIndex: number;
  kind: PhaseKind;
  label: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  /** Один вдох-выдох внутри `pulse`. */
  cycleMs: number | null;
  cycles: number | null;
  /** Сколько секунд длится задержка — для личного рекорда. */
  holdSeconds: number | null;
}

/** Минимальный масштаб круга: на выдохе он не схлопывается в точку. */
export const SCALE_MIN = 0.62;

export function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

export function phaseWord(kind: PhaseKind, label?: string): string {
  if (label) return label;
  switch (kind) {
    case 'inhale':
      return 'Вдох';
    case 'exhale':
      return 'Выдох';
    case 'hold':
      return 'Задержка';
    case 'holdOut':
      return 'Задержка на выдохе';
    case 'rest':
      return 'Восстановительный вдох';
    case 'pulse':
      return 'Вдох-выдох';
    case 'pose':
      return 'Держим';
    default:
      return 'Дыхание';
  }
}

/** 90 → «1:30», 45 → «45», 240 → «4:00». */
export function formatClock(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  if (safe < 60) return String(safe);
  const minutes = Math.floor(safe / 60);
  return `${minutes}:${String(safe % 60).padStart(2, '0')}`;
}

/** Длительность в тексте плана: «45 с», «1:30» — иначе «15» читается как счёт. */
export function formatSpan(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  return safe < 60 ? `${safe} с` : formatClock(safe);
}

export function buildSteps(practice: Practice): PracticeStep[] {
  const steps: PracticeStep[] = [];
  let cursor = 0;

  practice.rounds.forEach((round, roundIndex) => {
    round.phases.forEach((phase) => {
      const durationMs = Math.max(1, Math.round(phase.seconds * 1000));
      const cycles = phase.kind === 'pulse' && phase.cycles ? Math.max(1, phase.cycles) : null;
      steps.push({
        index: steps.length,
        roundIndex,
        kind: phase.kind,
        label: phaseWord(phase.kind, phase.label),
        startMs: cursor,
        endMs: cursor + durationMs,
        durationMs,
        cycleMs: cycles ? durationMs / cycles : null,
        cycles,
        holdSeconds:
          phase.kind === 'hold' || phase.kind === 'holdOut'
            ? Math.round(durationMs / 1000)
            : null,
      });
      cursor += durationMs;
    });
  });

  return steps;
}

/** 90 → «1:30», 30 → «30 с»: единица нужна, чтобы «рекорд 30» не читался как очки. */
export function holdLabel(seconds: number): string {
  return seconds < 60 ? `${Math.round(seconds)} с` : formatClock(seconds);
}

export interface LocatedStep {
  step: PracticeStep;
  elapsedInStep: number;
  remainingMs: number;
}

export function locate(steps: readonly PracticeStep[], elapsedMs: number): LocatedStep | null {
  if (steps.length === 0) return null;
  const last = steps[steps.length - 1];
  const clamped = Math.max(0, elapsedMs);

  if (clamped >= last.endMs) {
    return { step: last, elapsedInStep: last.durationMs, remainingMs: 0 };
  }

  for (const step of steps) {
    if (clamped < step.endMs) {
      return {
        step,
        elapsedInStep: clamped - step.startMs,
        remainingMs: step.endMs - clamped,
      };
    }
  }
  return { step: last, elapsedInStep: last.durationMs, remainingMs: 0 };
}

export interface RoundBounds {
  startMs: number;
  endMs: number;
}

export function roundBounds(
  steps: readonly PracticeStep[],
  roundIndex: number,
): RoundBounds | null {
  let startMs: number | null = null;
  let endMs: number | null = null;
  steps.forEach((step) => {
    if (step.roundIndex !== roundIndex) return;
    if (startMs === null) startMs = step.startMs;
    endMs = step.endMs;
  });
  return startMs !== null && endMs !== null ? { startMs, endMs } : null;
}

function easeInOut(p: number): number {
  return 0.5 - Math.cos(Math.PI * clamp01(p)) / 2;
}

/** Масштаб круга: только transform, никаких layout-свойств. */
export function circleScale(step: PracticeStep, progress: number): number {
  const p = clamp01(progress);
  switch (step.kind) {
    case 'inhale':
      return SCALE_MIN + (1 - SCALE_MIN) * easeInOut(p);
    case 'exhale':
      return 1 - (1 - SCALE_MIN) * easeInOut(p);
    case 'rest':
      return SCALE_MIN + (1 - SCALE_MIN) * easeInOut(Math.min(1, p / 0.5));
    case 'hold':
      return 1;
    case 'holdOut':
      return SCALE_MIN;
    case 'pulse': {
      const cycles = step.cycles && step.cycles > 0 ? step.cycles : 1;
      const phase = (p * cycles) % 1;
      const half = phase < 0.5 ? phase * 2 : (1 - phase) * 2;
      return SCALE_MIN + (1 - SCALE_MIN) * easeInOut(half);
    }
    case 'pose':
      // Поза и холод: круг стоит, время показывает кольцо — не мигаем телом.
      return 1;
    default:
      return 1;
  }
}

export interface Readout {
  value: string;
  word: string;
}

export function readout(step: PracticeStep, remainingMs: number): Readout {
  if (step.cycleMs && step.cycles) {
    const left = Math.max(1, Math.ceil(remainingMs / step.cycleMs - 0.0001));
    return { value: String(left), word: 'Вдохов осталось' };
  }
  const seconds = Math.max(0, Math.ceil(remainingMs / 1000));
  return { value: formatClock(seconds), word: step.label };
}

/** Короткие слова для плана: длинные подписи живут в плеере, не в списке. */
const PLAN_WORD: Record<PhaseKind, string> = {
  inhale: 'вдох',
  exhale: 'выдох',
  hold: 'задержка',
  holdOut: 'задержка',
  rest: 'восстановление',
  pulse: 'вдохи',
  pose: 'держим',
};

function describePhase(phase: PracticePhase): string {
  if (phase.kind === 'pulse') {
    const count = phase.cycles ?? 0;
    return `${count > 0 ? count : ''} вдохов`.trim();
  }
  // Своя подпись у поз и задержек (растяжка, боксовое дыхание), иначе — короткое слово.
  const name = phase.label ? phase.label.toLowerCase() : PLAN_WORD[phase.kind];
  return `${name} ${formatSpan(phase.seconds)}`;
}

/** Подпись раунда для экрана «перед началом». */
export function describeRound(round: PracticeRound): string {
  const phases = round.phases;
  if (phases.length === 0) return '';
  if (phases.length > 3) {
    const same = phases.every((phase) => phase.seconds === phases[0].seconds);
    const steps = `${phases.length} шагов`;
    return same ? `${steps} по ${formatClock(phases[0].seconds)}` : steps;
  }
  return phases.map(describePhase).join(' · ');
}

/** Одинаковые раунды в плане схлопываем: «Раунды 1–10» вместо десяти строк. */
export interface PlanRow {
  from: number;
  to: number;
  text: string;
}

export function planRows(rounds: readonly PracticeRound[]): PlanRow[] {
  const rows: PlanRow[] = [];
  rounds.forEach((round, index) => {
    const text = describeRound(round);
    const last = rows[rows.length - 1];
    if (last && last.text === text && last.to === index) {
      last.to = index + 1;
      return;
    }
    rows.push({ from: index + 1, to: index + 1, text });
  });
  return rows;
}

export interface PracticeSummary {
  roundsDone: number;
  bestHoldSec: number;
  elapsedSec: number;
}

/** Что засчитать, если человек закончил раньше: только полностью пройденное. */
export function summarize(steps: readonly PracticeStep[], elapsedMs: number): PracticeSummary {
  const rounds = new Map<number, RoundBounds>();
  steps.forEach((step) => {
    const bounds = rounds.get(step.roundIndex);
    if (!bounds) {
      rounds.set(step.roundIndex, { startMs: step.startMs, endMs: step.endMs });
    } else {
      bounds.endMs = step.endMs;
    }
  });

  let roundsDone = 0;
  rounds.forEach((bounds) => {
    if (elapsedMs >= bounds.endMs - 1) roundsDone += 1;
  });

  let bestHoldSec = 0;
  steps.forEach((step) => {
    if (step.holdSeconds !== null && step.endMs <= elapsedMs + 1) {
      bestHoldSec = Math.max(bestHoldSec, step.holdSeconds);
    }
  });

  return { roundsDone, bestHoldSec, elapsedSec: Math.round(elapsedMs / 1000) };
}
