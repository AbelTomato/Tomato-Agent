import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ConversationShell } from './components/ConversationShell';
import { DesktopProbe, isDesktop } from './components/DesktopProbe';
import './style.css';

createRoot(document.getElementById('root')!).render(<StrictMode>{isDesktop() ? <DesktopProbe /> : <ConversationShell />}</StrictMode>);
