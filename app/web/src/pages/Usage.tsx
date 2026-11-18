import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react';
import { apiErrorMessage } from '../api/errors';
import {
  DEFAULT_USAGE_WINDOW,
  HEAT_SLOTS,
  USAGE_WINDOWS,
  actionRows,
  catalogGroups,
  downloadUsageReport,
  fetchUsage,
  formatClock,
  formatDay,
  formatSpan,
  formatStamp,
  heatGrid,
  heatSlotLabel,
  percentText,
  screenLabels,
} from '../api/usage';
import type {
  ActionRow,
  ThoughtTopic,
  UsageData,
  UsageWindowDays,
} from '../api/usage';
import { Button, Card, CardBody, CardHeader, EmptyState, Kpi, ProgressBar, Segmented, Select, Skeleton, Table } from '../components/ui';
import { Icon } from '../components/Icon';
import { formatDateShort, formatNumber, pluralRu } from '../lib/format';
import { findRoute, hrefFor, navigate } from '../lib/router';
import { toast } from '../lib/toast';
import {
  logTrackerAction,
  readTrackerReport,
  setCollectionPaused,
  trackerScreenKey,
  useUsageTracker,
} from '../lib/usageTracker';
import type { TrackerEvent, TrackerReport } from '../lib/usageTracker';
import '../styles/pages-usage.css';

/* ── Константы ───────────────────────────────────────────── */

const WINDOW_OPTIONS = USAGE_WINDOWS.map((days) => ({
  value: String(days),
  label: `${days} дн.`,
}));

/** Порог, после которого времени достаточно, чтобы строить по нему бары. */
const TIME_ENOUGH_SECONDS = 60;

const WEEKDAY_FALLBACK = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];

type TabKey = 'summary' | 'coverage' | 'routes' | 'events';

interface TabDef {
  key: TabKey;
  label: string;
  count?: number;
}

/** Короткие подписи действий в ленте: длинные фразы ломают моноширинный ряд. */
const LOG_ACTION_LABELS: Record<TrackerEvent['action'], string> = {
  view: 'визит',
  pause: 'пауза сбора',
  resume: 'возобновление',
  export: 'выгрузка отчёта',
};

/* ── Мелкие помощники ────────────────────────────────────── */

/** Подпись экрана: сначала название маршрута, потом каталог сервера.
 *
 *  Каталог знает и веб-экраны, и интенты бота, причём ключи пересекаются
 *  (`journal`, `today`): у бота это «Мысль» и «Сводка дня». Для пути из
 *  браузера правдивее название раздела в оболочке — «Журнал» и «Сегодня».
 */
function screenTitle(path: string, labels: Map<string, string>): string {
  const route = findRoute(path);
  if (route) return route.title;
  const key = trackerScreenKey(path);
  return labels.get(key) ?? key;
}

/** Тепловая карта: человеческая подпись ячейки для скринридера и title. */
function heatCellLabel(day: string, slot: number, count: number): string {
  const from = slot * 3;
  const tail = count === 0 ? 'нет событий' : `${formatNumber(count)} ${pluralRu(count, ['событие', 'события', 'событий'])}`;
  return `${day}, с ${from} до ${from + 3} часов: ${tail}`;
}

function stampWithSeconds(at: number): string {
  return `${formatDateShort(new Date(at))}, ${formatClock(at)}`;
}

/* ── Вкладки (WAI-ARIA tabs) ─────────────────────────────── */

interface UsageTabsProps {
  tabs: readonly TabDef[];
  value: TabKey;
  onChange: (value: TabKey) => void;
}

