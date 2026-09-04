/** Thin fetch wrapper. Vite proxies /api to the FastAPI service in dev. */

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

const qs = (params) => {
  const cleaned = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  return cleaned.length ? `?${new URLSearchParams(cleaned)}` : ''
}

export const api = {
  health: () => request('/health'),

  overview: () => request('/api/analytics/overview'),
  pipeline: () => request('/api/analytics/pipeline'),
  performance: () => request('/api/analytics/performance'),
  failures: () => request('/api/analytics/failures'),
  agentStats: () => request('/api/analytics/agent'),
  experiment: () => request('/api/analytics/experiment'),

  transactions: (params) => request(`/api/transactions${qs(params)}`),
  transaction: (id) => request(`/api/transactions/${id}`),
  analyze: (id) => request(`/api/transactions/${id}/analyze`, { method: 'POST' }),

  recover: (id) => request(`/api/recovery/${id}/recover`, { method: 'POST' }),
  approve: (id) => request(`/api/recovery/${id}/approve`, { method: 'POST' }),
  stop: (id) => request(`/api/recovery/${id}/stop`, { method: 'POST' }),

  activity: (params) => request(`/api/activity${qs(params)}`),

  runQueue: (limit) => request(`/api/demo/run-queue?limit=${limit}`, { method: 'POST' }),
  tick: () => request('/api/demo/tick', { method: 'POST' }),
  scenario: (name) => request(`/api/demo/scenario/${name}`, { method: 'POST' }),
  reset: (scope = 'decisions') => request(`/api/demo/reset?scope=${scope}`, { method: 'POST' }),
  simulateFailure: (body) =>
    request('/api/demo/simulate/failure', { method: 'POST', body: JSON.stringify(body) }),
}
