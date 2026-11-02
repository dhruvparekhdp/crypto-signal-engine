export function plainApiError(status: number, body: string): string {
  if (status === 401) {
    return 'You need to sign in to view this page.';
  }
  if (status === 403) {
    return 'You do not have permission for this action.';
  }
  if (status === 404) {
    return 'That data is not available on the server.';
  }
  if (status >= 500) {
    return 'The server had a problem. We will retry shortly.';
  }
  if (status === 0) {
    return 'The server did not answer. Retrying in 15 s.';
  }
  try {
    const j = JSON.parse(body) as { error?: string; reason?: string };
    return j.error || j.reason || `Request failed (${status}).`;
  } catch {
    return `Request failed (${status}).`;
  }
}
