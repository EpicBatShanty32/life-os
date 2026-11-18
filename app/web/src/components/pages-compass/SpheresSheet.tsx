import { useEffect, useState } from 'react';
import type { CSSProperties, FormEvent } from 'react';
import {
  createSphere,
  deleteSphere,
  fetchSpheres,
  patchSphere,
} from '../../api/compass';
import type { Sphere } from '../../api/compass';
import { apiErrorMessage } from '../../api/errors';
import { toast } from '../../lib/toast';
import { Button, Input, Sheet, Skeleton } from '../ui';
import { InlineError, SphereDot, useTokenColors } from './shared';

export interface SpheresSheetProps {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}

function sameColor(left: string, right: string): boolean {
  return left.trim().toLowerCase() === right.trim().toLowerCase();
}

function uniqueColors(items: Sphere[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of items) {
    const color = item.color.trim();
    if (color && !seen.has(color.toLowerCase())) {
      seen.add(color.toLowerCase());
      result.push(color);
    }
  }
  return result;
}

function SphereRow({
  sphere,
  palette,
  onSaved,
  onRemoved,
}: {
  sphere: Sphere;
  palette: string[];
  onSaved: (sphere: Sphere) => void;
  onRemoved: (id: number) => void;
}) {
  const [name, setName] = useState(sphere.name);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setName(sphere.name);
  }, [sphere.name]);

  const dirty = name.trim() !== sphere.name && name.trim() !== '';

  async function saveName(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!dirty || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await patchSphere(sphere.id, { name: name.trim() });
      onSaved(saved);
    } catch (problem: unknown) {
      setError(apiErrorMessage(problem, 'Не удалось переименовать сферу'));
    } finally {
      setSaving(false);
    }
  }

  async function setColor(color: string) {
    if (sameColor(color, sphere.color)) return;
    try {
      const saved = await patchSphere(sphere.id, { color });
      onSaved(saved);
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось изменить цвет'), 'error');
    }
  }

  async function toggleArchive() {
    try {
      const saved = await patchSphere(sphere.id, { archived: !sphere.archived });
      onSaved(saved);
      toast(saved.archived ? 'Сфера в архиве' : 'Сфера возвращена', 'default');
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось изменить архив'), 'error');
    }
  }

  async function remove() {
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    try {
      const result = await deleteSphere(sphere.id);
      if (result.archived) {
        toast(
          `Сфера используется (${Object.values(result.usage).reduce((sum, value) => sum + value, 0)} записей) — перенесена в архив`,
          'default',
        );
        onSaved({ ...sphere, archived: true });
      } else {
        toast('Сфера удалена', 'default');
        onRemoved(sphere.id);
      }
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось удалить сферу'), 'error');
    }
  }

  return (
    <li className={sphere.archived ? 'pc-sphere-row is-archived' : 'pc-sphere-row'}>
      <SphereDot color={sphere.color} />
      <form className="pc-sphere-row__name" onSubmit={saveName}>
        <Input
          size="sm"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setError(null);
          }}
          aria-label={`Название сферы ${sphere.name}`}
          maxLength={120}
        />
        {dirty ? (
          <Button type="submit" size="sm" variant="secondary" loading={saving}>
            ОК
          </Button>
        ) : null}
      </form>
      <div className="pc-swatches" role="group" aria-label={`Цвет сферы ${sphere.name}`}>
        {palette.map((color, index) => (
          <button
            key={color}
            type="button"
            className="pc-swatch"
            style={{ '--pc-sphere': color } as CSSProperties}
            aria-label={`Цвет ${index + 1}`}
            aria-pressed={sameColor(color, sphere.color)}
            title={`Цвет ${index + 1}`}
            onClick={() => {
              void setColor(color);
            }}
          />
        ))}
      </div>
      <div className="pc-sphere-row__actions">
        <Button size="sm" variant="ghost" onClick={() => void toggleArchive()}>
          {sphere.archived ? 'Вернуть' : 'В архив'}
        </Button>
        <Button
          size="sm"
          variant={confirmDelete ? 'danger' : 'ghost'}
          onClick={() => void remove()}
          onBlur={() => setConfirmDelete(false)}
          title={confirmDelete ? 'Нажмите ещё раз, чтобы удалить' : 'Удалить сферу'}
        >
          {confirmDelete ? 'Точно?' : 'Удалить'}
        </Button>
      </div>
      {error ? <p className="pc-sphere-row__error">{error}</p> : null}
    </li>
  );
}

export function SpheresSheet({ open, onClose, onChanged }: SpheresSheetProps) {
  const [items, setItems] = useState<Sphere[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState('');
  const [newColor, setNewColor] = useState('');
  const [creating, setCreating] = useState(false);
  const colors = useTokenColors();
  const palette = colors.length > 0 ? colors : uniqueColors(items);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    setError(null);
    fetchSpheres('all')
      .then((list) => {
        if (alive) setItems(list);
      })
      .catch((problem: unknown) => {
        if (alive) setError(apiErrorMessage(problem, 'Не удалось загрузить сферы'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open]);

  useEffect(() => {
    if (!newColor) setNewColor(palette[0] ?? '');
  }, [palette, newColor]);

  function replace(sphere: Sphere) {
    setItems((previous) => previous.map((item) => (item.id === sphere.id ? sphere : item)));
    onChanged();
  }

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    try {
      const created = await createSphere({ name, color: newColor || undefined });
      setItems((previous) => [...previous, created]);
      setNewName('');
      onChanged();
      toast('Сфера добавлена', 'default');
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось добавить сферу'), 'error');
    } finally {
      setCreating(false);
    }
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Сферы жизни"
      description="Пять маркеров палитры. Цвет — только точка в списках."
      size="wide"
    >
      <form className="pc-spheres__add" onSubmit={add}>
        <Input
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
          placeholder="Название сферы"
          aria-label="Название новой сферы"
          maxLength={120}
        />
        <div className="pc-swatches" role="group" aria-label="Цвет новой сферы">
          {palette.map((color, index) => (
            <button
              key={color}
              type="button"
              className="pc-swatch"
              style={{ '--pc-sphere': color } as CSSProperties}
              aria-label={`Цвет ${index + 1}`}
              aria-pressed={sameColor(color, newColor)}
              onClick={() => setNewColor(color)}
            />
          ))}
        </div>
        <Button type="submit" size="sm" variant="secondary" loading={creating} disabled={!newName.trim()}>
          Добавить
        </Button>
      </form>

      {loading && items.length === 0 ? (
        <Skeleton variant="row" label="Загрузка сфер" />
      ) : error ? (
        <InlineError text={error} />
      ) : (
        <ul className="pc-spheres">
          {items.map((sphere) => (
            <SphereRow
              key={sphere.id}
              sphere={sphere}
              palette={palette}
              onSaved={replace}
              onRemoved={(id) => {
                setItems((previous) => previous.filter((item) => item.id !== id));
                onChanged();
              }}
            />
          ))}
        </ul>
      )}
      {items.length === 0 && !loading && !error ? (
        <p className="pc-muted">Сфер пока нет. Назовите первую — она станет маркером цели.</p>
      ) : null}
    </Sheet>
  );
}
