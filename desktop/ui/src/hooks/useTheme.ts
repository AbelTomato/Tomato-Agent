import { useEffect, useState } from 'react';

type Theme = 'light' | 'dark' | 'auto';

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem('theme');
    return (stored === 'light' || stored === 'dark' || stored === 'auto')
      ? stored
      : 'auto';
  });
  const [resolvedTheme, setResolvedTheme] = useState<'light' | 'dark'>(() => {
    const stored = localStorage.getItem('theme');
    const preference = stored === 'light' || stored === 'dark' || stored === 'auto' ? stored : 'auto';
    return preference === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : preference === 'dark' ? 'dark' : 'light';
  });

  useEffect(() => {
    const root = document.documentElement;
    const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
    const applyTheme = () => {
      const resolved = theme === 'auto' ? (systemTheme.matches ? 'dark' : 'light') : theme;
      setResolvedTheme(resolved);
      root.setAttribute('data-theme', resolved);
      root.style.colorScheme = resolved;
    };
    applyTheme();
    systemTheme.addEventListener('change', applyTheme);
    localStorage.setItem('theme', theme);
    return () => systemTheme.removeEventListener('change', applyTheme);
  }, [theme]);

  return { theme, resolvedTheme, setTheme };
}
