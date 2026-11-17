import { Icon } from './Icon';
import type { IconName } from './Icon';
import type { Accent } from '../lib/ui';

export interface KpiTrend {
  value: string;
  direction: 'up' | 'down';
  tone: 'good' | 'bad' | 'neutral';
}

interface KpiCardProps {
  icon: IconName;
  accent: Accent;
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  trend?: KpiTrend;
  progress?: number;
}

export function KpiCard({
  icon,
  accent,
  label,
  value,
  unit,
  hint,
  trend,
  progress,
}: KpiCardProps) {
  const meterValue =
    progress === undefined ? undefined : Math.min(100, Math.max(0, Math.round(progress)));

  return (
    <article className="card kpi" data-accent={accent}>
      <header className="kpi__head">
        <span className="kpi__icon" aria-hidden="true">
          <Icon name={icon} size={18} />
        </span>
        <span className="kpi__label">{label}</span>
        {trend ? (
          <span className={`kpi__trend kpi__trend--${trend.tone}`}>
            <Icon name={trend.direction === 'up' ? 'trend-up' : 'trend-down'} size={13} />
            <span>{trend.value}</span>
          </span>
        ) : null}
      </header>

      <p className="kpi__value">
        <strong>{value}</strong>
        {unit ? <span className="kpi__unit">{unit}</span> : null}
      </p>

      {meterValue !== undefined ? (
        <div
          className="meter"
          role="progressbar"
          aria-valuenow={meterValue}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${label}: ${meterValue}%`}
        >
          <span className="meter__fill" style={{ width: `${meterValue}%` }} />
        </div>
      ) : null}

      {hint ? <p className="kpi__hint">{hint}</p> : null}
    </article>
  );
}
