import type { CSSProperties } from 'react';

interface BrandMarkProps {
  /** Высота и ширина в пикселях: 30 в сайдбаре, 28 в мобильной шапке. */
  size?: number;
  className?: string;
}

const BASE = import.meta.env.BASE_URL ?? '/';

/**
 * Логотип-лепесток (HANDOFF §8). Единственное растровое пятно интерфейса:
 * не растягиваем, не перекрашиваем, keep `object-fit: contain`.
 */
export function BrandMark({ size = 30, className }: BrandMarkProps) {
  const style: CSSProperties = { width: size, height: size };
  return (
    <img
      className={className ? `brandmark ${className}` : 'brandmark'}
      src={`${BASE}logo-256.png`}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      decoding="async"
      draggable={false}
      style={style}
    />
  );
}
