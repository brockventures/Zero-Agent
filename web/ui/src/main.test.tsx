import { beforeEach, describe, expect, it, vi } from 'vitest';

describe('main entrypoint', () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = '';
  });

  it('throws when #root container is missing', async () => {
    await expect(import('./main')).rejects.toThrow(
      'mirrormere: #root container missing from display_react.html',
    );
  });

  it('initializes runtime and mounts App into #root when container exists', async () => {
    const root = document.createElement('div');
    root.id = 'root';
    document.body.appendChild(root);

    const startRuntimeMock = vi.fn();
    const renderMock = vi.fn();
    const createRootMock = vi.fn().mockReturnValue({ render: renderMock });

    vi.doMock('./runtime', () => ({
      startRuntime: startRuntimeMock,
    }));
    vi.doMock('react-dom/client', () => ({
      createRoot: createRootMock,
    }));

    await import('./main');

    expect(startRuntimeMock).toHaveBeenCalledOnce();
    expect(createRootMock).toHaveBeenCalledWith(root);
    expect(renderMock).toHaveBeenCalledOnce();
  });
});
