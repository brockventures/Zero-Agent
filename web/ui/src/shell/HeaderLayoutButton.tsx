import './HeaderLayoutButton.css';
import { useEffect, useState } from 'react';
import { fetchLayoutEditor } from '../editor/api';
import { useDisplayStore } from '../store';

export interface HeaderLayoutButtonProps {
  fetchImpl?: typeof fetch;
}

export function HeaderLayoutButton({ fetchImpl }: HeaderLayoutButtonProps = {}) {
  const [allowed, setAllowed] = useState(false);

  useEffect(() => {
    let active = true;
    fetchLayoutEditor(fetchImpl)
      .then((res) => {
        if (active) {
          setAllowed(Boolean(res.allow_layout_edit));
        }
      })
      .catch((err: unknown) => {
        console.warn('[MirrormereDisplay] fetch layout editor failed:', err);
        if (active) {
          setAllowed(false);
        }
      });
    return () => {
      active = false;
    };
  }, [fetchImpl]);

  if (!allowed) {
    return null;
  }

  return (
    <div className="header-layout" id="header-layout">
      <button
        id="header-layout-btn"
        className="header-layout-btn"
        type="button"
        aria-label="Edit Layout"
        title="Edit Layout"
        onClick={() => {
          useDisplayStore.getState().setLayoutEditorOpen(true);
        }}
      >
        <svg
          className="header-layout-icon"
          width="24"
          height="24"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
        </svg>
      </button>
    </div>
  );
}
