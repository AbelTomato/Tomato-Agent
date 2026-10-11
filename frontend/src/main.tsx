import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ConversationShell } from './components/ConversationShell';
import './style.css';

createRoot(document.getElementById('root')!).render(<StrictMode><ConversationShell /></StrictMode>);
