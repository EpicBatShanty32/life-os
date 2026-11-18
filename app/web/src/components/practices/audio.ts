import type { PhaseKind } from '../../api/practices';

/* ─────────────────────────────────────────────────────────────
   Звук, голос и вибрация плеера.

   Тон — осциллятор с огибающей, без файлов: мягкий, тихий, чтобы
   не резал слух в наушниках. Голос — только если браузер умеет
   SpeechSynthesis, иначе молча живём без него.
   ───────────────────────────────────────────────────────────── */

type AudioCtor = typeof AudioContext;

let context: AudioContext | null = null;
let contextBroken = false;

function audioCtor(): AudioCtor | null {
  if (typeof AudioContext === 'function') return AudioContext;
  const scope = window as Window & { webkitAudioContext?: AudioCtor };
  return scope.webkitAudioContext ?? null;
}

/** Создаём контекст на жесте пользователя: иначе браузер его «усыпит». */
export function primeAudio(): void {
  if (contextBroken) return;
  try {
    const Ctor = audioCtor();
    if (!Ctor) {
      contextBroken = true;
      return;
    }
    if (!context) context = new Ctor();
    if (context.state === 'suspended') void context.resume();
  } catch {
    contextBroken = true;
  }
}

const TONES: Record<PhaseKind, number> = {
  inhale: 528,
  exhale: 396,
  hold: 440,
  holdOut: 440,
  rest: 588,
  pulse: 552,
  pose: 495,
};

/** Короткий тон на смену фазы: 8 мс атаки, 600 мс затухания, пик 0.05. */
export function playTone(kind: PhaseKind): void {
  if (!context || context.state !== 'running') return;
  try {
    const now = context.currentTime;
    const oscillator = context.createOscillator();
    const gain = context.createGain();

    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(TONES[kind], now);
    if (kind === 'inhale' || kind === 'rest') {
      oscillator.frequency.linearRampToValueAtTime(TONES[kind] * 1.03, now + 0.36);
    }

    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.05, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.6);

    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(now);
    oscillator.stop(now + 0.65);
  } catch {
    /* звук не критичен для практики */
  }
}

/** Финальный тон — чуть выше и длиннее, чтобы отличить конец. */
export function playFinishTone(): void {
  if (!context || context.state !== 'running') return;
  try {
    const now = context.currentTime;
    [660, 880].forEach((frequency, index) => {
      const oscillator = context?.createOscillator();
      const gain = context?.createGain();
      if (!oscillator || !gain || !context) return;
      const start = now + index * 0.16;
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(frequency, start);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.04, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.5);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.55);
    });
  } catch {
    /* молча */
  }
}

export function speechAvailable(): boolean {
  return (
    typeof window !== 'undefined' &&
    'speechSynthesis' in window &&
    typeof window.SpeechSynthesisUtterance === 'function'
  );
}

export function speak(text: string): void {
  if (!speechAvailable()) return;
  try {
    const synth = window.speechSynthesis;
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'ru-RU';
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.volume = 0.7;
    const voice = synth.getVoices().find((item) => item.lang.toLowerCase().startsWith('ru'));
    if (voice) utterance.voice = voice;
    synth.speak(utterance);
  } catch {
    /* голос не критичен */
  }
}

export function stopSpeech(): void {
  try {
    if (speechAvailable()) window.speechSynthesis.cancel();
  } catch {
    /* молча */
  }
}

export function vibrationAvailable(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function';
}

export function vibrate(pattern: number | number[]): void {
  if (!vibrationAvailable()) return;
  try {
    navigator.vibrate(pattern);
  } catch {
    /* молча */
  }
}
