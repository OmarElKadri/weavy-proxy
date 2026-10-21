// console-snippet.js — run this in DevTools Console while signed in at app.weavy.ai.
// It prints the Firebase refresh token the browserless proxy needs. Treat the output
// like a password: anyone holding it can use your Weavy account.
(async () => {
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open('firebaseLocalStorageDb');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  if (![...db.objectStoreNames].includes('firebaseLocalStorage')) {
    console.error('Not signed in (firebaseLocalStorage store missing). Sign in first.');
    return;
  }
  const all = await new Promise((res, rej) => {
    const rq = db.transaction('firebaseLocalStorage', 'readonly').objectStore('firebaseLocalStorage').getAll();
    rq.onsuccess = () => res(rq.result);
    rq.onerror = () => rej(rq.error);
  });
  for (const row of all) {
    const v = row && row.value;
    if (v && v.stsTokenManager && v.stsTokenManager.refreshToken) {
      console.log(JSON.stringify({
        email: v.email,
        refresh_token: v.stsTokenManager.refreshToken,
      }, null, 2));
    }
  }
})();
