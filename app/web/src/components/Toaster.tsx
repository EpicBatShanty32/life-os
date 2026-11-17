import { createPortal } from 'react-dom';
import { useToasts } from '../lib/toast';
import { Icon } from './Icon';

export function Toaster() {
  const items = useToasts();

  return createPortal(
    <div className="toaster" role="status" aria-live="polite" aria-atomic="false">
      {items.map((item) => (
        <div key={item.id} className={`toast toast--${item.tone}`}>
          <span className="toast__icon" aria-hidden="true">
            <Icon
              name={item.tone === 'success' ? 'check-circle' : item.tone === 'error' ? 'close' : 'sparkles'}
              size={17}
            />
          </span>
          <span className="toast__text">{item.text}</span>
        </div>
      ))}
    </div>,
    document.body,
  );
}
