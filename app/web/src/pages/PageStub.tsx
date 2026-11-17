import type { IconName } from '../components/Icon';
import { Icon } from '../components/Icon';

interface PageStubProps {
  title: string;
  note: string;
  icon: IconName;
}

/**
 * Осознанная заглушка: у каждого раздела свой файл-компонент,
 * заголовок и явный текст «Раздел в работе» вместо пустых страниц.
 */
export function PageStub({ title, note, icon }: PageStubProps) {
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="page-head__kicker">Life OS · v3</p>
          <h1 className="page-head__title">{title}</h1>
          <p className="page-head__sub">{note}</p>
        </div>
        <span className="status-pill">
          <span className="status-pill__dot" aria-hidden="true" />
          Раздел в работе
        </span>
      </header>

      <section className="card stub" aria-labelledby="stub-title">
        <div className="stub__art" aria-hidden="true">
          <span className="stub__icon">
            <Icon name={icon} size={30} />
          </span>
          <span className="stub__ring stub__ring--one" />
          <span className="stub__ring stub__ring--two" />
        </div>

        <h2 className="stub__title" id="stub-title">
          Раздел в работе
        </h2>
        <p className="stub__text">
          Экран «{title}» появится в следующих фазах. Сначала подключим API Ф4
          (<code>/api/overview</code> и тематические роутеры), затем соберём графики, формы и плеер.
        </p>

        <ul className="stub__list">
          <li>
            <Icon name="check" size={15} />
            <span>Маршрут, заголовок и активный пункт меню уже работают</span>
          </li>
          <li>
            <Icon name="check" size={15} />
            <span>Раздел находится через ⌘K и мобильную навигацию</span>
          </li>
          <li>
            <Icon name="check" size={15} />
            <span>Светлая и тёмная темы, размеры 1440/1280/768/390 учтены</span>
          </li>
        </ul>

        <a className="btn btn--ghost stub__back" href="#/">
          <Icon name="home" size={16} />
          <span>На Обзор</span>
        </a>
      </section>
    </div>
  );
}
