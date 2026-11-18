import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import '../styles/pages-compass.css';
import { useInbox } from '../api/tasks';
import type { InboxConvertTo, InboxItem } from '../api/tasks';
import { formatRelative, pluralRu } from '../lib/format';
import { toast } from '../lib/toast';
import {
  Button,
  EmptyState,
  Icon,
  Menu,
  Section,
} from '../components/ui';
import {
  InlineError,
  PageHead,
  SkeletonRows,
  longToday,
  todayIso,
} from '../components/pages-compass/shared';

export function Inbox() {
  const inbox = useInbox();
  const [text, setText] = useState('');
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const items = inbox.data?.items ?? [];
  const count = items.length;

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = text.trim();
    if (!value || saving) return;
    setSaving(true);
    const ok = await inbox.add(value);
    setSaving(false);
    if (ok) setText('');
    inputRef.current?.focus();
  }

  async function convert(item: InboxItem, to: InboxConvertTo, payload?: Record<string, unknown>) {
    const labels: Record<InboxConvertTo, string> = {
      task: 'Перенесено в задачи на сегодня',
      personal: 'Перенесено в личные задачи',
      project: 'Создан проект',
      book: 'Добавлено в библиотеку',
      delete: 'Удалено',
    };
    const ok = await inbox.convert(item, to, payload);
    if (ok) toast(labels[to], 'success');
  }

  function menuItems(item: InboxItem) {
    return [
      {
        id: 'task',
        label: 'В задачи на сегодня',
        icon: 'check' as const,
        onSelect: () => {
          void convert(item, 'task', { dueDate: todayIso() });
        },
      },
      {
        id: 'project',
        label: 'В проект',
        icon: 'briefcase' as const,
        onSelect: () => {
          void convert(item, 'project');
        },
      },
      {
        id: 'personal',
        label: 'В личное',
        icon: 'user' as const,
        onSelect: () => {
          void convert(item, 'personal');
        },
      },
      {
        id: 'book',
        label: 'В книги и идеи',
        icon: 'book' as const,
        onSelect: () => {
          void convert(item, 'book');
        },
      },
      {
        id: 'delete',
        label: 'Удалить',
        icon: 'trash' as const,
        danger: true,
        separatorBefore: true,
        onSelect: () => {
          void inbox.remove(item);
        },
      },
    ];
  }

  return (
    <div className="pc-page">
      <PageHead
        title="Инбокс"
        date={longToday()}
        note={
          count > 0
            ? `${count} ${pluralRu(count, ['запись', 'записи', 'записей'])} ${pluralRu(count, ['ждёт', 'ждут', 'ждут'])} разбора`
            : 'Всё разобрано'
        }
      />

      <Section
        title="Неразобранное"
        description="Одна строка — одна мысль. Разбирайте по одной, не всё сразу."
      >
        <form className="pc-inbox__add" onSubmit={add}>
          <Icon name="inbox" size={18} className="pc-inbox__icon" />
          <input
            ref={inputRef}
            id="inbox-new"
            className="ui-input pc-inbox__input"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Что нужно не забыть?"
            aria-label="Новая запись в инбокс"
            autoComplete="off"
            maxLength={2000}
          />
          <Button type="submit" size="sm" variant="secondary" loading={saving} disabled={!text.trim()}>
            Добавить
          </Button>
        </form>
        <p className="pc-inbox__hint">
          Enter — сохранить и остаться в поле. После разбора запись исчезает отсюда.
        </p>

        {inbox.loading && !inbox.data ? (
          <SkeletonRows rows={4} label="Загрузка инбокса" />
        ) : inbox.error ? (
          <InlineError text={inbox.error} onRetry={inbox.reload} />
        ) : count === 0 ? (
          <EmptyState
            title="Всё разобрано"
            description="Новая мысль попадёт сюда — просто начните печатать выше."
          />
        ) : (
          <ul className="pc-inbox">
            {items.map((item) => (
              <li className="pc-inbox__item" key={item.id}>
                <span className="pc-inbox__mark" aria-hidden="true" />
                <div className="pc-inbox__body">
                  <p className="pc-inbox__text">{item.text}</p>
                  <span className="pc-inbox__meta">{formatRelative(item.created)}</span>
                </div>
                <div className="pc-inbox__actions">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      void convert(item, 'task', { dueDate: todayIso() });
                    }}
                  >
                    В задачи
                  </Button>
                  <Menu
                    items={menuItems(item)}
                    label={`Разобрать: ${item.text.slice(0, 48)}`}
                    triggerClassName="pc-row-action"
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
