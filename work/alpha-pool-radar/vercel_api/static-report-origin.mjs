function isPrivateIpv4(hostname) {
  const parts = hostname.split('.');
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part) || Number(part) > 255)) return false;
  const [a, b] = parts.map(Number);
  return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127);
}

export function staticReportUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const host = url.hostname.toLowerCase();
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null;
    if (url.pathname !== '/' || !host || host === 'localhost' || host.endsWith('.local')) return null;
    if (isPrivateIpv4(host) || host.includes(':')) return null;
    return new URL('/report.json', url.origin).toString();
  } catch {
    return null;
  }
}
