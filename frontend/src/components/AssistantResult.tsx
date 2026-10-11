import { type Citation, type UnifiedResult } from '../api';
import { CitationList } from './CitationList';

const validationLabels = {
  not_required: '无需执行验证（只读回答）', passed: '验证通过',
  failed: '验证失败，任务目标尚未验证通过', unavailable: '验证不可用，未验证通过',
};

export function AssistantResult({ result, onOpenDocument }: {
  result: UnifiedResult; onOpenDocument: (citation: Citation) => void;
}) {
  return <section className="assistant-result" aria-label="助手结果">
    {result.answer && <p className="task-answer">{result.answer}</p>}
    <p className="capability-note">验证状态：{result.validation_status ? validationLabels[result.validation_status] : '尚无验证事实'}</p>
    {['no_results', 'insufficient'].includes(result.evidence_status ?? '') && <p className="notice">材料不足，尚无充分来源支撑。</p>}
    {!!result.citations?.length && <CitationList citations={result.citations} onOpenDocument={onOpenDocument} />}
    {result.outline && <details><summary>查看提纲</summary><pre>{JSON.stringify(result.outline, null, 2)}</pre></details>}
    {result.draft != null && <details><summary>查看草稿</summary><pre>{result.draft}</pre></details>}
    {!!result.test_results?.length && <section aria-label="助手测试报告"><h3>测试结果</h3>{result.test_results.map((report, index) =>
      <details key={index}><summary>测试 {index + 1}：{report.passed === true ? '通过' : '未通过'} · 退出码 {report.exit_code ?? '未知'}</summary><pre>{JSON.stringify(report, null, 2)}</pre></details>)}</section>}
    {!!result.artifacts?.length && <section aria-label="助手产物"><h3>产物</h3><ul>{result.artifacts.map((artifact) =>
      <li key={artifact.artifact_id}>{artifact.kind === 'diff' ? '本轮 diff' : artifact.kind} · {artifact.size_bytes} bytes
        <details><summary>产物元数据</summary><pre>{JSON.stringify(artifact, null, 2)}</pre></details>
      </li>)}</ul></section>}
    {result.saved_path && <p className="notice success">草稿已保存：{result.saved_path}</p>}
  </section>;
}