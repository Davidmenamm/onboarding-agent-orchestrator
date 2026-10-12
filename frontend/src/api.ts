export type Role = 'applicant' | 'analyst' | 'admin'
export type Session = { token: string; role: Role; username: string }

const KEY = 'onboarding.session'
export const getSession = (): Session | null => JSON.parse(sessionStorage.getItem(KEY) || 'null')
export const setSession = (s: Session | null) => (s ? sessionStorage.setItem(KEY, JSON.stringify(s)) : sessionStorage.removeItem(KEY))

export class ApiError extends Error {
  constructor(public status: number, message: string, public correlationId?: string) { super(message) }
}

export async function api<T = any>(path: string, opts: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers as Record<string, string>) }
  const s = getSession()
  if (s) headers.Authorization = `Bearer ${s.token}`
  if (opts.json !== undefined) headers['Content-Type'] = 'application/json'
  let res: Response | undefined
  for (let attempt = 0; attempt < 3; attempt++) {           // retry transient errors with backoff
    res = await fetch(path, { ...opts, headers, body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body })
    if (![502, 503, 504, 429].includes(res.status)) break
    await new Promise(r => setTimeout(r, 400 * 2 ** attempt))
  }
  const body = await res!.json().catch(() => ({}))
  if (!res!.ok) {
    const d = body.detail
    const msg = Array.isArray(d) ? d.map((e: any) => `${e.loc?.slice(-1)[0]}: ${e.msg}`).join('; ') : d || res!.statusText
    throw new ApiError(res!.status, msg, res!.headers.get('x-request-id') || body.correlation_id)
  }
  return body
}

export const AGENT_LABEL: Record<string, string> = {
  supervisor: 'Orchestrator', identity: 'Identity verification', risk: 'Risk screening',
  documentation: 'Documentation', remediation: 'Remediation plan', response: 'Response',
  applicant: 'Application', orchestrator: 'Orchestrator', analyst: 'Analyst', client: 'You', guard: 'Policy guard', system: 'System',
}

export const STATUS_LABEL: Record<string, string> = {
  received: 'Received', processing: 'In progress', awaiting_client: 'Action needed', awaiting_analyst: 'Under review',
  approved: 'Approved', approved_edd: 'Approved', rejected: 'Not approved', failed: 'Needs attention', expired: 'Expired',
}
