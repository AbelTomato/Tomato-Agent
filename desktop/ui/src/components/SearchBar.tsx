import { useState, useRef, useEffect, forwardRef, useImperativeHandle } from 'react';
import { Search, X } from 'lucide-react';
import { useDebounce } from '../utils/search';

interface SearchBarProps {
  onSearch: (query: string) => void;
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
}

export interface SearchBarHandle {
  focus: () => void;
  blur: () => void;
}

export const SearchBar = forwardRef<SearchBarHandle, SearchBarProps>(({
  onSearch,
  placeholder = '搜索会话...',
  className = '',
  autoFocus = false,
}, ref) => {
  const [query, setQuery] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const debouncedQuery = useDebounce(query, 300);

  // 暴露给父组件的方法
  useImperativeHandle(ref, () => ({
    focus: () => inputRef.current?.focus(),
    blur: () => inputRef.current?.blur(),
  }));

  // 触发搜索
  useEffect(() => {
    onSearch(debouncedQuery);
  }, [debouncedQuery, onSearch]);

  // 自动聚焦
  useEffect(() => {
    if (autoFocus && inputRef.current) {
      inputRef.current.focus();
    }
  }, [autoFocus]);

  // 清除搜索
  const handleClear = () => {
    setQuery('');
    inputRef.current?.focus();
  };

  // 键盘事件
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      handleClear();
      inputRef.current?.blur();
    }
  };

  return (
    <div className={`relative ${className}`}>
      <Search
        className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
        size={18}
      />
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => setIsFocused(true)}
        onBlur={() => setIsFocused(false)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className={`
          w-full pl-10 pr-10 py-2 
          border rounded-lg
          transition-all duration-200
          ${
            isFocused
              ? 'border-primary-500 ring-2 ring-primary-100 dark:ring-primary-900'
              : 'border-gray-300 dark:border-gray-600'
          }
          bg-white dark:bg-neutral-800
          text-gray-900 dark:text-gray-100
          placeholder-gray-400 dark:placeholder-gray-500
          focus:outline-none
        `}
      />
      {query && (
        <button
          onClick={handleClear}
          className="
            absolute right-3 top-1/2 -translate-y-1/2
            text-gray-400 hover:text-gray-600 dark:hover:text-gray-300
            transition-colors duration-150
            hover-transition
          "
          aria-label="清除搜索"
        >
          <X size={18} />
        </button>
      )}
    </div>
  );
});

SearchBar.displayName = 'SearchBar';