function UsageTabs({ tabs, value, onChange }: UsageTabsProps) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const move = (index: number, direction: 1 | -1): number =>
    (index + direction + tabs.length) % tabs.length;

  const onKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = -1;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = move(index, 1);
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = move(index, -1);
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    if (next < 0) return;
    event.preventDefault();
    const target = tabs[next];
    if (!target) return;
    onChange(target.key);
    refs.current[next]?.focus();
  };

  return (
    <div className="usage-tabs" role="tablist" aria-label="Разделы статистики">
      {tabs.map((tab, index) => {
        const selected = tab.key === value;
        return (
          <button
            key={tab.key}
            ref={(node) => {
              refs.current[index] = node;
            }}
            type="button"
            role="tab"
            id={`usage-tab-${tab.key}`}
            className="usage-tabs__tab"
            aria-selected={selected}
            aria-controls={`usage-panel-${tab.key}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.key)}
            onKeyDown={(event) => onKeyDown(event, index)}
          >
            {tab.label}
            {tab.count !== undefined ? (
              <span className="usage-tabs__count">{formatNumber(tab.count)}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

/* ── Полоса сбора ────────────────────────────────────────── */

interface CollectBarProps {
  paused: boolean;
  data: UsageData | null;
  report: TrackerReport;
  days: UsageWindowDays;
  refreshing: boolean;
  onToggle: () => void;
  onDownload: () => void;
  onReload: () => void;
}

function CollectBar({
  paused,
  data,
  report,
  days,
  refreshing,
  onToggle,
  onDownload,
  onReload,
}: CollectBarProps) {
  const lastServerDay = data?.screens.find((screen) => screen.lastUsed)?.lastUsed ?? null;

  return (
    <section
      className="usage-collect"
      data-paused={paused ? 'true' : 'false'}
      aria-label="Сбор статистики"
    >
      <div className="usage-collect__top">
        <p className="usage-collect__state">
          <span className="usage-collect__dot" aria-hidden="true" />
          <span className="usage-collect__label">
            {paused ? 'Сбор приостановлен' : 'Сбор включён'}
          </span>
          <span className="usage-collect__hint">
            {paused ? 'браузер не пишет время и переходы' : 'запись в реальном времени'}
          </span>
        </p>
        <div className="usage-collect__actions">
          <Button
            size="sm"
            leadingIcon={paused ? 'play' : 'pause'}
            onClick={onToggle}
            aria-pressed={paused}
          >
            {paused ? 'Возобновить сбор' : 'Пауза сбора'}
          </Button>
          <Button size="sm" leadingIcon="download" onClick={onDownload} disabled={!data}>
            Скачать отчёт JSON
          </Button>
          <Button
            size="sm"
            variant="ghost"
            leadingIcon="sync"
            loading={refreshing}
            onClick={onReload}
          >
            Обновить
          </Button>
        </div>
      </div>

      <p className="usage-collect__meta">
        <span>
          Событий в базе: <b>{data ? formatNumber(data.totals.events) : '—'}</b>
        </span>
        <span>
          последнее:{' '}
          <b>{report.lastEventAt ? formatStamp(report.lastEventAt) : formatDay(lastServerDay)}</b>
        </span>
        <span>
          окно {days} дн.:{' '}
          <b>
            {data
              ? `${formatNumber(data.totals.activeDays)} ${pluralRu(data.totals.activeDays, ['день', 'дня', 'дней'])}`
              : '—'}
          </b>{' '}
          с записями
        </span>
        <span>
          браузер записал:{' '}
          <b>
            {formatNumber(report.visits)} {pluralRu(report.visits, ['визит', 'визита', 'визитов'])}
          </b>
        </span>
      </p>

      <p className="usage-collect__note">
        Визиты экранов и сообщения бота считает сервер — это сводка из локальной базы.
        Время на экране, переходы и ленту событий пишет браузер: они остаются в этом
        профиле и никуда не отправляются. Пауза останавливает только браузерный сбор,
        счётчики базы продолжают пополняться.
      </p>
    </section>
  );
}

/* ── Бары «на что уходит время» ──────────────────────────── */

interface BarRow {
  id: string;
  name: string;
  hint?: string;
  value: number;
  label: string;
}

function BarList({ rows }: { rows: readonly BarRow[] }) {
  const max = rows.reduce((top, row) => Math.max(top, row.value), 1);
  return (
    <div className="usage-bars">
      {rows.map((row) => (
        <div className="usage-bar" key={row.id}>
          <span className="usage-bar__label">
            <span className="usage-bar__name">{row.name}</span>
            {row.hint ? <span className="usage-bar__source">{row.hint}</span> : null}
          </span>
          <span className="usage-bar__track">
            <span className="usage-bar__rail" aria-hidden="true">
              <span
                className="usage-bar__fill"
                style={{ width: `${Math.max(2, Math.round((row.value / max) * 100))}%` }}
              />
            </span>
            <span className="usage-bar__value">{row.label}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

/* ── Сводка ──────────────────────────────────────────────── */

interface SummaryPanelProps {
  data: UsageData;
  report: TrackerReport;
  labels: Map<string, string>;
}

function SummaryPanel({ data, report, labels }: SummaryPanelProps) {
  const timeMode = report.totalSeconds >= TIME_ENOUGH_SECONDS;

  const timeRows = useMemo<BarRow[]>(() => {
    const timed = report.screens.filter((row) => row.seconds > 0);
    return timed.slice(0, 10).map((row) => ({
      id: row.path,
      name: screenTitle(row.path, labels),
      value: row.seconds,
      label: `${formatSpan(row.seconds)} · ${percentText((row.seconds / report.totalSeconds) * 100)}`,
    }));
  }, [report, labels]);

  const visitRows = useMemo<BarRow[]>(() => {
    const total = data.screens.reduce((sum, screen) => sum + screen.count, 0) || 1;
    return data.screens.slice(0, 10).map((screen) => ({
      id: `${screen.source}:${screen.key}`,
      name: screen.label,
      hint: screen.source === 'bot' ? 'бот' : undefined,
      value: screen.count,
      label: `${formatNumber(screen.count)} · ${percentText((screen.count / total) * 100)}`,
    }));
  }, [data]);

  const restTime = report.screens
    .filter((row) => row.seconds > 0)
    .slice(10)
    .reduce((sum, row) => sum + row.seconds, 0);
  const restTimeCount = report.screens.filter((row) => row.seconds > 0).length - 10;
  const restVisits = data.screens.slice(10).reduce((sum, screen) => sum + screen.count, 0);

  const actions = useMemo(() => actionRows(data), [data]);

  return (
    <>
      <div className="usage-grid">
        <Card>
          <CardHeader
            title="На что уходит время"
            subtitle={
              timeMode
                ? `Время на экране за ${data.window.days} дн. — ${formatSpan(report.totalSeconds)} всего`
                : `Время ещё не накоплено — показываю визиты за ${data.window.days} дн.`
            }
          />
          <CardBody>
            {timeMode ? (
              <>
                <BarList rows={timeRows} />
                {restTimeCount > 0 ? (
                  <p className="usage-bar__rest">
                    ещё {formatNumber(restTimeCount)}{' '}
                    {pluralRu(restTimeCount, ['экран', 'экрана', 'экранов'])} — {formatSpan(restTime)}
                  </p>
                ) : null}
                <p className="usage-bars__note">
                  Время мерит браузер: пока вкладка активна и открыт экран. Бот и вкладки
                  в фоне не считаются.
                </p>
              </>
            ) : visitRows.length > 0 ? (
              <>
                <BarList rows={visitRows} />
                {restVisits > 0 ? (
                  <p className="usage-bar__rest">
                    остальные экраны — {formatNumber(restVisits)}{' '}
                    {pluralRu(restVisits, ['визит', 'визита', 'визитов'])}
                  </p>
                ) : null}
                <p className="usage-bars__note">
                  Доля визитов из базы: открытия экранов веба и сообщения бота. Доли по
                  времени появятся, когда трекер накопит минуту на экранах.
                </p>
              </>
            ) : (
              <EmptyState
                title="Пока нет данных"
                description="Откройте пару разделов — визиты появятся в сводке автоматически."
              />
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Частые действия" subtitle="Что именно делалось — и сколько раз" />
          <CardBody>
            <Table<ActionRow>
              columns={[
                {
                  key: 'label',
                  header: 'Действие',
                  cell: (row) => (
                    <>
                      <span>{row.label}</span>
                      {row.where ? (
                        <span className="usage-actions__where">{row.where}</span>
                      ) : null}
                    </>
                  ),
                },
                {
                  key: 'count',
                  header: 'Раз',
                  numeric: true,
                  width: '64px',
                  cell: (row) => <span className="usage-actions__num">{formatNumber(row.count)}</span>,
                },
                {
                  key: 'share',
                  header: 'Доля',
                  numeric: true,
                  width: '76px',
                  cell: (row) => (
                    <span className="usage-actions__num">{percentText(row.share * 100)}</span>
                  ),
                },
              ]}
              rows={actions}
              rowKey={(row) => row.action}
              caption="Частые действия за выбранное окно"
              empty={
                <EmptyState
                  title="Действий пока нет"
                  description="Первое событие появится, как только вы откроете экран или напишете боту."
                />
              }
            />
          </CardBody>
        </Card>
      </div>
    </>
  );
}

/* ── Покрытие ────────────────────────────────────────────── */

function CoveragePanel({ data }: { data: UsageData }) {
  const groups = useMemo(() => catalogGroups(data), [data]);
  const { used, total, pct, web, bot } = data.coverage;

  return (
    <Card>
      <CardHeader
        title="Покрытие функций"
        subtitle={`Каталог из ${formatNumber(total)} функций: что уже пробовали, а что ни разу`}
      />
      <CardBody>
        <div className="usage-coverage">
          <div className="usage-coverage__lead">
            <div>
              <p className="usage-coverage__total">
                <span className="usage-coverage__pct">{formatNumber(pct, 1)} %</span>
                <span className="usage-coverage__fraction">
                  {formatNumber(used)} из {formatNumber(total)} функций
                </span>
              </p>
              <p className="usage-bars__note">
                Функция считается использованной, если в базе есть хотя бы одно событие по
                ней. Ниже — весь каталог по разделам: знакомые сверху, «ни разу» следом.
              </p>
            </div>
            <div className="usage-coverage__meters">
              <ProgressBar
                label="Веб-экраны"
                value={web.used}
                max={web.total || 1}
                tone="accent"
                valueLabel={`${formatNumber(web.used)} из ${formatNumber(web.total)}`}
              />
              <ProgressBar
                label="Бот"
                value={bot.used}
                max={bot.total || 1}
                tone="cat-2"
                valueLabel={`${formatNumber(bot.used)} из ${formatNumber(bot.total)}`}
              />
            </div>
          </div>

          <div className="usage-coverage__groups">
            {groups.map((group) => (
              <section className="usage-cats" key={group.id} aria-label={group.title}>
                <header className="usage-cats__head">
                  <h3 className="usage-cats__title">{group.title}</h3>
                  <span className="usage-cats__count">
                    {formatNumber(group.used)} / {formatNumber(group.total)}
                  </span>
                </header>
                <p className="usage-cats__hint">{group.hint}</p>
                <ul className="usage-cats__list">
                  {group.rows.map((row) => (
                    <li
                      className="usage-feature"
                      key={`${row.source}:${row.key}`}
                      data-used={row.used ? 'true' : 'false'}
                    >
                      <Icon
                        name={row.used ? 'check-circle' : 'dot'}
                        size={13}
                        className="usage-feature__mark"
                      />
                      <span className="usage-feature__name" title={row.label}>
                        {row.label}
                      </span>
                      <span className="usage-feature__flag">
                        {row.used
                          ? `${formatNumber(row.count)} · ${formatDay(row.lastUsed)}`
                          : 'ни разу'}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        </div>
      </CardBody>
    </Card>
  );
}

/* ── Маршруты ────────────────────────────────────────────── */

function RoutesPanel({ report, labels }: { report: TrackerReport; labels: Map<string, string> }) {
  const rows = report.transitions.slice(0, 12);
  const max = rows[0]?.count ?? 1;

  return (
    <Card>
      <CardHeader
        title="Маршруты"
        subtitle={`Переходы «экран → экран» с этого браузера — топ-12 из ${formatNumber(
          report.transitions.length,
        )}`}
      />
      <CardBody>
        {rows.length === 0 ? (
          <EmptyState
            title="Переходов пока нет"
            description="Трекер записывает маршрут при смене экрана — пройдите пару разделов, и связки появятся."
          />
        ) : (
          <ol className="usage-routes">
            {rows.map((row) => (
              <li className="usage-route" key={`${row.from}\u0000${row.to}`}>
                <span className="usage-route__from">{screenTitle(row.from, labels)}</span>
                <span className="usage-route__arrow" aria-hidden="true">
                  →
                </span>
                <span className="usage-route__to">
                  <span className="usage-route__name">{screenTitle(row.to, labels)}</span>
                  <span className="usage-route__rail" aria-hidden="true">
                    <span
                      className="usage-route__fill"
                      style={{ width: `${Math.round((row.count / max) * 100)}%` }}
                    />
                  </span>
                </span>
                <span className="usage-route__num">
                  {formatNumber(row.count)}
                  <span className="visually-hidden">
                    {' '}
                    {pluralRu(row.count, ['переход', 'перехода', 'переходов'])}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        )}
        <p className="usage-bars__note">
          Маршруты считает браузер: сервер хранит только сводку по экранам, поэтому
          порядок переходов есть лишь здесь, на этом устройстве.
        </p>
      </CardBody>
    </Card>
  );
}

/* ── Лента событий ───────────────────────────────────────── */

function EventsPanel({ report, labels }: { report: TrackerReport; labels: Map<string, string> }) {
  const filterId = useId();
  const [filter, setFilter] = useState<string>('all');

  const visitsByScreen = useMemo(() => {
    const counts = new Map<string, number>();
    report.events.forEach((event) => {
      if (event.action !== 'view') return;
      counts.set(event.screen, (counts.get(event.screen) ?? 0) + 1);
    });
    return counts;
  }, [report]);

  const activeFilter = filter !== 'all' && visitsByScreen.has(filter) ? filter : 'all';
  const rows =
    activeFilter === 'all'
      ? report.events
      : report.events.filter((event) => event.screen === activeFilter);

  const options = [
    { value: 'all', label: `Все экраны (${formatNumber(report.events.length)})` },
    ...[...visitsByScreen.entries()]
      .sort((left, right) => right[1] - left[1])
      .map(([path, count]) => ({
        value: path,
        label: `${screenTitle(path, labels)} · ${formatNumber(count)}`,
      })),
  ];

  return (
    <Card>
      <CardHeader
        title="События"
        subtitle={`Последние ${formatNumber(report.events.length)} записей браузерного трекера: время, экран, действие, сколько пробыл`}
      />
      <CardBody>
        <div className="usage-events__bar">
          <span className="usage-events__filter">
            <label htmlFor={filterId}>Показать</label>
            <Select
              id={filterId}
              size="sm"
              options={options}
              value={activeFilter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </span>
          <span className="usage-bars__note">
            Старше {formatNumber(240)} записей браузер не хранит — так лента не раздувает
            профиль.
          </span>
        </div>

        {rows.length === 0 ? (
          <EmptyState
            title="Лента пуста"
            description="Трекер записывает визиты с первого открытия приложения. Пройдите пару экранов — записи появятся."
          />
        ) : (
          <div className="usage-events__scroll">
            <table className="usage-events__table" aria-label="Последние события трекера">
              <thead>
                <tr>
                  <th scope="col">Время</th>
                  <th scope="col">Экран</th>
                  <th scope="col">Действие</th>
                  <th scope="col" className="is-num">
                    На экране
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((event, index) => {
                  const open = event.durationMs === 0 && event.action === 'view';
                  return (
                    <tr key={`${event.at}-${event.action}-${index}`}>
                      <td className="is-muted">{stampWithSeconds(event.at)}</td>
                      <td className="is-screen">{screenTitle(event.screen, labels)}</td>
                      <td className="is-action">{LOG_ACTION_LABELS[event.action]}</td>
                      <td className="is-num is-muted">
                        {event.durationMs > 0
                          ? formatSpan(event.durationMs / 1000)
                          : open
                            ? 'идёт'
                            : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/* ── Тепловая карта ──────────────────────────────────────── */

function HeatmapCard({ data }: { data: UsageData }) {
  const { slots, max } = useMemo(() => heatGrid(data.heatmap), [data.heatmap]);
  const weekdays = data.heatmap.weekdays.length === 7 ? data.heatmap.weekdays : WEEKDAY_FALLBACK;
  // Пик считаем по тем же клеткам, что и рисуем: серверный peak — это час,
  // а клетка карты — трёхчасовой слот, и число в подписи не совпадало бы с ней.
  const peak = useMemo(() => {
    let best: { weekday: number; slot: number; count: number } | null = null;
    for (let weekday = 0; weekday < slots.length; weekday += 1) {
      const row = slots[weekday] ?? [];
      for (let slot = 0; slot < row.length; slot += 1) {
        const count = row[slot] ?? 0;
        if (count > 0 && (best === null || count > best.count)) {
          best = { weekday, slot, count };
        }
      }
    }
    return best;
  }, [slots]);
  const legend = [0, 0.25, 0.5, 0.75, 1];

  const alphaFor = (value: number): number =>
    value <= 0 ? 0 : 0.14 + 0.76 * (value / Math.max(1, max));

  return (
    <Card>
      <CardHeader
        title="Когда я пользуюсь"
        subtitle="События веба и бота: день недели × трёхчасовой слот"
      />
      <CardBody>
        {max === 0 ? (
          <EmptyState
            title="Карта пока пустая"
            description="Событий за окно нет: как только появятся записи, клетки начнут темнеть."
          />
        ) : (
          <div className="usage-heat">
            <div className="usage-heat__wrap">
              <table className="usage-heat__table">
                <caption className="visually-hidden">
                  Тепловая карта событий по дням недели и трёхчасовым слотам
                </caption>
                <thead>
                  <tr>
                    <th scope="col">
                      <span className="visually-hidden">День</span>
                    </th>
                    {Array.from({ length: HEAT_SLOTS }, (_, slot) => (
                      <th scope="col" key={slot}>
                        {heatSlotLabel(slot)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {slots.map((row, weekday) => {
                    const day = weekdays[weekday] ?? '';
                    return (
                      <tr key={day || weekday}>
                        <th scope="row">{day}</th>
                        {row.map((value, slot) => (
                          <td
                            className="usage-heat__cell"
                            key={slot}
                            title={heatCellLabel(day, slot, value)}
                            aria-label={heatCellLabel(day, slot, value)}
                          >
                            <span
                              className="usage-heat__fill"
                              style={{ opacity: alphaFor(value) }}
                            />
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="usage-heat__foot">
              <span className="usage-heat__legend">
                <span>0</span>
                {legend.map((step) => (
                  <span className="usage-heat__swatch" key={step} aria-hidden="true">
                    <span
                      className="usage-heat__fill"
                      style={{ opacity: alphaFor(step * max) }}
                    />
                  </span>
                ))}
                <span>{formatNumber(max)}</span>
              </span>
              {peak ? (
                <span className="usage-heat__peak">
                  Пик: {weekdays[peak.weekday]}, {heatSlotLabel(peak.slot)} ·{' '}
                  <b>{formatNumber(peak.count)}</b>{' '}
                  {pluralRu(peak.count, ['событие', 'события', 'событий'])}
                </span>
              ) : null}
            </div>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/* ── Мысли ───────────────────────────────────────────────── */

interface ThoughtColumnProps {
  title: string;
  hint: string;
  topics: readonly ThoughtTopic[];
  metric: 'count' | 'growth' | 'fresh';
  empty: string;
}

function ThoughtColumn({ title, hint, topics, metric, empty }: ThoughtColumnProps) {
  const max = topics.reduce((top, topic) => Math.max(top, topic.count), 1);

  return (
    <section className="usage-thoughts__col" aria-label={title}>
      <header className="usage-thoughts__head">
        <h3 className="usage-thoughts__title">{title}</h3>
        <span className="usage-thoughts__count">{formatNumber(topics.length)}</span>
      </header>
      <p className="usage-thoughts__hint">{hint}</p>
      {topics.length === 0 ? (
        <p className="usage-thoughts__hint">{empty}</p>
      ) : (
        <div className="usage-thoughts__list">
          {topics.map((topic) => {
            const value =
              metric === 'growth'
                ? `${(topic.growthPct ?? 0) > 0 ? '+' : ''}${formatNumber(topic.growthPct ?? 0)} %`
                : formatNumber(topic.count);
            const meta =
              metric === 'growth'
                ? `было ${formatNumber(topic.prevCount)} · стало ${formatNumber(topic.count)}`
                : metric === 'fresh'
                  ? `впервые ${formatDay(topic.firstSeen)}`
                  : `последняя запись ${formatDay(topic.lastSeen)}`;
            return (
              <a
                className="usage-thought"
                key={topic.topic}
                href={hrefFor('/journal', { q: topic.label })}
                title={`Открыть журнал по теме «${topic.label}»`}
              >
                <span className="usage-thought__name">{topic.label}</span>
                <span className="usage-thought__num">{value}</span>
                <span className="usage-thought__rail" aria-hidden="true">
                  <span
                    className="usage-thought__fill"
                    style={{ width: `${Math.max(4, Math.round((topic.count / max) * 100))}%` }}
                  />
                </span>
                <span className="usage-thought__meta">{meta}</span>
              </a>
            );
          })}
        </div>
      )}
    </section>
  );
}

function ThoughtsCard({ data }: { data: UsageData }) {
  const thoughts = data.thoughts;
  const period = thoughts.window?.days ?? data.window.days;

  return (
    <Card>
      <CardHeader
        title="Мысли"
        subtitle={
          thoughts.entries > 0
            ? `Темы журнала за ${period} дн.: ${formatNumber(thoughts.entries)} ${pluralRu(
                thoughts.entries,
                ['запись', 'записи', 'записей'],
              )}`
            : `Темы журнала за ${period} дн.`
        }
        actions={
          <Button
            size="sm"
            variant="ghost"
            trailingIcon="arrow-right"
            onClick={() => navigate('/journal')}
          >
            Журнал
          </Button>
        }
      />
      <CardBody>
        {thoughts.note ? <p className="usage-bars__note">{thoughts.note}</p> : null}
        <div className="usage-thoughts">
          <ThoughtColumn
            title="Частые темы"
            hint="Что обсуждается чаще всего"
            topics={thoughts.topics.slice(0, 8)}
            metric="count"
            empty="Пока ни одной темы."
          />
          <ThoughtColumn
            title="Растёт"
            hint="Стало заметно чаще прошлого окна"
            topics={thoughts.rising.slice(0, 6)}
            metric="growth"
            empty="Роста пока нет — сравнение идёт с прошлым окном."
          />
          <ThoughtColumn
            title="Новое"
            hint="Впервые появилось в этом окне"
            topics={thoughts.new.slice(0, 6)}
            metric="fresh"
            empty="Новых тем нет."
          />
        </div>
        <p className="usage-bars__note">
          Темы считаются из текста журнала: значимые слова и метки бота, без внешних
          сервисов. Клик по теме открывает журнал.
        </p>
      </CardBody>
    </Card>
  );
}

/* ── Экран ───────────────────────────────────────────────── */

export function Usage() {
  const [windowDays, setWindowDays] = useState<UsageWindowDays>(DEFAULT_USAGE_WINDOW);
  const [data, setData] = useState<UsageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [tab, setTab] = useState<TabKey>('summary');

  const tracker = useUsageTracker();
  const report = useMemo(() => readTrackerReport(windowDays), [tracker.revision, windowDays]);
  const labels = useMemo(
    () => (data ? screenLabels(data) : new Map<string, string>()),
    [data],
  );

  useEffect(() => {
    let alive = true;
    setRefreshing(true);
    fetchUsage(windowDays)
      .then((next) => {
        if (!alive) return;
        setData(next);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!alive) return;
        setError(apiErrorMessage(cause, 'Статистика недоступна'));
      })
      .finally(() => {
        if (!alive) return;
        setLoading(false);
        setRefreshing(false);
      });
    return () => {
      alive = false;
    };
  }, [windowDays, reloadToken]);

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);

  const toggleCollection = useCallback(() => {
    const next = !tracker.paused;
    setCollectionPaused(next);
    toast(
      next
        ? 'Сбор приостановлен: браузер не пишет время и переходы'
        : 'Сбор включён: время и переходы снова пишутся',
      next ? 'default' : 'success',
    );
  }, [tracker.paused]);

  const download = useCallback(() => {
    if (!data) return;
    logTrackerAction('export');
    try {
      const name = downloadUsageReport(data, readTrackerReport(windowDays));
      toast(`Отчёт сохранён: ${name}`, 'success');
    } catch {
      toast('Не удалось собрать файл отчёта', 'error');
    }
  }, [data, windowDays]);

  const tabs: TabDef[] = [
    { key: 'summary', label: 'Сводка' },
    { key: 'coverage', label: 'Покрытие', count: data?.coverage.total },
    { key: 'routes', label: 'Маршруты', count: report.transitions.length },
    { key: 'events', label: 'События', count: report.events.length },
  ];

  const panels: Record<TabKey, ReactNode> = data ? {
    summary: <SummaryPanel data={data} report={report} labels={labels} />,
    coverage: <CoveragePanel data={data} />,
    routes: <RoutesPanel report={report} labels={labels} />,
    events: <EventsPanel report={report} labels={labels} />,
  } : {
    summary: null,
    coverage: null,
    routes: null,
    events: null,
  };

  return (
    <div className="page usage">
      <header className="page-head usage-head">
        <div className="page-head__text">
          <p className="page-head__kicker usage-head__kicker">Локальная аналитика</p>
          <h1 className="page-head__title">Как я пользуюсь Life OS</h1>
          <p className="page-head__sub">
            Только на этом компьютере: сводка лежит в локальной базе, время и переходы
            пишет браузер. Наружу ничего не уходит.
          </p>
        </div>
        <div className="usage-head__actions">
          <Segmented
            options={WINDOW_OPTIONS}
            value={String(windowDays)}
            onChange={(value) => setWindowDays(Number(value) as UsageWindowDays)}
            ariaLabel="Окно статистики"
          />
        </div>
      </header>

      <CollectBar
        paused={tracker.paused}
        data={data}
        report={report}
        days={windowDays}
        refreshing={refreshing}
        onToggle={toggleCollection}
        onDownload={download}
        onReload={reload}
      />

      {error ? (
        <Card role="status">
          <CardBody>
            <EmptyState
              title="Статистика не загрузилась"
              description={`${error}. ${
                data ? 'Показываю то, что уже пришло.' : 'Проверьте сервер и повторите.'
              }`}
              action={
                <Button size="sm" leadingIcon="sync" onClick={reload}>
                  Повторить
                </Button>
              }
            />
          </CardBody>
        </Card>
      ) : null}

      {loading && !data ? (
        <Card>
          <CardBody>
            <Skeleton variant="block" label="Загружаю статистику использования" />
            <Skeleton variant="text" lines={3} label="Загружаю показатели" />
          </CardBody>
        </Card>
      ) : null}

      {data ? (
        <>
          <section className="usage-kpis" aria-label="Показатели статистики">
            <Kpi
              label="Активное время"
              value={report.totalSeconds > 0 ? formatSpan(report.totalSeconds) : '—'}
              hint={
                report.firstSeen
                  ? `Браузер считает с ${formatStamp(report.firstSeen)}`
                  : 'Считает браузер, пока вкладка активна'
              }
            />
            <Kpi
              label="Покрытие функций"
              value={formatNumber(data.coverage.pct, 1)}
              unit="%"
              hint={`${formatNumber(data.coverage.used)} из ${formatNumber(
                data.coverage.total,
              )} функций`}
            />
            <Kpi
              label="Визиты"
              value={formatNumber(data.totals.web)}
              delta={
                data.trend.changePct !== null
                  ? {
                      value: `${data.trend.changePct > 0 ? '+' : ''}${formatNumber(
                        data.trend.changePct,
                        0,
                      )} %`,
                      direction: data.trend.changePct > 0 ? 'up' : data.trend.changePct < 0 ? 'down' : 'flat',
                      tone: 'neutral',
                      label: 'к прошлому окну такой же длины',
                    }
                  : undefined
              }
              hint={`в боте: ${formatNumber(data.totals.bot)} ${pluralRu(data.totals.bot, [
                'событие',
                'события',
                'событий',
              ])}`}
            />
            <Kpi
              label="Не использовано"
              value={formatNumber(Math.max(0, data.coverage.total - data.coverage.used))}
              unit={pluralRu(Math.max(0, data.coverage.total - data.coverage.used), [
                'функция',
                'функции',
                'функций',
              ])}
              hint={`веб ${formatNumber(
                Math.max(0, data.coverage.web.total - data.coverage.web.used),
              )} · бот ${formatNumber(
                Math.max(0, data.coverage.bot.total - data.coverage.bot.used),
              )}`}
            />
          </section>

          <UsageTabs tabs={tabs} value={tab} onChange={setTab} />

          {(Object.keys(panels) as TabKey[]).map((key) => (
            <div
              className="usage-panel"
              key={key}
              role="tabpanel"
              id={`usage-panel-${key}`}
              aria-labelledby={`usage-tab-${key}`}
              tabIndex={0}
              hidden={key !== tab}
            >
              {panels[key]}
            </div>
          ))}

          <HeatmapCard data={data} />

          <ThoughtsCard data={data} />
        </>
      ) : null}
    </div>
  );
}
