import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  Practice,
  PracticeAward,
  PracticeResult,
  PracticeSessionInput,
} from '../../api/practices';
import { formatNumber, pluralRu } from '../../lib/format';
import { Button, Icon, Skeleton } from '../ui';
import {
  playFinishTone,
  playTone,
  primeAudio,
  speak,
  speechAvailable,
  stopSpeech,
  vibrate,
  vibrationAvailable,
} from './audio';
import { usePracticePrefs, useReducedMotion, useScrollLock, useWakeLock } from './hooks';
import type { PracticePrefs } from './hooks';
import {
  buildSteps,
  clamp01,
  circleScale,
  formatClock,
  holdLabel,
  locate,
  planRows,
  readout,
  roundBounds,
  summarize,
} from './timeline';

/* ─────────────────────────────────────────────────────────────
   Плеер практики: круг, фаза, счёт и управление.

   Таймер идёт от performance.now(), а не от числа тиков: каждый
   кадр мы заново выясняем, какой шаг сценария соответствует
   прошедшему времени. Поэтому пауза, пропуск раунда, погашенный
   экран и возврат во вкладку не сдвигают отсчёт.

   Анимация — только transform/opacity (круг) и dashoffset у кольца.
   React перерисовывается не чаще раза в секунду: крупное число
   меняется, только когда меняется его значение.

   Очки и серию считает сервер: итог показывает его ответ, а не
   локальную арифметику — иначе экран и /api/points разъезжаются.
   ───────────────────────────────────────────────────────────── */

const RING_LENGTH = 270.18; // 2πr при r = 43 во viewBox 0 0 100 100

const KIND_LABEL: Record<Practice['kind'], string> = {
  breathing: 'Дыхание',
  stretch: 'Растяжка',
  other: 'Другое',
};

const AWARD_WORD: Record<string, string> = {
  practice: 'за практику',
  record: 'за рекорд',
  streak: 'за серию',
};

/** Состав начисления: «+10 за практику · +5 за рекорд · +2 за серию». */
function awardText(awards: readonly PracticeAward[]): string {
  return awards
    .filter((award) => award.points > 0)
    .map((award) => `+${award.points} ${AWARD_WORD[award.kind] ?? award.reason}`)
    .join(' · ');
}

/** Ноль очков — это не ошибка: говорим причину, а не молчим. */
function zeroPointsText(result: PracticeResult): string {
  if (result.duplicated) return 'Сегодня эта практика уже засчитана: второй раз очки не идут.';
  if (result.roundsDone === 0)
    return 'Ни одного полного раунда — очков нет. Засчитываются только пройденные раунды.';
  if (result.capped) return 'На сегодня кап очков набран — дальше только завтра.';
  return 'Очки не начислены.';
}

const FOCUSABLE =
  'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface PracticePlayerProps {
  practice: Practice;
  onSubmit: (input: PracticeSessionInput) => Promise<PracticeResult>;
  onClose: () => void;
}

type Mode = 'intro' | 'active' | 'result';

interface Snapshot {
  roundsDone: number;
  bestHoldSec: number;
  elapsedSec: number;
}

interface Display {
  value: string;
  word: string;
  round: number;
}

