import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';

interface MessageContentProps {
  content: string;
}

/**
 * 消息内容渲染组件 - 支持 Markdown 和 GFM
 * 
 * 功能:
 * - GitHub Flavored Markdown (表格、任务列表、删除线等)
 * - 代码块语法高亮
 * - 代码复制按钮
 * - 自动链接识别
 */
export function MessageContent({ content }: MessageContentProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code(props) {
          const { node, inline, className, children, ...rest } = props as any;
          const match = /language-(\w+)/.exec(className || '');
          const code = String(children).replace(/\n$/, '');
          
          return !inline && match ? (
            <CodeBlock language={match[1]} code={code} />
          ) : (
            <CodeBlock language="" code={code} inline />
          );
        },
        // 表格样式
        table({ children }) {
          return (
            <div className="overflow-x-auto my-4">
              <table className="min-w-full border-collapse border border-gray-300 dark:border-gray-700">
                {children}
              </table>
            </div>
          );
        },
        th({ children }) {
          return (
            <th className="border border-gray-300 dark:border-gray-700 bg-gray-100 dark:bg-gray-800 px-4 py-2 text-left font-semibold">
              {children}
            </th>
          );
        },
        td({ children }) {
          return (
            <td className="border border-gray-300 dark:border-gray-700 px-4 py-2">
              {children}
            </td>
          );
        },
        // 链接样式
        a({ href, children }) {
          return (
            <a
              href={href}
              className="text-primary hover:underline"
              target="_blank"
              rel="noopener noreferrer"
            >
              {children}
            </a>
          );
        },
        // 任务列表样式
        input({ type, checked, ...props }) {
          if (type === 'checkbox') {
            return (
              <input
                type="checkbox"
                checked={checked}
                disabled
                className="mr-2"
                {...props}
              />
            );
          }
          return <input type={type} checked={checked} {...props} />;
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
