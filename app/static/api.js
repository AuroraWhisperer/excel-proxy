window.proxyApi = async function (path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers }
  });
  const payload = await response.json();
  if (!response.ok)
    throw new Error(
      payload.message ||
        payload.error?.message ||
        (typeof payload.detail === 'string'
          ? payload.detail
          : `请求失败（HTTP ${response.status}）`)
    );
  return payload;
};
