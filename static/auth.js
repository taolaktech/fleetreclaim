/**
 * The app's only Firebase client: one initialization, one auth listener.
 *
 * Firebase config is fetched from /api/config (web config is public by design;
 * Admin credentials stay on the server). When the server reports auth disabled
 * — a machine with no Firebase project — the app runs open for local development
 * and Firebase is never loaded.
 */

const SDK = 'https://www.gstatic.com/firebasejs/10.12.5';

const state = {
  user: null,          // { uid, email, displayName, photoURL } or null
  loading: true,
  authEnabled: true,
  error: '',
};

const listeners = new Set();
let firebaseAuth = null;      // the Auth instance, once Firebase is loaded
let firebaseLib = null;       // the firebase-auth module namespace

const notify = () => listeners.forEach(fn => fn(snapshot()));

function snapshot() {
  return {
    user: state.user,
    loading: state.loading,
    isAuthenticated: !!state.user,
    authEnabled: state.authEnabled,
    error: state.error,
  };
}

/** Subscribe to auth state; called immediately with the current value. */
export function onAuth(fn) {
  listeners.add(fn);
  fn(snapshot());
  return () => listeners.delete(fn);
}

export const getUser = () => state.user;

const profile = user => ({
  uid: user.uid,
  email: user.email || '',
  displayName: user.displayName || '',
  photoURL: user.photoURL || '',
});

let readyPromise = null;

/** Resolves once Firebase has settled the initial auth state. */
export function authReady() {
  if (!readyPromise) readyPromise = init();
  return readyPromise;
}

async function init() {
  let config;
  try {
    config = await (await fetch('/api/config')).json();
  } catch (e) {
    state.loading = false;
    state.error = 'Could not reach the server.';
    notify();
    return snapshot();
  }

  state.authEnabled = !!config.auth_enabled;
  if (!state.authEnabled) {
    state.user = { uid: 'dev-local', email: 'dev@localhost', displayName: 'Local dev', photoURL: '' };
    state.loading = false;
    notify();
    return snapshot();
  }

  if (!config.firebase || !config.firebase.apiKey) {
    state.loading = false;
    state.error = 'Firebase is not configured on the server.';
    notify();
    return snapshot();
  }

  const [{ initializeApp }, auth] = await Promise.all([
    import(`${SDK}/firebase-app.js`),
    import(`${SDK}/firebase-auth.js`),
  ]);
  firebaseLib = auth;
  firebaseAuth = auth.getAuth(initializeApp(config.firebase));
  await auth.setPersistence(firebaseAuth, auth.browserLocalPersistence).catch(() => {});

  // A redirect sign-in finishes here; surface its failure like a popup failure.
  auth.getRedirectResult(firebaseAuth).catch(error => {
    state.error = messageFor(error);
    notify();
  });

  return new Promise(resolve => {
    auth.onAuthStateChanged(firebaseAuth, user => {
      state.user = user ? profile(user) : null;
      state.loading = false;
      notify();
      resolve(snapshot());
    });
  });
}

/** Human-readable, non-sensitive text for a Firebase auth error. */
function messageFor(error) {
  const code = (error && error.code) || '';
  if (code === 'auth/popup-closed-by-user' || code === 'auth/cancelled-popup-request') return '';
  return {
    'auth/popup-blocked': 'Your browser blocked the sign-in popup — retrying with a redirect.',
    'auth/network-request-failed': 'Network problem — check your connection and try again.',
    'auth/user-disabled': 'This account has been disabled.',
    'auth/unauthorized-domain': 'This domain is not authorized in the Firebase console.',
    'auth/operation-not-allowed': 'Google sign-in is not enabled for this Firebase project.',
  }[code] || 'Sign-in failed. Please try again.';
}

/** "Continue with Google" — popup first, redirect when popups are blocked. */
export async function signInWithGoogle() {
  await authReady();
  if (!state.authEnabled) return snapshot();
  if (!firebaseAuth) throw new Error('Authentication is unavailable right now.');

  const provider = new firebaseLib.GoogleAuthProvider();
  // Let people pick between personal and Workspace accounts every time.
  provider.setCustomParameters({ prompt: 'select_account' });
  state.error = '';
  try {
    await firebaseLib.signInWithPopup(firebaseAuth, provider);
  } catch (error) {
    const code = (error && error.code) || '';
    if (code === 'auth/popup-blocked' || code === 'auth/operation-not-supported-in-this-environment') {
      await firebaseLib.signInWithRedirect(firebaseAuth, provider);
      return snapshot();
    }
    state.error = messageFor(error);
    notify();
    if (state.error) throw new Error(state.error);
  }
  return snapshot();
}

export async function signOut() {
  await authReady();
  if (firebaseAuth) await firebaseLib.signOut(firebaseAuth);
  state.user = null;
  notify();
  window.location.href = '/login';
}

/** Fresh Firebase ID token for the signed-in user, or '' when auth is off. */
export async function idToken(forceRefresh = false) {
  await authReady();
  if (!state.authEnabled || !firebaseAuth || !firebaseAuth.currentUser) return '';
  return firebaseAuth.currentUser.getIdToken(forceRefresh);
}

/**
 * fetch() with the Firebase ID token attached. A 401 means the session is no
 * longer valid: retry once with a refreshed token, then send the user to /login.
 */
export async function authFetch(url, options = {}, retry = true) {
  const token = await idToken(!retry);
  const headers = new Headers(options.headers || {});
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(url, { ...options, headers });
  if (response.status === 401) {
    if (retry && token) return authFetch(url, options, false);
    await signOut();
  }
  return response;
}

/** Send unauthenticated visitors to /login; resolves with the signed-in user. */
export async function requireAuth() {
  const current = await authReady();
  if (!current.isAuthenticated) {
    window.location.replace('/login');
    return null;
  }
  return current.user;
}
