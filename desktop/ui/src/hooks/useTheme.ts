import { useEffect, useState } from 'react';

type Theme = 'light' | 'dark' | 'auto';

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem('theme');
    return (stored === 'light' || stored === 'dark' || stored === 'auto')
      ? stored
      : 'auto';
  });

  useEffect(() => {
    const root = document.documentElement;
    
    if (theme === 'auto') {
      // Remove explicit theme, let CSS light-dark() handle it
      root.removeAttribute('data-theme');
    } else {
      // Set explicit theme
      root.setAttribute('data-theme', theme);
    }
    
    localStorage.setItem('theme', theme);
  }, [theme]);

  return { theme, setTheme };
}
