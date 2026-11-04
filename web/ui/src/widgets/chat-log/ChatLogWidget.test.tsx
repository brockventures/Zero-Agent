import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import sampleData from '../../../../../widgets/chat-log/testdata/sample.json';
import { useDisplayStore } from '../../store';
import { ChatLogWidget } from './ChatLogWidget';

const initialStore = useDisplayStore.getState();

describe('ChatLogWidget', () => {
  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
  });

  afterEach(() => {
    cleanup();
  });

  it('renders messages with sample.json data matching legacy widget.html', () => {
    useDisplayStore.setState({
      widgets: {
        'chat-1': {
          meta: {
            type: 'chat-log',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: sampleData,
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<ChatLogWidget id="chat-1" type="chat-log" />);
    const root = container.querySelector('.widget-chat-log') as HTMLElement;
    expect(root).not.toBeNull();

    // Session from data.session
    const session = root.querySelector('.chat-log-session');
    expect(session?.textContent).toBe('Kitchen assistant');

    // Updated at timestamp
    const updated = root.querySelector('.chat-log-updated');
    expect(updated?.textContent).toBe('18:42');

    // Messages container
    const messagesContainer = root.querySelector('.chat-log-messages');
    expect(messagesContainer).not.toBeNull();

    const messages = root.querySelectorAll('.chat-log-message');
    expect(messages.length).toBe(4);

    // Message 0: human
    expect(messages[0].classList.contains('chat-log-human')).toBe(true);
    expect(messages[0].querySelector('.chat-log-author')?.textContent).toBe('sam');
    expect(messages[0].querySelector('.chat-log-ts')?.textContent).toBe(' 18:39');
    expect(messages[0].querySelector('.chat-log-text')?.textContent).toBe(
      'What is on the calendar tomorrow?'
    );

    // Message 1: agent
    expect(messages[1].classList.contains('chat-log-agent')).toBe(true);
    expect(messages[1].querySelector('.chat-log-author')?.textContent).toBe('assistant');
    expect(messages[1].querySelector('.chat-log-ts')?.textContent).toBe(' 18:39');
    expect(messages[1].querySelector('.chat-log-text')?.textContent).toBe(
      'Dentist at 9:00 and soccer practice at 4:30.'
    );

    // Message 2: human
    expect(messages[2].classList.contains('chat-log-human')).toBe(true);
    expect(messages[2].querySelector('.chat-log-author')?.textContent).toBe('sam');
    expect(messages[2].querySelector('.chat-log-ts')?.textContent).toBe(' 18:41');
    expect(messages[2].querySelector('.chat-log-text')?.textContent).toBe(
      'Add milk and eggs to the shopping list.'
    );

    // Message 3: agent
    expect(messages[3].classList.contains('chat-log-agent')).toBe(true);
    expect(messages[3].querySelector('.chat-log-author')?.textContent).toBe('assistant');
    expect(messages[3].querySelector('.chat-log-ts')?.textContent).toBe(' 18:42');
    expect(messages[3].querySelector('.chat-log-text')?.textContent).toBe(
      'Done. Milk and eggs are on the shopping list.'
    );
  });

  it('renders empty state on unhydrated data: {} and empty messages: []', () => {
    // Unhydrated data: {}
    useDisplayStore.setState({
      widgets: {
        'chat-unhydrated': {
          state: 'healthy',
          data: {},
          timestamp: '',
        },
      },
    });

    const { container: c1 } = render(<ChatLogWidget id="chat-unhydrated" type="chat-log" />);
    const empty1 = c1.querySelector('.chat-log-empty');
    expect(empty1).not.toBeNull();
    expect(empty1?.textContent).toBe('No messages yet');
    expect(c1.querySelector('.chat-log-messages')).toBeNull();
    expect(c1.querySelector('.chat-log-header')).toBeNull();

    // Empty array messages: []
    useDisplayStore.setState({
      widgets: {
        'chat-empty': {
          state: 'healthy',
          data: { messages: [] },
          timestamp: '',
        },
      },
    });

    const { container: c2 } = render(<ChatLogWidget id="chat-empty" type="chat-log" />);
    const empty2 = c2.querySelector('.chat-log-empty');
    expect(empty2).not.toBeNull();
    expect(empty2?.textContent).toBe('No messages yet');
    expect(c2.querySelector('.chat-log-messages')).toBeNull();
  });

  it('falls back session to config.title and then Chat', () => {
    // Falls back to config.title
    useDisplayStore.setState({
      widgets: {
        'chat-title': {
          meta: {
            type: 'chat-log',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { title: 'Living Room Chat' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            messages: [{ role: 'human', text: 'Hello' }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c1 } = render(<ChatLogWidget id="chat-title" type="chat-log" />);
    expect(c1.querySelector('.chat-log-session')?.textContent).toBe('Living Room Chat');
    expect(c1.querySelector('.chat-log-updated')).toBeNull();

    // Falls back to default 'Chat'
    useDisplayStore.setState({
      widgets: {
        'chat-default': {
          meta: {
            type: 'chat-log',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            messages: [{ role: 'agent', text: 'Ready' }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c2 } = render(<ChatLogWidget id="chat-default" type="chat-log" />);
    expect(c2.querySelector('.chat-log-session')?.textContent).toBe('Chat');
  });

  it('falls back author to role when author is omitted', () => {
    useDisplayStore.setState({
      widgets: {
        'chat-no-author': {
          state: 'healthy',
          data: {
            messages: [
              { role: 'human', text: 'Test query' },
              { role: 'agent', text: 'Test response' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<ChatLogWidget id="chat-no-author" type="chat-log" />);
    const authors = container.querySelectorAll('.chat-log-author');
    expect(authors[0]?.textContent).toBe('human');
    expect(authors[1]?.textContent).toBe('agent');
  });

  it('renders widget-error-banner when state is error', () => {
    useDisplayStore.setState({
      widgets: {
        'chat-error': {
          state: 'error',
          data: {},
          timestamp: '',
        },
      },
    });

    const { container } = render(<ChatLogWidget id="chat-error" type="chat-log" />);
    const banner = container.querySelector('.widget-error-banner');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('⚠');
    expect(banner?.textContent).toContain('Widget Error');
  });

  it('escapes HTML and script tags in text safely', () => {
    const maliciousText = '<script>window.__pwned = true;</script><b>Hello</b><img src="x" onerror="alert(1)"/>';

    useDisplayStore.setState({
      widgets: {
        'chat-xss': {
          state: 'healthy',
          data: {
            messages: [
              {
                role: 'human',
                author: '<evil>',
                text: maliciousText,
              },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<ChatLogWidget id="chat-xss" type="chat-log" />);
    // Script or img elements must NOT exist in the DOM
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();

    // Text content must contain the exact raw string safely rendered
    const textEl = container.querySelector('.chat-log-text');
    expect(textEl?.textContent).toBe(maliciousText);

    const authorEl = container.querySelector('.chat-log-author');
    expect(authorEl?.textContent).toBe('<evil>');
  });

  it('handles non-object messages, missing authors/roles, and PascalCase fields', () => {
    useDisplayStore.setState({
      widgets: {
        'chat-edge': {
          state: 'healthy',
          data: {
            UpdatedAt: '2026-10-31T14:30:00Z',
            Messages: [
              null,
              'invalid-primitive',
              { Text: 'No author or role', time: '' },
              { role: 'human', author: '   ', text: 'Whitespace author' },
            ],
          },
          timestamp: '2026-10-31T14:30:00Z',
        },
      },
    });

    const { container } = render(<ChatLogWidget id="chat-edge" type="chat-log" />);
    const messages = container.querySelectorAll('.chat-log-message');
    expect(messages.length).toBe(2);

    // First valid message: role defaults to agent, no ts
    expect(messages[0].classList.contains('chat-log-agent')).toBe(true);
    expect(messages[0].querySelector('.chat-log-author')?.textContent).toBe('agent');
    expect(messages[0].querySelector('.chat-log-ts')).toBeNull();

    // Second valid message: author is whitespace so falls back to human
    expect(messages[1].classList.contains('chat-log-human')).toBe(true);
    expect(messages[1].querySelector('.chat-log-author')?.textContent).toBe('human');
  });

  it('handles config.Title, data.updatedAt with raw fallback, and non-date message timestamps', () => {
    useDisplayStore.setState({
      widgets: {
        'chat-title-fallback': {
          meta: {
            type: 'chat-log',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { Title: 'Kitchen Channel' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            updatedAt: 'raw-updated-string',
            messages: [
              {
                role: 'system',
                Timestamp: 'raw-timestamp-string',
                text: 'System alert message',
              },
              {
                role: 'assistant',
                timestamp: '2026-10-31T15:00:00Z',
                text: 'Assistant response',
              },
            ],
          },
          timestamp: '2026-10-31T15:00:00Z',
        },
      },
    });

    const { container } = render(<ChatLogWidget id="chat-title-fallback" type="chat-log" />);
    expect(container.querySelector('.chat-log-session')?.textContent).toBe('Kitchen Channel');
    expect(container.querySelector('.chat-log-updated')?.textContent).toBe('raw-updated-string');

    const messages = container.querySelectorAll('.chat-log-message');
    expect(messages.length).toBe(2);
    expect(messages[0].querySelector('.chat-log-ts')?.textContent).toContain('raw-timestamp-string');
    expect(messages[0].querySelector('.chat-log-author')?.textContent).toBe('agent');
    expect(messages[1].querySelector('.chat-log-ts')?.textContent).toContain('15:00');
  });
});
