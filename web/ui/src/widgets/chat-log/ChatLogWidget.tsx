import './ChatLogWidget.css';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import { formatTime } from '../format';

/**
 * Native React widget component for chat-log voice history and conversation transcripts.
 * Ports widgets/chat-log/views/widget.html.
 */
export function ChatLogWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const data = entry?.data as Record<string, unknown> | undefined;

  const rawSession = data?.session ?? data?.Session;
  const session =
    typeof rawSession === 'string' && rawSession.trim() !== ''
      ? rawSession.trim()
      : undefined;

  const rawTitle = config?.title ?? config?.Title;
  const title =
    typeof rawTitle === 'string' && rawTitle.trim() !== ''
      ? rawTitle.trim()
      : undefined;

  const sessionDisplay = session || title || 'Chat';

  const rawUpdatedAt =
    data?.updated_at ?? data?.updatedAt ?? data?.UpdatedAt;
  const updatedAt =
    rawUpdatedAt !== undefined && rawUpdatedAt !== null && String(rawUpdatedAt).trim() !== ''
      ? String(rawUpdatedAt).trim()
      : '';
  const formattedUpdated = updatedAt
    ? (formatTime('15:04', updatedAt) || updatedAt)
    : null;

  const isError = entry?.state === 'error';

  const rawMessages = Array.isArray(data?.messages)
    ? data.messages
    : Array.isArray(data?.Messages)
      ? data.Messages
      : [];

  return (
    <div className="widget-chat-log">
      {isError && (
        <div className="widget-error-banner">
          <span className="error-icon">⚠</span>
          <span>Widget Error</span>
        </div>
      )}

      {rawMessages.length > 0 ? (
        <>
          <div className="chat-log-header">
            <span className="chat-log-session">{sessionDisplay}</span>
            {formattedUpdated ? (
              <span className="chat-log-updated">{formattedUpdated}</span>
            ) : null}
          </div>
          <div className="chat-log-messages">
            {rawMessages.map((item, i) => {
              if (!item || typeof item !== 'object') return null;
              const m = item as Record<string, unknown>;
              const role = String(m.role ?? m.Role ?? '').toLowerCase();
              const rawAuthor = m.author ?? m.Author;
              const author =
                typeof rawAuthor === 'string' && rawAuthor.trim() !== ''
                  ? rawAuthor.trim()
                  : undefined;
              const authorDisplay = author || (role === 'human' ? 'human' : 'agent');

              const rawTs = m.ts ?? m.timestamp ?? m.Timestamp ?? m.time;
              const ts =
                rawTs !== undefined && rawTs !== null && String(rawTs).trim() !== ''
                  ? String(rawTs).trim()
                  : '';
              const formattedTs = ts ? (formatTime('15:04', ts) || ts) : null;

              const text = String(m.text ?? m.Text ?? '');
              const itemKey = String(
                m.id ?? m.ID ?? `${ts || i}:${role}:${authorDisplay}:${text.slice(0, 32)}`,
              );

              return (
                <div
                  key={itemKey}
                  className={`chat-log-message chat-log-${role === 'human' ? 'human' : 'agent'}`}
                >
                  <div className="chat-log-meta">
                    <span className="chat-log-author">{authorDisplay}</span>
                    {formattedTs ? (
                      <span className="chat-log-ts"> {formattedTs}</span>
                    ) : null}
                  </div>
                  <div className="chat-log-text">{text}</div>
                </div>
              );
            })}
          </div>
        </>
      ) : (
        <div className="chat-log-empty">
          <span>No messages yet</span>
        </div>
      )}
    </div>
  );
}
