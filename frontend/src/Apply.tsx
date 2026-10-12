import { useEffect, useRef, useState } from 'react'
import { api, ApiError, AGENT_LABEL, STATUS_LABEL } from './api'

type Product = { code: string; label: string }
type Step = { agent: string; type: string; ok: boolean | null; ts: string }
type View = {
  status: string; verdict: string | null; response: string | null; product: string; applicant: string; document: string
  documents: { code: string; label: string; edd: boolean }[] | null; information_request: string | null; steps: Step[]
}

const SCENARIOS = [
  ['8', 'Happy path'], ['1', 'Low identity confidence → selfie request'], ['2', 'Identity not verified'],
  ['3', 'Medium risk → extra documents'], ['4', 'High risk → analyst review'], ['5', 'Identity provider timeout'],
]

export default function Apply({ statusId }: { statusId?: string }) {
  return statusId ? <Status id={statusId} /> : <Form />
}

function Form() {
  const [products, setProducts] = useState<Product[]>([])
  const [name, setName] = useState('')
  const [doc, setDoc] = useState('')
  const [product, setProduct] = useState('cuenta_ahorros')
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  useEffect(() => { api<Product[]>('/api/v1/products').then(setProducts).catch(setError) }, [])

  const errors = {
    name: name.trim().split(/\s+/).length < 2 || !/^[A-Za-zÀ-ÿ' .-]+$/.test(name.trim()) ? 'Enter your first and last name.' : '',
    doc: /^\d{10}$/.test(doc) ? '' : 'Your national ID has exactly 10 digits.',
  }
  const valid = !errors.name && !errors.doc

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setTouched({ name: true, doc: true })
    if (!valid) return
    setSubmitting(true); setError(null)
    try {
      const r = await api<{ onboarding_id: string }>('/api/v1/onboarding/start', {
        method: 'POST', json: { prospect_name: name.trim(), document_id: doc, product },
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      })
      location.hash = `apply/status/${r.onboarding_id}`
    } catch (err) { setError(err as ApiError) } finally { setSubmitting(false) }
  }

  return (
    <div className="grid two">
      <section className="card">
        <h1>Open your account online</h1>
        <p className="muted">It takes about a minute. We verify your identity and prepare your document checklist automatically.</p>
        <form onSubmit={submit} noValidate>
          <label htmlFor="name">Full name</label>
          <input id="name" autoComplete="name" value={name} onChange={e => setName(e.target.value)}
            onBlur={() => setTouched(t => ({ ...t, name: true }))} aria-invalid={!!(touched.name && errors.name)}
            aria-describedby="name-err" placeholder="e.g. Juan Perez" />
          <p id="name-err" className="field-error" aria-live="polite">{touched.name && errors.name}</p>

          <label htmlFor="doc">National ID number</label>
          <input id="doc" inputMode="numeric" autoComplete="off" maxLength={10} value={doc}
            onChange={e => setDoc(e.target.value.replace(/\D/g, ''))} onBlur={() => setTouched(t => ({ ...t, doc: true }))}
            aria-invalid={!!(touched.doc && errors.doc)} aria-describedby="doc-err doc-hint" placeholder="10 digits" />
          <p id="doc-hint" className="hint">{doc.length}/10 digits</p>
          <p id="doc-err" className="field-error" aria-live="polite">{touched.doc && errors.doc}</p>

          <fieldset>
            <legend>Product</legend>
            <div className="choices">
              {products.map(p => (
                <label key={p.code} className={`choice ${product === p.code ? 'selected' : ''}`}>
                  <input type="radio" name="product" value={p.code} checked={product === p.code} onChange={() => setProduct(p.code)} />
                  {p.label}
                </label>
              ))}
            </div>
          </fieldset>

          {error && <ErrorBox error={error} />}
          <button className="btn wide" disabled={submitting}>{submitting ? 'Submitting…' : 'Start application'}</button>
          <p className="muted small">Your personal data is encrypted and never shared with external AI services.</p>
        </form>
      </section>
      <aside className="card soft">
        <h2>Demo scenarios</h2>
        <p className="muted small">Bank services are mocked. The <b>last digit</b> of the ID selects the scenario:</p>
        <ul className="scenarios">
          {SCENARIOS.map(([d, label]) => (
            <li key={d}><button className="link" onClick={() => setDoc(`171234567${d}`)}>…{d}</button> {label}</li>
          ))}
        </ul>
      </aside>
    </div>
  )
}

