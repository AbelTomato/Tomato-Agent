import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ConversationShell } from './components/ConversationShell';
import { isDesktop } from './components/DesktopProbe';
import { DesktopConnection } from './components/DesktopConnection';
import { DesktopChatApp } from './desktop-chat/DesktopChatApp';
import './style.css';

const desktopChatRoute = import.meta.env.MODE === 'desktop-chat'
  || new URLSearchParams(window.location.search).get('desktop-chat') === '1';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {desktopChatRoute ? <DesktopChatApp /> : isDesktop() ? <DesktopConnection /> : <ConversationShell />}
  </StrictMode>,
);
