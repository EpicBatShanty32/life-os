import { useId } from 'react';
import type { ReactNode } from 'react';

export type IconName =
  | 'home'
  | 'today'
  | 'target'
  | 'habits'
  | 'mood'
  | 'scale'
  | 'moon'
  | 'sun'
  | 'auto'
  | 'dumbbell'
  | 'apple'
  | 'book'
  | 'journal'
  | 'chart'
  | 'settings'
  | 'search'
  | 'plus'
  | 'chevron-left'
  | 'chevron-right'
  | 'flame'
  | 'sync'
  | 'droplet'
  | 'clock'
  | 'arrow-right'
  | 'arrow-up-right'
  | 'check'
  | 'check-circle'
  | 'play'
  | 'close'
  | 'sparkles'
  | 'trend-up'
  | 'trend-down'
  | 'command'
  | 'dot';

const PATHS: Record<IconName, ReactNode> = {
  home: (
    <>
      <path d="M3.5 10.8 12 3.5l8.5 7.3" />
      <path d="M5.5 9.5v11h13v-11" />
      <path d="M9.5 20.5v-5.8h5v5.8" />
    </>
  ),
  today: (
    <>
      <rect x="3.5" y="4.8" width="17" height="15.7" rx="3" />
      <path d="M8 3v3.2M16 3v3.2M3.5 9.4h17" />
      <path d="m8.6 14.6 2.2 2.2 4.6-4.7" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="4.4" />
      <circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  habits: (
    <>
      <path d="M20 7.2A8.4 8.4 0 0 0 6 4.2" />
      <path d="M4 4.5v4.4h4.4" />
      <path d="M4 16.8a8.4 8.4 0 0 0 14 3" />
      <path d="M20 19.5v-4.4h-4.4" />
    </>
  ),
  mood: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.7 14.2c.9 1.4 2 2.1 3.3 2.1s2.4-.7 3.3-2.1" />
      <circle cx="9.2" cy="9.8" r=".95" fill="currentColor" stroke="none" />
      <circle cx="14.8" cy="9.8" r=".95" fill="currentColor" stroke="none" />
    </>
  ),
  scale: (
    <>
      <rect x="4" y="3.2" width="16" height="17.6" rx="4" />
      <path d="M8.2 8.6a4.2 4.2 0 0 1 7.6 0" />
      <path d="m12 12.4 2.6-2.2" />
    </>
  ),
  moon: <path d="M20.3 14.3A8.6 8.6 0 0 1 9.7 3.7a8.6 8.6 0 1 0 10.6 10.6Z" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4.1" />
      <path d="M12 2.6v2.2M12 19.2v2.2M4.3 4.3l1.6 1.6M18.1 18.1l1.6 1.6M2.6 12h2.2M19.2 12h2.2M4.3 19.7l1.6-1.6M18.1 5.9l1.6-1.6" />
    </>
  ),
  auto: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 3.5a8.5 8.5 0 0 1 0 17" fill="currentColor" stroke="none" />
    </>
  ),
  dumbbell: (
    <>
      <path d="M6.6 7.2v9.6M17.4 7.2v9.6M3.4 10v4M20.6 10v4M6.6 12h10.8" />
    </>
  ),
  apple: (
    <>
      <path d="M12 8.4c-1.2-2.5-4.7-2.7-6.2.2-1.8 3.3.2 9.6 3.3 10.9 1.2.5 2 .2 2.9-.1.9.3 1.7.6 2.9.1 3.1-1.3 5.1-7.6 3.3-10.9-1.5-2.9-5-2.7-6.2-.2Z" />
      <path d="M12 8.4c.1-1.9.9-3 2.4-3.6" />
    </>
  ),
  book: (
    <>
      <path d="M4.2 5.4A2.6 2.6 0 0 1 6.8 3H20v15.4H6.8a2.6 2.6 0 0 0-2.6 2.6V5.4Z" />
      <path d="M4.2 20.4A2.6 2.6 0 0 1 6.8 18H20" />
      <path d="M9 7.2h6" />
    </>
  ),
  journal: (
    <>
      <rect x="5" y="3.2" width="14" height="17.6" rx="2.8" />
      <path d="M9 8h6M9 11.6h6M9 15.2h3.8" />
    </>
  ),
  chart: (
    <>
      <path d="M4 4v16.4h16" />
      <path d="M8.4 17V11M12.8 17V7.4M17.2 17v-4.2" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
    </>
  ),
  search: (
    <>
      <circle cx="10.8" cy="10.8" r="6.6" />
      <path d="m15.7 15.7 4.1 4.1" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  'chevron-left': <path d="M15 5.5 8.5 12l6.5 6.5" />,
  'chevron-right': <path d="M9 5.5 15.5 12 9 18.5" />,
  flame: (
    <>
      <path d="M12 21c4 0 6.5-2.6 6.5-6 0-2.4-1.4-4.3-2.7-5.9-.5 1.6-1.4 2.3-2.4 2.5.4-2.9-1-5.6-3.9-7.6.2 2.6-1.4 4-2.7 5.7-1 1.4-1.8 3-1.8 5.3C5.5 18.4 8 21 12 21Z" />
      <path d="M12 17.6c1.4 0 2.3-.9 2.3-2.2 0-1.2-.8-2-1.6-2.8-.2.7-.6 1.1-1 1.3.1-1.2-.4-2.3-1.5-3.1 0 1.3-.5 1.9-.9 2.6-.4.6-.6 1.2-.6 2 .1 1.3.9 2.2 3.3 2.2Z" />
    </>
  ),
  sync: (
    <>
      <path d="M20 12a8 8 0 0 0-13.8-5.5L4 9" />
      <path d="M4 4.4V9h4.6" />
      <path d="M4 12a8 8 0 0 0 13.8 5.5L20 15" />
      <path d="M20 19.6V15h-4.6" />
    </>
  ),
  droplet: (
    <path d="M12 3.4c3 4 5.5 6.7 5.5 9.8A5.5 5.5 0 0 1 12 18.8a5.5 5.5 0 0 1-5.5-5.6c0-3.1 2.5-5.8 5.5-9.8Z" />
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.4V12l3.1 1.9" />
    </>
  ),
  'arrow-right': <path d="M4.5 12h15M13.5 6l6 6-6 6" />,
  'arrow-up-right': <path d="M7 17 17 7M9.2 7H17v7.8" />,
  check: <path d="m5 12.6 4.4 4.4L19 7.4" />,
  'check-circle': (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m8.2 12.3 2.6 2.6 5-5.1" />
    </>
  ),
  play: <path d="M8.4 5.6v12.8L19 12 8.4 5.6Z" fill="currentColor" stroke="none" />,
  close: <path d="m6.4 6.4 11.2 11.2M17.6 6.4 6.4 17.6" />,
  sparkles: (
    <>
      <path d="M12 3.2l1.7 4.6 4.6 1.7-4.6 1.7L12 15.8l-1.7-4.6-4.6-1.7 4.6-1.7L12 3.2Z" />
      <path d="M18.2 15.4l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2Z" />
    </>
  ),
  'trend-up': (
    <>
      <path d="M3.5 17.2 9.4 11.3l3.9 3.9 7.2-7.2" />
      <path d="M15.3 8h5.2v5.2" />
    </>
  ),
  'trend-down': (
    <>
      <path d="M3.5 6.8 9.4 12.7l3.9-3.9 7.2 7.2" />
      <path d="M15.3 16h5.2v-5.2" />
    </>
  ),
  command: (
    <path d="M9.1 6.2a2.9 2.9 0 1 0-2.9 2.9h11.6a2.9 2.9 0 1 0-2.9-2.9v11.6a2.9 2.9 0 1 0 2.9-2.9H6.2a2.9 2.9 0 1 0 2.9 2.9V6.2Z" />
  ),
  dot: <circle cx="12" cy="12" r="2.6" fill="currentColor" stroke="none" />,
};

export interface IconProps {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
}

export function Icon({ name, size = 20, strokeWidth = 1.7, className }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}

/** Логотип — три «контура» на градиенте палитры, только inline SVG. */
export function LogoMark({ size = 34 }: { size?: number }) {
  const rawId = useId();
  const gradientId = `lifeos-logo-${rawId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#A18BB7" />
          <stop offset="0.55" stopColor="#B9CBE8" />
          <stop offset="1" stopColor="#AEDEC2" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="10" fill={`url(#${gradientId})`} />
      <path
        d="M9.5 22.5V13M16 22.5V8M22.5 22.5v-6"
        stroke="#FFFFFF"
        strokeWidth="2.6"
        strokeLinecap="round"
        opacity="0.95"
      />
      <circle cx="9.5" cy="9.6" r="2.1" fill="#FFFFFF" />
    </svg>
  );
}