function Status({ id }: { id: string }) {
  const [view, setView] = useState<View | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [live, setLive] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    api<View>(`/api/v1/onboarding/${id}`).then(setView).catch(setError)
    const es = new EventSource(`/api/v1/onboarding/${id}/events`)
    esRef.current = es
    es.onopen = () => setLive(true)
    es.addEventListener('status', (e: MessageEvent) => setView(JSON.parse(e.data)))
    es.onerror = () => setLive(false)   // EventSource auto-reconnects
    return () => es.close()
  }, [id])

  if (error) return <section className="card narrow"><ErrorBox error={error} /><a className="btn" href="#apply">Start over</a></section>
  if (!view) return <section className="card narrow" aria-busy="true"><p>Loading your application…</p></section>
  const done = ['approved', 'approved_edd', 'rejected', 'failed', 'expired'].includes(view.status)

  return (
    <div className="grid two">
      <section className="card">
        <div className="row between">
          <h1>Your application</h1>
          <span className={`badge ${view.status}`}>{STATUS_LABEL[view.status] || view.status}</span>
        </div>
        <p className="muted small">{view.applicant} · ID {view.document} · reference <code>{id.slice(0, 8)}</code></p>
        <Outcome view={view} id={id} />
        {done && <a className="btn secondary" href="#apply">Start another application</a>}
      </section>
      <aside className="card soft" aria-live="polite">
        <div className="row between"><h2>Progress</h2>{!done && <span className={`dot ${live ? 'on' : ''}`} title={live ? 'Live' : 'Reconnecting'} />}</div>
        <Timeline steps={view.steps} running={!done && view.status === 'processing'} />
      </aside>
    </div>
  )
}

function Outcome({ view, id }: { view: View; id: string }) {
  if (view.status === 'awaiting_client') return <InfoRequest id={id} request={view.information_request} />
  if (view.status === 'awaiting_analyst')
    return <div className="callout info"><h3>We're taking a closer look</h3><p>Your application needs a short additional review by our team. This usually takes up to 2 business days. You can close this page; your progress is saved.</p></div>
  if (view.status === 'failed')
    return <div className="callout warn"><h3>Something went wrong on our side</h3><p>Your application is safe and has been sent to our team for manual handling. Reference: <code>{id}</code></p></div>
  if (['received', 'processing'].includes(view.status))
    return <div className="callout"><div className="spinner" aria-hidden /> <p>We're checking your details. This usually takes less than a minute.</p></div>
  return (
    <>
      <div className={`callout ${view.status === 'rejected' ? 'neutral' : 'success'}`}>
        <p className="prewrap">{view.response}</p>
      </div>
      {view.documents && (
        <>
          <h3>Your document checklist</h3>
          <ul className="checklist">
            {view.documents.map(d => <li key={d.code}><input type="checkbox" aria-label={d.label} /> {d.label}{d.edd && <span className="tag">additional</span>}</li>)}
          </ul>
        </>
      )}
    </>
  )
}

function InfoRequest({ id, request }: { id: string; request: string | null }) {
  const [message, setMessage] = useState('')
  const [selfie, setSelfie] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await api(`/api/v1/onboarding/${id}/messages`, { method: 'POST', json: { message: message || 'Provided as requested.', evidence: selfie ? { selfie: true } : {} } })
      setSent(true)
    } catch (err) { setError(err as ApiError) }
  }
  if (sent) return <div className="callout"><div className="spinner" aria-hidden /><p>Thanks! We're continuing with your application.</p></div>
  return (
    <form className="callout action" onSubmit={submit}>
      <h3>We need something from you</h3>
      <p>{request || 'Please provide the additional information requested.'}</p>
      <label className="check"><input type="checkbox" checked={selfie} onChange={e => setSelfie(e.target.checked)} /> I completed the selfie / liveness check (simulated)</label>
      <label htmlFor="msg">Message (optional)</label>
      <textarea id="msg" rows={3} value={message} onChange={e => setMessage(e.target.value)} maxLength={2000} />
      {error && <ErrorBox error={error} />}
      <button className="btn">Send</button>
    </form>
  )
}

export function Timeline({ steps, running }: { steps: Step[]; running?: boolean }) {
  // Collapse started/finished pairs into one row per agent run.
  const rows: { agent: string; state: 'running' | 'ok' | 'fail' | 'info'; label: string; ts: string }[] = []
  for (const s of steps) {
    if (s.type === 'agent_started') rows.push({ agent: s.agent, state: 'running', label: AGENT_LABEL[s.agent] || s.agent, ts: s.ts })
    else if (s.type === 'agent_finished') {
      const r = [...rows].reverse().find(x => x.agent === s.agent && x.state === 'running')
      if (r) r.state = s.ok === false ? 'fail' : 'ok'
    } else rows.push({ agent: s.agent, state: 'info', label: ({ received: 'Application received', escalated: 'Waiting for input', human_input: 'Input received', finalized: 'Decision issued' } as any)[s.type] || s.type, ts: s.ts })
  }
  return (
    <ol className="timeline">
      {rows.map((r, i) => (
        <li key={i} className={r.state === 'running' && !running ? 'ok' : r.state}>
          <span className="tl-dot" aria-hidden />
          <span>{r.label}</span>
          <time className="muted small">{new Date(r.ts).toLocaleTimeString()}</time>
        </li>
      ))}
    </ol>
  )
}

export function ErrorBox({ error }: { error: ApiError | Error }) {
  const cid = (error as ApiError).correlationId
  return <div role="alert" className="callout error"><b>We couldn't complete that.</b> {error.message}{cid && <div className="small muted">Reference: <code>{cid}</code></div>}</div>
}
