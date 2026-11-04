import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { startRuntime } from './runtime';
import './styles.css';
import './styles/overrides.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('mirrormere: #root container missing from display_react.html');
}

// Start the event stream once, outside React. Starting it from an effect would
// open a second EventSource under StrictMode's double-invoked effects.
startRuntime();

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
