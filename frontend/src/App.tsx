import { useEffect, useState } from 'react'
import { api, getSession, setSession, type Role, type Session } from './api'
import Apply from './Apply'
import Analyst from './Analyst'
import Insights from './Insights'

type Page = 'apply' | 'analyst' | 'insights'

function useHash() {
  const [hash, setHash] = useState(location.hash.slice(1))
  useEffect(() => {
    const h = () => setHash(location.hash.slice(1))
    addEventListener('hashchange', h)
    return () => removeEventListener('hashchange', h)
  }, [])
  return hash
}

export default function App() {
  const hash = useHash()
  const [session, setS] = useState<Session | null>(getSession())
  const [page, ...rest] = (hash || 'apply').split('/')
  const current = (['apply', 'analyst', 'insights'].includes(page) ? page : 'apply') as Page

  const login = async (role: Role) => {
    const username = role === 'applicant' ? 'applicant.demo' : `${role}.demo`
    const s = await api<Session & { access_token: string }>('/api/v1/auth/dev-login', { method: 'POST', json: { username, role } })
    const sess = { token: s.access_token, role, username }
    setSession(sess); setS(sess)
  }
  const logout = () => { setSession(null); setS(null) }
  const staff = session && session.role !== 'applicant'

  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header className="topbar">
        <div className="brand"><span className="logo" aria-hidden>◆</span> NovaBank <span className="muted">Digital Onboarding</span></div>
        <nav aria-label="Main">
          <a href="#apply" aria-current={current === 'apply' ? 'page' : undefined}>Open an account</a>
          <a href="#analyst" aria-current={current === 'analyst' ? 'page' : undefined}>Analyst console</a>
          <a href="#insights" aria-current={current === 'insights' ? 'page' : undefined}>Insights</a>
        </nav>
        <div className="session">
          {session ? (
            <><span className="pill">{session.role}</span><span className="muted small">{session.username}</span>
              <button className="btn ghost sm" onClick={logout}>Sign out</button></>
          ) : <span className="muted small">Not signed in</span>}
        </div>
      </header>
      <main id="main" tabIndex={-1}>
        {current === 'apply' && <Apply statusId={rest[0] === 'status' ? rest[1] : undefined} />}
        {current !== 'apply' && !staff && <StaffLogin onLogin={login} />}
        {current === 'analyst' && staff && <Analyst reviewId={rest[0]} role={session!.role} />}
        {current === 'insights' && staff && <Insights role={session!.role} />}
      </main>
      <footer className="footer muted small">Demo environment · mocked bank services · LLM via OpenRouter receives pseudonymized data only</footer>
    </>
  )
}

function StaffLogin({ onLogin }: { onLogin: (r: Role) => Promise<void> }) {
  const [err, setErr] = useState('')
  const go = (r: Role) => onLogin(r).catch(e => setErr(e.message))
  return (
    <section className="card narrow">
      <h1>Staff sign-in</h1>
      <p className="muted">Local development sign-in. On Azure this is replaced by Microsoft Entra ID (same token validation).</p>
      <div className="row gap">
        <button className="btn" onClick={() => go('analyst')}>Sign in as analyst</button>
        <button className="btn secondary" onClick={() => go('admin')}>Sign in as admin</button>
      </div>
      {err && <p role="alert" className="error">{err}</p>}
    </section>
  )
}
