import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, AGENT_LABEL, STATUS_LABEL, type Role } from './api'
import { ErrorBox } from './Apply'

type ReviewRow = { id: string; onboarding_id: string; reasons: string[]; created_at: string; product_code: string; applicant: string; onboarding_status: string; action?: string }

const REASON_LABEL: Record<string, string> = {
  HR1_min_identity_confidence: 'Low identity confidence', HR2_high_risk: 'High risk', HR3_identity_not_verified: 'Identity not verified',
  HR4a_identity_missing: 'Identity result missing', HR4b_risk_missing: 'Risk result missing', HR5_step_budget: 'Step budget exceeded',
  HR6_supervisor_uncertainty: 'Orchestrator uncertain', llm_unavailable: 'AI unavailable', system_failure: 'System failure',
}
const reason = (r: string) => REASON_LABEL[r] || r.replace(/_/g, ' ')
const age = (ts: string) => { const m = Math.round((Date.now() - +new Date(ts)) / 60000); return m < 60 ? `${m} min` : `${Math.round(m / 60)} h` }

export default function Analyst({ reviewId, role }: { reviewId?: string; role: Role }) {
  return reviewId ? <Detail id={reviewId} role={role} /> : <Queue />
}

function Queue() {
  const [tab, setTab] = useState<'pending' | 'resolved'>('pending')
  const [rows, setRows] = useState<ReviewRow[] | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [filter, setFilter] = useState('')
  const load = useCallback(() => api<ReviewRow[]>(`/api/v1/reviews?status=${tab}`).then(setRows).catch(setError), [tab])
  useEffect(() => { setRows(null); load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])
  const shown = (rows || []).filter(r => !filter || r.reasons.some(x => reason(x).toLowerCase().includes(filter.toLowerCase())) || r.product_code.includes(filter))

  return (
    <section className="card">
      <div className="row between wrap">
        <h1>Review queue</h1>
        <div className="row gap">
          <div role="tablist" className="tabs">
            {(['pending', 'resolved'] as const).map(t => <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t}</button>)}
          </div>
          <input aria-label="Filter by reason or product" placeholder="Filter…" value={filter} onChange={e => setFilter(e.target.value)} />
        </div>
      </div>
      {error && <ErrorBox error={error} />}
      {!rows ? <p aria-busy="true">Loading…</p> : shown.length === 0 ? <p className="empty">No {tab} reviews. 🎉</p> : (
        <table>
          <thead><tr><th>Applicant</th><th>Product</th><th>Reasons</th><th>{tab === 'pending' ? 'Waiting' : 'Decision'}</th><th /></tr></thead>
          <tbody>
            {shown.map(r => (
              <tr key={r.id}>
                <td>{r.applicant}</td>
                <td>{r.product_code}</td>
                <td>{r.reasons.map(x => <span key={x} className={`chip ${x.includes('risk') ? 'red' : ''}`}>{reason(x)}</span>)}</td>
                <td>{tab === 'pending' ? <span className={`sla ${(Date.now() - +new Date(r.created_at)) > 864e5 ? 'late' : ''}`}>{age(r.created_at)}</span> : r.action}</td>
                <td><a className="btn sm" href={`#analyst/${r.id}`}>Open</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function Detail({ id }: { id: string; role: Role }) {
  const [d, setD] = useState<any>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const load = useCallback(() => api(`/api/v1/reviews/${id}`).then(setD).catch(setError), [id])
  useEffect(() => { load() }, [load])
  if (error) return <section className="card"><ErrorBox error={error} /><a href="#analyst">← Back to queue</a></section>
  if (!d) return <section className="card" aria-busy="true">Loading case…</section>
  const p = d.review.proposal || {}
  const pending = d.review.status === 'pending'

  return (
    <div className="stack">
      <a href="#analyst" className="back">← Back to queue</a>
      <section className="card">
        <div className="row between wrap">
          <div>
            <h1>{d.onboarding.applicant} <span className="muted">· {d.onboarding.product}</span></h1>
            <p className="muted small">ID {d.onboarding.document} · onboarding <code>{d.review.onboarding_id.slice(0, 8)}</code> · <span className={`badge ${d.onboarding.status}`}>{STATUS_LABEL[d.onboarding.status]}</span></p>
          </div>
          <Reveal id={id} />
        </div>
        <div className="row wrap gap">{d.review.reasons.map((x: string) => <span key={x} className="chip red">{reason(x)}</span>)}</div>
      </section>

      <div className="grid three">
        <section className="card">
          <h2>Identity</h2>
          {d.identity ? <>
            <p>{d.identity.verified ? '✅ Verified' : '❌ Not verified'}</p>
            <Confidence value={d.identity.confidence} threshold={0.8} />
          </> : <p className="muted">No result{d.errors.length ? ' — tool error' : ''}</p>}
        </section>
        <section className="card">
          <h2>Risk</h2>
          {d.risk ? <>
            <span className={`badge risk-${d.risk.risk_level}`}>{d.risk.risk_level} risk</span>
            {d.risk.matches.length ? <ul className="small">{d.risk.matches.map((m: string) => <li key={m}>{m}</li>)}</ul> : <p className="muted small">No list matches</p>}
          </> : <p className="muted">No result</p>}
        </section>
        <section className="card">
          <h2>Documents</h2>
          {d.documents ? <ul className="small">{d.documents.documents.map((x: any) => <li key={x.code}>{x.label}{x.edd && <span className="tag">EDD</span>}</li>)}</ul> : <p className="muted">Not prepared yet</p>}
        </section>
      </div>

      <div className="grid two">
        <section className="card">
          <h2>Proposed solution</h2>
          <p>{p.summary}</p>
          {p.recommended_actions?.length > 0 && <ol>{p.recommended_actions.map((a: string) => <li key={a}>{a}</li>)}</ol>}
          {p.citations?.length > 0 && <p className="small muted">Policy sources: {p.citations.map((c: string) => <code key={c}>{c}</code>)}</p>}
          {pending ? <DecisionForm id={id} onDone={load} /> : <div className="callout success">Resolved: <b>{d.review.action}</b> — {d.review.notes}</div>}
        </section>
        <section className="card">
          <h2>Orchestrator reasoning</h2>
          <ol className="trail">
            {d.supervisor_trail.map((t: any, i: number) => (
              <li key={i} className={t.vetoed ? 'vetoed' : ''}>
                <div className="row between"><b>Step {t.step}: {(t.next || []).map((n: string) => AGENT_LABEL[n] || n).join(' + ')}{t.verdict && ` → ${t.verdict}`}</b>
                  {typeof t.uncertainty === 'number' && <span className="small muted">uncertainty {t.uncertainty.toFixed(2)}</span>}</div>
                <p className="small">{t.rationale}</p>
                {t.vetoed && <p className="small error">Vetoed by policy guard: {(t.violations || []).map(reason).join(', ')}</p>}
              </li>
            ))}
          </ol>
          {d.agent_notes.length > 0 && <details><summary>Agent notes ({d.agent_notes.length})</summary>
            {d.agent_notes.map((n: any, i: number) => <p key={i} className="small"><b>{AGENT_LABEL[n.agent]}:</b> {n.note}</p>)}</details>}
          {d.errors.length > 0 && <details open><summary className="error">Errors ({d.errors.length})</summary>
            {d.errors.map((e: any, i: number) => <p key={i} className="small"><code>{e.tool || e.agent}</code> {e.error}</p>)}</details>}
        </section>
      </div>
    </div>
  )
}

function Confidence({ value, threshold }: { value: number; threshold: number }) {
  return (
    <div className="meter" role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={1} aria-label="Identity confidence">
      <div className={`fill ${value < threshold ? 'low' : ''}`} style={{ width: `${value * 100}%` }} />
      <div className="threshold" style={{ left: `${threshold * 100}%` }} title={`threshold ${threshold}`} />
      <span className="meter-label">{(value * 100).toFixed(0)}% <span className="muted">(min {threshold * 100}%)</span></span>
    </div>
  )
}

function DecisionForm({ id, onDone }: { id: string; onDone: () => void }) {
  const [action, setAction] = useState<'approve' | 'reject' | 'request_info' | ''>('')
  const [notes, setNotes] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const valid = action && notes.trim().length >= 5
  const submit = async () => {
    setBusy(true); setError(null)
    try { await api(`/api/v1/reviews/${id}/decision`, { method: 'POST', json: { action, notes } }); setTimeout(onDone, 800) }
    catch (e) { setError(e as ApiError) } finally { setBusy(false); setConfirming(false) }
  }
  return (
    <form className="decision" onSubmit={e => { e.preventDefault(); if (valid) setConfirming(true) }}>
      <fieldset>
        <legend>Your decision</legend>
        <div className="choices">
          {([['approve', 'Approve'], ['reject', 'Reject'], ['request_info', 'Request information']] as const).map(([v, l]) => (
            <label key={v} className={`choice ${action === v ? 'selected' : ''} ${v}`}>
              <input type="radio" name="action" checked={action === v} onChange={() => setAction(v)} /> {l}
            </label>
          ))}
        </div>
      </fieldset>
      <label htmlFor="notes">Notes (required, kept in the audit trail)</label>
      <textarea id="notes" rows={3} value={notes} onChange={e => setNotes(e.target.value)} placeholder="What did you check and why?" />
      {error && <ErrorBox error={error} />}
      {!confirming
        ? <button className="btn" disabled={!valid}>Submit decision</button>
        : <div className="callout warn row between wrap"><span>Confirm <b>{action.replace('_', ' ')}</b>? This resumes the case.</span>
          <span className="row gap"><button type="button" className="btn ghost" onClick={() => setConfirming(false)}>Cancel</button>
            <button type="button" className="btn" disabled={busy} onClick={submit}>{busy ? 'Sending…' : 'Confirm'}</button></span></div>}
    </form>
  )
}

function Reveal({ id }: { id: string }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [data, setData] = useState<any>(null)
  const [error, setError] = useState<ApiError | null>(null)
  if (data) return <div className="callout warn small">🔓 {data.prospect_name} · {data.document_id} <span className="muted">(access audited)</span></div>
  if (!open) return <button className="btn ghost sm" onClick={() => setOpen(true)}>Reveal personal data…</button>
  return (
    <form className="reveal" onSubmit={async e => { e.preventDefault(); try { setData(await api(`/api/v1/reviews/${id}/reveal`, { method: 'POST', json: { reason } })) } catch (err) { setError(err as ApiError) } }}>
      <label htmlFor="why">Reason for access (audited)</label>
      <input id="why" value={reason} onChange={e => setReason(e.target.value)} minLength={10} required placeholder="e.g. compare with branch records" />
      <button className="btn sm" disabled={reason.length < 10}>Reveal</button>
      {error && <ErrorBox error={error} />}
    </form>
  )
}
