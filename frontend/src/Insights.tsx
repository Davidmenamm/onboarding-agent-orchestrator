import { useEffect, useState } from 'react'
import { api, ApiError, AGENT_LABEL, type Role } from './api'
import { ErrorBox } from './Apply'

const pct = (a: number, b: number) => (b ? `${Math.round((100 * a) / b)}%` : '—')

export default function Insights({ role }: { role: Role }) {
  const [s, setS] = useState<any>(null)
  const [rules, setRules] = useState<any[]>([])
  const [docs, setDocs] = useState<any[]>([])
  const [error, setError] = useState<ApiError | null>(null)
  const [msg, setMsg] = useState('')
  const load = () => Promise.all([
    api('/api/v1/analytics/summary').then(setS), api<any[]>('/api/v1/admin/hard-rules').then(setRules), api<any[]>('/api/v1/admin/knowledge').then(setDocs),
  ]).catch(setError)
  useEffect(() => { load() }, [])
  if (error) return <section className="card"><ErrorBox error={error} /></section>
  if (!s) return <section className="card" aria-busy="true">Loading…</section>
  const t = s.totals

  const saveRule = async (code: string, value: string) => {
    try { await api(`/api/v1/admin/hard-rules/${code}`, { method: 'PUT', json: { value: Number(value) } }); setMsg(`Saved ${code}`); load() }
    catch (e) { setError(e as ApiError) }
  }
  const reingest = async () => { const r = await api('/api/v1/admin/knowledge/reingest', { method: 'POST' }); setMsg(`Knowledge sync: ${JSON.stringify(r)}`); load() }

  return (
    <div className="stack">
      <h1>Insights</h1>
      <div className="kpis">
        <Kpi label="Applications" value={t.total} />
        <Kpi label="Straight-through" value={pct(t.straight_through, t.total)} hint="approved without human review" />
        <Kpi label="Escalation rate" value={pct(t.escalated, t.total)} />
        <Kpi label="Guard vetoes" value={t.guard_overrides} hint="AI verdicts blocked by hard rules" />
        <Kpi label="Fallbacks used" value={t.fallbacks} hint="backup model / template" />
        <Kpi label="p95 time to decision" value={t.p95_ms ? `${Math.round(t.p95_ms / 1000)} s` : '—'} />
        <Kpi label="LLM cost" value={`$${Number(t.llm_cost_usd).toFixed(4)}`} />
        <Kpi label="Failed" value={t.failed} />
      </div>
      <div className="grid two">
        <section className="card">
          <h2>Outcomes by product</h2>
          <table><thead><tr><th>Product</th><th>Verdict</th><th>Count</th></tr></thead>
            <tbody>{s.by_product.map((r: any, i: number) => <tr key={i}><td>{r.product_code}</td><td>{r.verdict || 'in progress'}</td><td>{r.n}</td></tr>)}</tbody></table>
          <p className="small muted">Groups smaller than {s.k_anonymity} are shown as “&lt;{s.k_anonymity}” (k-anonymity).</p>
        </section>
        <section className="card">
          <h2>Agents</h2>
          <table><thead><tr><th>Agent</th><th>Runs</th><th>Avg</th><th>Failures</th></tr></thead>
            <tbody>{s.agents.map((a: any) => <tr key={a.agent}><td>{AGENT_LABEL[a.agent] || a.agent}</td><td>{a.runs}</td><td>{(a.avg_ms / 1000).toFixed(1)} s</td><td>{a.failures}</td></tr>)}</tbody></table>
          <h3>Escalation reasons</h3>
          <ul className="small">{s.escalation_reasons.map((r: any) => <li key={r.reason}>{r.reason}: {r.n}</li>)}</ul>
          <p className="small muted">Analyst decisions: {s.analyst_decisions.approved} approved / {s.analyst_decisions.rejected} rejected</p>
        </section>
      </div>
      <section className="card">
        <h2>Hard rules <span className="muted small">(enforced by code; the AI decides everything else)</span></h2>
        {msg && <p role="status" className="small success-text">{msg}</p>}
        <table><thead><tr><th>Rule</th><th>Condition</th><th>Effect</th><th>Version</th></tr></thead>
          <tbody>{rules.map(r => (
            <tr key={r.code}><td><b>{r.code}</b><div className="small muted">{r.description}</div></td>
              <td><code>{r.field} {r.operator} </code>{r.kind === 'threshold' && role === 'admin'
                ? <form className="inline" onSubmit={e => { e.preventDefault(); saveRule(r.code, (e.currentTarget.elements.namedItem('v') as HTMLInputElement).value) }}>
                    <input name="v" type="number" step="0.01" defaultValue={r.value} aria-label={`${r.code} value`} /><button className="btn sm">Save</button></form>
                : <code>{JSON.stringify(r.value ?? '')}</code>}</td>
              <td>{r.effect.replace(/_/g, ' ')}</td><td>v{r.version}</td></tr>))}</tbody></table>
      </section>
      <section className="card">
        <div className="row between"><h2>Policy knowledge base <span className="muted small">(single source of truth for the RAG)</span></h2>
          {role === 'admin' && <button className="btn sm" onClick={reingest}>Re-sync knowledge</button>}</div>
        <table><thead><tr><th>Document</th><th>Type</th><th>Product</th><th>Chunks</th><th>Effective</th></tr></thead>
          <tbody>{docs.map(d => <tr key={d.source_path}><td>{d.title}<div className="small muted"><code>{d.source_path}</code></div></td><td>{d.doc_type}</td><td>{d.product_code || 'all'}</td><td>{d.chunks}</td><td>{d.effective_date}</td></tr>)}</tbody></table>
      </section>
    </div>
  )
}

function Kpi({ label, value, hint }: { label: string; value: any; hint?: string }) {
  return <div className="kpi" title={hint}><div className="kpi-value">{value}</div><div className="kpi-label">{label}</div>{hint && <div className="small muted">{hint}</div>}</div>
}
