import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ConversationShell } from './components/ConversationShell';
import { isDesktop } from './components/DesktopProbe';
import { DesktopConnection } from './components/DesktopConnection';
import './style.css';

createRoot(document.getElementById('root')!).render(<StrictMode>{isDesktop() ? <DesktopConnection /> : <ConversationShell />}</StrictMode>);
