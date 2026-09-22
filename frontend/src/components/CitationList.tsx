import { type Citation, isSafeExternalUrl } from '../api';

type CitationListProps = {
  citations: Citation[];
  onOpenDocument: (citation: Citation) => void;
};

export function CitationList({ citations, onOpenDocument }: CitationListProps) {
  if (citations.length === 0) return null;

  return (
    <section className="citations" aria-label="回答来源">
      <h3>来源</h3>
      <ol>
        {citations.map((citation) => (
          <li key={citation.citation_id}>
            <strong>{citation.title}</strong>
            {citation.heading_path && <span className="citation-heading">{citation.heading_path}</span>}
            <span className="citation-lines">第 {citation.start_line}–{citation.end_line} 行</span>
            <p>{citation.text}</p>
            {isSafeExternalUrl(citation.source_url) ? (
              <a href={citation.source_url} target="_blank" rel="noreferrer">打开原文</a>
            ) : (
              <button className="text-button" type="button" onClick={() => onOpenDocument(citation)}>查看已导入原文</button>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}