export function NotFound() {
  return (
    <div className="page">
      <header className="page-head">
        <div className="page-head__text">
          <h1 className="page-head__title">Страница не найдена</h1>
          <p className="page-head__sub">Такого раздела нет — возможно, ссылка устарела.</p>
        </div>
      </header>

      <section className="card stub" aria-labelledby="not-found-title">
        <div className="stub__art" aria-hidden="true">
          <span className="stub__icon">
            <span className="stub__code">404</span>
          </span>
        </div>
        <h2 className="stub__title" id="not-found-title">
          Проверьте адрес
        </h2>
        <p className="stub__text">
          Доступные разделы: Обзор, Сегодня, Цели, Привычки, Тело и весы, Сон, Тренировки,
          Питание, Курсы, Журнал, Настроение, Аналитика и Настройки. Открыть любой из них
          можно через поиск — ⌘K.
        </p>
        <a className="btn btn--primary stub__back" href="#/">
          <span>На Обзор</span>
          <span aria-hidden="true">→</span>
        </a>
      </section>
    </div>
  );
}
