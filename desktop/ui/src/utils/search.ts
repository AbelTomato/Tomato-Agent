import { useState, useEffect } from 'react';

// 会话搜索结果类型
export interface SearchResult {
  sessionId: string;
  sessionTitle: string;
  matches: {
    messageId: string;
    content: string;
    highlightedContent: string;
  }[];
}

// 搜索会话
export function searchSessions(
  sessions: Array<{ id: string; title: string; messages: Array<{ id: string; content: string }> }>,
  query: string
): SearchResult[] {
  if (!query.trim()) {
    return [];
  }

  const lowerQuery = query.toLowerCase();
  const results: SearchResult[] = [];

  sessions.forEach(session => {
    const titleMatch = session.title.toLowerCase().includes(lowerQuery);
    const messageMatches = session.messages
      .filter(msg => msg.content.toLowerCase().includes(lowerQuery))
      .map(msg => ({
        messageId: msg.id,
        content: msg.content,
        highlightedContent: highlightMatch(msg.content, query),
      }));

    if (titleMatch || messageMatches.length > 0) {
      results.push({
        sessionId: session.id,
        sessionTitle: session.title,
        matches: messageMatches,
      });
    }
  });

  return results;
}

// 高亮匹配文本
export function highlightMatch(text: string, query: string): string {
  if (!query.trim()) {
    return text;
  }

  const regex = new RegExp(`(${escapeRegex(query)})`, 'gi');
  return text.replace(regex, '<mark>$1</mark>');
}

// 转义正则表达式特殊字符
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// 防抖Hook
export function useDebounce<T>(value: T, delay: number = 300): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);

  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => {
      clearTimeout(handler);
    };
  }, [value, delay]);

  return debouncedValue;
}

// 搜索历史管理
const SEARCH_HISTORY_KEY = 'tomato-agent-search-history';
const MAX_HISTORY_SIZE = 10;

export function getSearchHistory(): string[] {
  try {
    const history = localStorage.getItem(SEARCH_HISTORY_KEY);
    return history ? JSON.parse(history) : [];
  } catch {
    return [];
  }
}

export function saveSearchHistory(query: string): void {
  if (!query.trim()) {
    return;
  }

  try {
    const history = getSearchHistory();
    const filtered = history.filter(item => item !== query);
    const newHistory = [query, ...filtered].slice(0, MAX_HISTORY_SIZE);
    localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(newHistory));
  } catch (error) {
    console.error('Failed to save search history:', error);
  }
}

export function clearSearchHistory(): void {
  try {
    localStorage.removeItem(SEARCH_HISTORY_KEY);
  } catch (error) {
    console.error('Failed to clear search history:', error);
  }
}
