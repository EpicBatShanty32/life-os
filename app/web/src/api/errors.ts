/** Разбор ошибок apiRequest без изменения общего клиента. */

export function apiStatus(error: unknown): number | null {
  if (!(error instanceof Error)) return null;
  const match = /HTTP\s+(\d{3})/.exec(error.message);
  if (!match) return null;
  const status = Number(match[1]);
  return Number.isFinite(status) ? status : null;
}

/** Человеческая строка для тоста/строки блока. */
export function apiErrorMessage(error: unknown, fallback = 'Не удалось выполнить действие'): string {
  if (!(error instanceof Error)) return fallback;
  if (/abort/i.test(error.message)) return 'Сервер не ответил — попробуйте ещё раз';

  const status = apiStatus(error);
  if (status === 401 || status === 403) return 'Нужен доступ владельца';
  if (status === 404) return 'Запись не найдена — возможно, её уже удалили';
  if (status === 409) return 'Запись уже существует или занята';
  if (status === 422) return 'Проверьте заполнение полей';
  if (status !== null && status >= 500) return 'Ошибка сервера — попробуйте позже';
  return fallback;
}
