// Cloudflare Pages Function — proxies requests to courses.illinois.edu/cisapp/explorer
// so the client can read live section enrollment status without CORS issues.
//
// A request to /api/illinois/schedule/2026/spring/CS/225/35917.xml is forwarded to
// https://courses.illinois.edu/cisapp/explorer/schedule/2026/spring/CS/225/35917.xml
// and the response is returned with permissive CORS headers.

const UPSTREAM = 'https://courses.illinois.edu/cisapp/explorer/';

export async function onRequestGet({ params }) {
  const subpath = Array.isArray(params.path) ? params.path.join('/') : (params.path || '');

  if (!/^[A-Za-z0-9._\-\/]+$/.test(subpath)) {
    return new Response('Bad path', { status: 400 });
  }

  const upstreamRes = await fetch(UPSTREAM + subpath, {
    cf: { cacheTtl: 60, cacheEverything: true },
  });

  return new Response(upstreamRes.body, {
    status: upstreamRes.status,
    headers: {
      'content-type': upstreamRes.headers.get('content-type') || 'application/xml',
      'access-control-allow-origin': '*',
      'cache-control': 'public, max-age=60',
    },
  });
}
