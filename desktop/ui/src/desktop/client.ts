import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

/** All UI access to native services crosses this boundary. */
export const desktopClient = { invoke, listen };
