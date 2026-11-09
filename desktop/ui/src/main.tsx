import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { DesktopChatApp } from './desktop-chat/DesktopChatApp';
import './style.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DesktopChatApp />
  </StrictMode>,
);
