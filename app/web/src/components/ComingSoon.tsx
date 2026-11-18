import type { RouteDef } from '../lib/router';
import { navigate } from '../lib/router';
import { navGroupForPath } from '../nav';
import { Button, EmptyState } from './ui';

/**
 * Заглушка для маршрутов, объявленных в навигации v4, но ещё без экрана.
 * Честно говорит, что страницы нет, вместо «404» в свежем меню.
 */
export function ComingSoon({ route }: { route: RouteDef }) {
  const group = navGroupForPath(route.path);
  return (
    <div className="page">
      <header className="page-head">
        <div className="page-head__text">
          {group ? <p className="page-head__kicker">{group.title}</p> : null}
          <h1 className="page-head__title">{route.title}</h1>
          {route.note ? <p className="page-head__sub">{route.note}</p> : null}
        </div>
      </header>

      <EmptyState
        title="Экран ещё не собран"
        description="Маршрут и место в меню готовы, страница появится вместе с данными раздела."
        action={
          <Button variant="secondary" size="sm" onClick={() => navigate('/')}>
            Вернуться на Обзор
          </Button>
        }
      />
    </div>
  );
}