export function PracticePlayer({ practice, onSubmit, onClose }: PracticePlayerProps) {
  const steps = useMemo(() => buildSteps(practice), [practice]);
  const totalMs = steps.length > 0 ? steps[steps.length - 1].endMs : 1;
  const totalRounds = Math.max(1, practice.rounds.length);
  const plan = useMemo(() => planRows(practice.rounds), [practice.rounds]);

  const { prefs, setPref } = usePracticePrefs();
  const reduced = useReducedMotion();

  const [mode, setMode] = useState<Mode>('intro');
  const [paused, setPaused] = useState(false);
  const [confirmExit, setConfirmExit] = useState(false);
  const [display, setDisplay] = useState<Display>({
    value: formatClock(practice.seconds),
    word: 'Готовы?',
    round: 0,
  });
  const [announcement, setAnnouncement] = useState('');
  const [ringOn, setRingOn] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [result, setResult] = useState<PracticeResult | null>(null);
  const [resultError, setResultError] = useState(false);

  const shellRef = useRef<HTMLElement>(null);
  const resultTitleRef = useRef<HTMLHeadingElement>(null);
  const discRef = useRef<HTMLSpanElement>(null);
  const ringRef = useRef<SVGCircleElement>(null);
  const barRef = useRef<HTMLSpanElement>(null);
  const phaseBarRef = useRef<HTMLSpanElement>(null);
  const segRefs = useRef<Array<HTMLSpanElement | null>>([]);

  const elapsedRef = useRef(0);
  const runStartRef = useRef<number | null>(null);
  const wallStartRef = useRef<string>('');
  const lastStepRef = useRef(-1);
  const finishedRef = useRef(false);
  const resumeAfterConfirmRef = useRef(false);

  const displayRef = useRef(display);
  const prefsRef = useRef(prefs);
  const stepsRef = useRef(steps);
  const modeRef = useRef<Mode>(mode);
  const reducedRef = useRef(reduced);
  const confirmRef = useRef(confirmExit);
  const submitRef = useRef(onSubmit);
  const closeRef = useRef(onClose);
  const finishRef = useRef<(elapsed: number) => void>(() => undefined);

  useEffect(() => {
    displayRef.current = display;
    prefsRef.current = prefs;
    stepsRef.current = steps;
    modeRef.current = mode;
    reducedRef.current = reduced;
    confirmRef.current = confirmExit;
    submitRef.current = onSubmit;
    closeRef.current = onClose;
  }, [display, prefs, steps, mode, paused, reduced, confirmExit, onSubmit, onClose]);

  useScrollLock(true);
  useWakeLock(mode === 'active' && !paused);

  const elapsedAt = useCallback((now: number): number => {
    const base = elapsedRef.current;
    return runStartRef.current === null ? base : base + (now - runStartRef.current);
  }, []);

  /* ── Кадр: визуал пишем в DOM напрямую, состояние — по секунде ── */

  const renderFrame = useCallback(
    (now: number) => {
      const list = stepsRef.current;
      const elapsed = elapsedAt(now);

      if (elapsed >= totalMs) {
        finishRef.current(elapsed);
        return;
      }

      const found = locate(list, elapsed);
      if (!found) return;
      const { step, elapsedInStep, remainingMs } = found;
      const progress = step.durationMs > 0 ? clamp01(elapsedInStep / step.durationMs) : 0;

      if (!reducedRef.current && discRef.current) {
        discRef.current.style.transform = `scale(${circleScale(step, progress).toFixed(4)})`;
      }
      if (ringRef.current) {
        ringRef.current.style.strokeDashoffset = String(RING_LENGTH * (1 - progress));
      }
      if (barRef.current) {
        barRef.current.style.transform = `scaleX(${clamp01(elapsed / totalMs).toFixed(4)})`;
      }
      if (phaseBarRef.current) {
        phaseBarRef.current.style.transform = `scaleX(${progress.toFixed(4)})`;
      }

      const bounds = roundBounds(list, step.roundIndex);
      const roundProgress =
        bounds && bounds.endMs > bounds.startMs
          ? clamp01((elapsed - bounds.startMs) / (bounds.endMs - bounds.startMs))
          : 0;
      segRefs.current.forEach((node, roundIndex) => {
        if (!node) return;
        const value =
          roundIndex < step.roundIndex ? 1 : roundIndex === step.roundIndex ? roundProgress : 0;
        node.style.transform = `scaleX(${value.toFixed(4)})`;
      });

      if (step.index !== lastStepRef.current) {
        const isFirst = lastStepRef.current === -1;
        lastStepRef.current = step.index;
        const staticPhase =
          step.kind === 'hold' ||
          step.kind === 'holdOut' ||
          step.kind === 'rest' ||
          step.kind === 'pose';
        setRingOn(staticPhase);

        if (!isFirst) {
          const current = prefsRef.current;
          if (current.sound) playTone(step.kind);
          if (current.vibration) {
            vibrate(step.kind === 'hold' || step.kind === 'holdOut' ? 20 : 10);
          }
          if (current.voice && step.kind !== 'pulse') speak(step.label);
          setAnnouncement(`Раунд ${step.roundIndex + 1} из ${totalRounds}. ${step.label}`);
        }
      }

      const next = readout(step, remainingMs);
      const shown = displayRef.current;
      if (
        shown.value !== next.value ||
        shown.word !== next.word ||
        shown.round !== step.roundIndex
      ) {
        const value: Display = {
          value: next.value,
          word: next.word,
          round: step.roundIndex,
        };
        displayRef.current = value;
        setDisplay(value);
      }
    },
    [elapsedAt, totalMs, totalRounds],
  );

  /* ── Запуск, пауза, пропуск, итог ─────────────────────────── */

  const startPractice = useCallback(() => {
    primeAudio();
    elapsedRef.current = 0;
    runStartRef.current = performance.now();
    wallStartRef.current = new Date().toISOString();
    lastStepRef.current = -1;
    finishedRef.current = false;
    resumeAfterConfirmRef.current = false;
    setSnapshot(null);
    setResult(null);
    setResultError(false);
    setConfirmExit(false);
    setPaused(false);
    setRingOn(false);
    setMode('active');
    const first = stepsRef.current[0];
    setAnnouncement(first ? `Раунд 1 из ${totalRounds}. ${first.label}` : 'Практика началась');
    if (prefsRef.current.sound) playTone('inhale');
  }, [totalRounds]);

  const togglePause = useCallback(() => {
    if (modeRef.current !== 'active') return;
    if (runStartRef.current === null) {
      runStartRef.current = performance.now();
      setPaused(false);
      setAnnouncement('Продолжаем');
      return;
    }
    elapsedRef.current += performance.now() - runStartRef.current;
    runStartRef.current = null;
    setPaused(true);
    stopSpeech();
    setAnnouncement('Пауза');
  }, []);

  const finish = useCallback((elapsed: number) => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    const capped = Math.min(elapsed, totalMs);
    const summary = summarize(stepsRef.current, capped);
    elapsedRef.current = capped;
    runStartRef.current = null;
    setPaused(false);
    setConfirmExit(false);
    setSnapshot(summary);
    setResult(null);
    setResultError(false);
    setMode('result');
    setAnnouncement('Практика завершена');
    stopSpeech();
    if (prefsRef.current.sound) playFinishTone();
    if (prefsRef.current.vibration) vibrate([14, 60, 14]);

    const input: PracticeSessionInput = {
      roundsDone: summary.roundsDone,
      bestHoldSec: summary.bestHoldSec,
      startedAt: wallStartRef.current || new Date(Date.now() - summary.elapsedSec * 1000).toISOString(),
      finishedAt: new Date().toISOString(),
    };
    submitRef
      .current(input)
      .then((value) => setResult(value))
      .catch(() => setResultError(true));
  }, [totalMs]);

  useEffect(() => {
    finishRef.current = finish;
  }, [finish]);

  const finishNow = useCallback(() => {
    finishRef.current(elapsedAt(performance.now()));
  }, [elapsedAt]);

  const skipRound = useCallback(() => {
    const list = stepsRef.current;
    const elapsed = elapsedAt(performance.now());
    const current = locate(list, elapsed);
    if (!current) return;
    const nextRound = current.step.roundIndex + 1;
    const bounds = nextRound < totalRounds ? roundBounds(list, nextRound) : null;
    if (!bounds) {
      finishRef.current(elapsed);
      return;
    }
    elapsedRef.current = bounds.startMs;
    runStartRef.current = performance.now();
    lastStepRef.current = -1;
    setPaused(false);
    setAnnouncement(`Раунд ${nextRound + 1} из ${totalRounds}`);
  }, [elapsedAt, totalRounds]);

  const openExitConfirm = useCallback(() => {
    if (modeRef.current !== 'active') {
      stopSpeech();
      closeRef.current();
      return;
    }
    resumeAfterConfirmRef.current = runStartRef.current !== null;
    if (runStartRef.current !== null) {
      elapsedRef.current += performance.now() - runStartRef.current;
      runStartRef.current = null;
      setPaused(true);
    }
    setConfirmExit(true);
    setAnnouncement('Выход из практики: подтвердите');
  }, []);

  const dismissConfirm = useCallback(() => {
    setConfirmExit(false);
    if (resumeAfterConfirmRef.current && !finishedRef.current) {
      resumeAfterConfirmRef.current = false;
      runStartRef.current = performance.now();
      setPaused(false);
      setAnnouncement('Продолжаем');
    }
  }, []);

  const exitWithoutSave = useCallback(() => {
    stopSpeech();
    closeRef.current();
  }, []);

  const confirmFinish = useCallback(() => {
    setConfirmExit(false);
    finishRef.current(elapsedRef.current);
  }, []);

  /* ── Цикл отсчёта: rAF на экране, редкий таймер в фоне ────── */

  useEffect(() => {
    if (mode !== 'active' || paused) return undefined;
    let raf = 0;
    let timer = 0;
    let stopped = false;

    const frame = () => {
      if (stopped) return;
      renderFrame(performance.now());
      raf = requestAnimationFrame(frame);
    };

    const tickWhileHidden = () => {
      if (stopped) return;
      renderFrame(performance.now());
      timer = window.setTimeout(tickWhileHidden, 300);
    };

    const restart = () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
      raf = 0;
      timer = 0;
      if (document.visibilityState === 'visible') {
        raf = requestAnimationFrame(frame);
      } else {
        timer = window.setTimeout(tickWhileHidden, 300);
      }
    };

    document.addEventListener('visibilitychange', restart);
    restart();

    return () => {
      stopped = true;
      document.removeEventListener('visibilitychange', restart);
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };
  }, [mode, paused, renderFrame]);

  /* ── Фокус, клавиатура, восстановление ───────────────────── */

  useEffect(() => {
    shellRef.current?.focus();
  }, []);

  useEffect(() => {
    if (mode === 'result') resultTitleRef.current?.focus();
  }, [mode]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Tab') {
        const nodes = Array.from(
          shellRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
        ).filter((node) => node.offsetParent !== null || node === document.activeElement);
        if (nodes.length === 0) return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        const active = document.activeElement;
        if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        } else if (event.shiftKey && (active === first || active === shellRef.current)) {
          event.preventDefault();
          last.focus();
        }
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        if (confirmRef.current) {
          dismissConfirm();
          return;
        }
        if (modeRef.current === 'result') {
          closeRef.current();
          return;
        }
        if (modeRef.current === 'intro') {
          closeRef.current();
          return;
        }
        openExitConfirm();
        return;
      }

      if (event.key === ' ' || event.code === 'Space') {
        if (confirmRef.current) return;
        const target = event.target;
        if (target instanceof HTMLElement && target.closest('button, a, input, textarea, select')) {
          return;
        }
        if (modeRef.current === 'intro') {
          event.preventDefault();
          startPractice();
          return;
        }
        if (modeRef.current === 'active') {
          event.preventDefault();
          togglePause();
        }
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [dismissConfirm, openExitConfirm, startPractice, togglePause]);

  /* ── Разметка ────────────────────────────────────────────── */

  const toggles: Array<{ key: keyof PracticePrefs; label: string }> = [
    { key: 'sound', label: 'Звук' },
    ...(vibrationAvailable() ? [{ key: 'vibration' as const, label: 'Вибрация' }] : []),
    ...(speechAvailable() ? [{ key: 'voice' as const, label: 'Голос' }] : []),
  ];

  const stage =
    mode === 'intro' ? (
      <div className="pp-intro">
        <p className="pp-intro__summary">{practice.summary}</p>

        <ol className="pp-plan">
          {plan.map((row) => (
            <li className="pp-plan__row" key={row.from}>
              <span className="pp-plan__name">
                {totalRounds > 1
                  ? row.from === row.to
                    ? `Раунд ${row.from}`
                    : `Раунды ${row.from}–${row.to}`
                  : 'Что делаем'}
              </span>
              <span className="pp-plan__desc">{row.text}</span>
            </li>
          ))}
        </ol>

        <p className="pp-intro__total">
          {formatClock(practice.seconds)} · {practice.points} очков
          {practice.recordHoldSec
            ? ` · мой рекорд ${practice.recordLabel ?? holdLabel(practice.recordHoldSec)}`
            : ''}
        </p>

        {practice.todayDone ? (
          <p className="pp-intro__flag">
            Сегодня уже пройдена: запись останется, а очки второй раз не начислят.
          </p>
        ) : null}

        {practice.safetyNote ? (
          <div className="pp-warning" role="note">
            <p className="pp-warning__title">Перед началом</p>
            <p>{practice.safetyNote}</p>
          </div>
        ) : null}
      </div>
    ) : mode === 'result' ? (
      <div className="pp-result">
        <div>
          <p className="pp-result__kicker">Практика завершена</p>
          <h3 className="pp-result__title" ref={resultTitleRef} tabIndex={-1}>
            {snapshot
              ? `${snapshot.roundsDone} из ${totalRounds} ${pluralRu(totalRounds, ['раунда', 'раундов', 'раундов'])}`
              : 'Итог'}
          </h3>
          <p className="pp-result__note">
            {snapshot ? formatClock(snapshot.elapsedSec) : '—'} · {KIND_LABEL[practice.kind]}
          </p>
        </div>

        <p
          className="pp-result__record"
          role="status"
          data-tone={resultError ? 'error' : undefined}
        >
          {resultError
            ? 'Очки не записались — попробуйте ещё раз'
            : result?.isRecord
              ? `Новый рекорд задержки: ${result.recordLabel ?? holdLabel(result.recordHoldSec ?? 0)}`
              : ''}
        </p>

        <dl className="pp-facts">
          <div className="pp-fact">
            <dt>Лучшая задержка</dt>
            <dd>{snapshot && snapshot.bestHoldSec > 0 ? holdLabel(snapshot.bestHoldSec) : '—'}</dd>
          </div>
          <div className="pp-fact">
            <dt>Очки</dt>
            <dd>
              {result ? (
                `+${result.pointsEarned}`
              ) : resultError ? (
                '—'
              ) : (
                <Skeleton variant="text" lines={1} label="Считаем очки" />
              )}
            </dd>
          </div>
          <div className="pp-fact">
            <dt>Серия</dt>
            <dd>
              {result
                ? `${result.streakDays} ${pluralRu(result.streakDays, ['день', 'дня', 'дней'])}`
                : '—'}
            </dd>
          </div>
          <div className="pp-fact">
            <dt>Всего очков</dt>
            <dd>{result ? formatNumber(result.totalPoints) : '—'}</dd>
          </div>
          <div className="pp-fact">
            <dt>Уровень</dt>
            <dd>{result ? result.level.title : '—'}</dd>
          </div>
        </dl>

        <p className="pp-result__foot">
          {resultError
            ? 'Сессия не записана: сервер не ответил. Практика при этом пройдена.'
            : result
              ? result.pointsEarned > 0
                ? [
                    awardText(result.awards),
                    result.cap.remaining > 0
                      ? `ещё ${result.cap.remaining} можно набрать сегодня`
                      : 'на сегодня кап набран',
                  ]
                    .filter(Boolean)
                    .join(' · ')
                : zeroPointsText(result)
              : 'Считаем очки…'}
        </p>
      </div>
    ) : (
      <div className="pp-active">
        <div className="pp-breath" data-ring={ringOn ? 'on' : undefined}>
          <span className="pp-breath__disc" ref={discRef} aria-hidden="true" />
          <svg className="pp-breath__ring" viewBox="0 0 100 100" aria-hidden="true">
            <circle className="pp-ring__track" cx="50" cy="50" r="43" />
            <circle className="pp-ring__fill" cx="50" cy="50" r="43" ref={ringRef} />
          </svg>
          <div className="pp-readout" data-paused={paused || undefined}>
            <div className="pp-readout__count">{display.value}</div>
            <div className="pp-readout__word">{paused ? 'Пауза' : display.word}</div>
          </div>
        </div>

        {reduced ? (
          <div className="pp-phasebar" aria-hidden="true">
            <span ref={phaseBarRef} />
          </div>
        ) : null}

        <div className="pp-rounds" data-single={totalRounds === 1 || undefined}>
          {totalRounds > 1 ? (
            <>
              <span className="pp-rounds__label">
                Раунд {display.round + 1} из {totalRounds}
              </span>
              <span className="pp-rounds__segs" aria-hidden="true">
                {practice.rounds.map((_, index) => (
                  <span className="pp-seg" key={index}>
                    <span
                      className="pp-seg__fill"
                      ref={(node) => {
                        segRefs.current[index] = node;
                      }}
                    />
                  </span>
                ))}
              </span>
            </>
          ) : (
            <span className="pp-rounds__label">
              {practice.level?.title ?? 'Один подход'} · {formatClock(practice.seconds)}
            </span>
          )}
        </div>
      </div>
    );

  return (
    <section
      className="pp-player"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pp-player-title"
      ref={shellRef}
      tabIndex={-1}
      data-reduced={reduced || undefined}
    >
      <div className="pp-player__inner">
        <header className="pp-player__head">
          <div className="pp-player__headings">
            <p className="pp-player__kicker">
              {KIND_LABEL[practice.kind]}
              {practice.level ? ` · ${practice.level.title.toLowerCase()}` : ''} ·{' '}
              {practice.minutes} мин
            </p>
            <h2 className="pp-player__title" id="pp-player-title">
              {practice.title}
            </h2>
          </div>
          <button
            type="button"
            className="pp-close"
            onClick={openExitConfirm}
            aria-label="Закрыть практику"
          >
            <Icon name="close" size={18} />
          </button>
        </header>

        <div className="pp-stage" data-mode={mode}>
          {stage}
        </div>

        <div className="pp-foot">
          {confirmExit ? (
            <div className="pp-confirm">
              <p className="pp-confirm__text">Прервать? Сделанное можно засчитать.</p>
              <div className="pp-actions">
                <Button variant="primary" fullWidth onClick={confirmFinish}>
                  Засчитать сделанное
                </Button>
                <Button variant="secondary" onClick={exitWithoutSave}>
                  Без записи
                </Button>
                <Button variant="ghost" onClick={dismissConfirm}>
                  Продолжить
                </Button>
              </div>
            </div>
          ) : mode === 'intro' ? (
            <Button variant="primary" fullWidth onClick={startPractice}>
              Начать
            </Button>
          ) : mode === 'result' ? (
            <div className="pp-actions">
              <Button variant="primary" fullWidth onClick={startPractice}>
                Ещё раз
              </Button>
              <Button variant="secondary" fullWidth onClick={onClose}>
                К практикам
              </Button>
            </div>
          ) : (
            <div className="pp-running">
              <div className="pp-toggles">
                {toggles.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    className="pp-toggle"
                    aria-pressed={prefs[item.key]}
                    onClick={() => {
                      if (item.key === 'sound' && !prefs.sound) primeAudio();
                      setPref(item.key, !prefs[item.key]);
                    }}
                  >
                    {`${item.label}: ${prefs[item.key] ? 'вкл' : 'выкл'}`}
                  </button>
                ))}
              </div>

              <p className="pp-hint">Пробел — пауза, Esc — выход</p>

              <div className="pp-total" aria-hidden="true">
                <span ref={barRef} />
              </div>

              <div className="pp-actions">
                <Button variant="primary" fullWidth onClick={togglePause}>
                  {paused ? 'Продолжить' : 'Пауза'}
                </Button>
                <Button variant="secondary" onClick={skipRound}>
                  Пропустить раунд
                </Button>
                <Button variant="ghost" onClick={finishNow}>
                  Закончить
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>

      <p className="pp-live" aria-live="polite">
        {announcement}
      </p>
    </section>
  );
}
