import { useSystem } from '../store/hooks';

export function StatusDot() {
  const system = useSystem();

  let statusClass = 'status-ok';
  let title = 'System Status: Healthy';
  let visible = false;

  if (system.online === false) {
    statusClass = 'status-error';
    title = 'System Status: Offline';
    visible = true;
  } else if (system.configStatus === 'error') {
    statusClass = 'status-degraded';
    title = `Configuration Degraded: ${system.configError || 'Error'}`;
    visible = true;
  }

  return (
    <div className="header-status" id="header-status" style={{ display: visible ? '' : 'none' }}>
      <div className="status-jewel" title={title}>
        <span className={`status-dot ${statusClass}`} id="system-status-dot" title={title} />
      </div>
    </div>
  );
}
