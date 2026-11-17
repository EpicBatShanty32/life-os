import { useId } from 'react';
import type { DayPoint } from '../api/types';
import type { Accent } from '../lib/ui';
import { formatDateShort, formatNumber } from '../lib/format';

const WIDTH = 320;
const PAD_X = 4;
const PAD_Y = 12;

const ACCENT_HEX: Record<Accent, string> = {
  violet: '#A18BB7',
  green: '#AEDEC2',
  peach: '#F2C9B6',
  blue: '#B9CBE8',
};

interface SparklineProps {
  points: DayPoint[];
  label: string;
  accent?: Accent;
  height?: number;
  unit?: string;
  digits?: number;
}

type Point = readonly [number, number];

/** Плавная кривая через середины сегментов — без библиотек. */
function smoothPath(coords: readonly Point[]): string {
  if (coords.length === 0) return '';
  const first = coords[0];
  let path = `M${first[0].toFixed(2)},${first[1].toFixed(2)}`;
  for (let index = 0; index < coords.length - 1; index += 1) {
    const p0 = coords[index - 1] ?? coords[index];
    const p1 = coords[index];
    const p2 = coords[index + 1];
    const p3 = coords[index + 2] ?? p2;
    const tension = 0.18;
    const cp1x = p1[0] + (p2[0] - p0[0]) * tension;
    const cp1y = p1[1] + (p2[1] - p0[1]) * tension;
    const cp2x = p2[0] - (p3[0] - p1[0]) * tension;
    const cp2y = p2[1] - (p3[1] - p1[1]) * tension;
    path += ` C${cp1x.toFixed(2)},${cp1y.toFixed(2)} ${cp2x.toFixed(2)},${cp2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
  }
  return path;
}

export function Sparkline({
  points,
  label,
  accent = 'green',
  height = 104,
  unit = 'кг',
  digits = 1,
}: SparklineProps) {
  const rawId = useId();
  const gradientId = `spark-${rawId.replace(/[^a-zA-Z0-9_-]/g, '')}`;

  if (points.length < 2) {
    return <p className="sparkline__empty">Мало данных для графика</p>;
  }

  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const innerWidth = WIDTH - PAD_X * 2;
  const innerHeight = height - PAD_Y * 2;
  const color = ACCENT_HEX[accent];

  const coords: Point[] = points.map((point, index) => {
    const x = PAD_X + (index * innerWidth) / (points.length - 1);
    const y = PAD_Y + (1 - (point.value - min) / span) * innerHeight;
    return [x, y] as const;
  });

  const linePath = smoothPath(coords);
  const first = coords[0];
  const last = coords[coords.length - 1];
  const areaPath = `${linePath} L${last[0].toFixed(2)},${(height - PAD_Y).toFixed(2)} L${first[0].toFixed(2)},${(height - PAD_Y).toFixed(2)} Z`;

  return (
    <figure className="sparkline">
      <svg
        className="sparkline__svg"
        viewBox={`0 0 ${WIDTH} ${height}`}
        role="img"
        aria-label={label}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor={color} stopOpacity="0.4" />
            <stop offset="1" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>

        {[0.25, 0.5, 0.75].map((fraction) => (
          <line
            key={fraction}
            x1={PAD_X}
            x2={WIDTH - PAD_X}
            y1={PAD_Y + innerHeight * fraction}
            y2={PAD_Y + innerHeight * fraction}
            className="sparkline__grid"
          />
        ))}

        <path d={areaPath} fill={`url(#${gradientId})`} stroke="none" />
        <path
          d={linePath}
          fill="none"
          stroke={color}
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />

        {points.map((point, index) => (
          <circle
            key={`${point.date}-${index}`}
            cx={coords[index][0]}
            cy={coords[index][1]}
            r={index === points.length - 1 ? 3.4 : 2}
            fill={color}
            stroke="var(--surface)"
            strokeWidth="1.4"
            className="sparkline__dot"
          >
            <title>{`${formatDateShort(point.date)}: ${formatNumber(point.value, digits)} ${unit}`}</title>
          </circle>
        ))}
      </svg>
      <figcaption className="sparkline__axis">
        <span>{formatDateShort(points[0].date)}</span>
        <span>{formatDateShort(points[points.length - 1].date)}</span>
      </figcaption>
    </figure>
  );
}
