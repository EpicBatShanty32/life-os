import type { NavBadges } from '../api/types';
import { MOBILE_NAV } from '../nav';
import { Icon } from './Icon';

interface MobileNavProps {
  path: string;
  badges: NavBadges | null;
}

export function MobileNav({ path, badges }: MobileNavProps) {
  return (
    <nav className="mobile-nav" aria-label="Главные разделы">
      {MOBILE_NAV.map((item) => {
        const active = path === item.path;
        const badge = item.badge && badges ? badges[item.badge] : 0;
        return (
          <a
            key={item.path}
            href={`#${item.path}`}
            className={`mobile-nav__item ${active ? 'is-active' : ''}`}
            aria-current={active ? 'page' : undefined}
            aria-label={badge ? `${item.label}, ${badge} новых` : item.label}
          >
            <span className="mobile-nav__icon">
              <Icon name={item.icon} size={21} />
              {badge > 0 ? (
                <span className="mobile-nav__badge" aria-hidden="true">
                  {badge}
                </span>
              ) : null}
            </span>
            <span className="mobile-nav__label">{item.label}</span>
          </a>
        );
      })}
    </nav>
  );
}
